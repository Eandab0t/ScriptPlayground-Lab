"""ScriptPlayground core: run Discord bot code locally and render what it sends.

Write plain discord.py code (embeds, Views, Modals) in the web editor and press
Run. The code executes in a dedicated event-loop thread with a mocked
`Interaction` — nothing touches the network. Everything the script sends,
edits, or opens is captured and serialized for the UI.

Scripting contract (all optional, plain functions in the module namespace):

    async def main():                          # runs once per Run; may loop forever
        ...
    async def on_click(interaction, custom_id, values=None):
        ...                                    # a button was clicked / select chosen
    async def on_submit(interaction, values, modal_id=None):
        ...                                    # a modal was submitted (values: {custom_id: text})
    async def on_message(message):
        ...                                    # you typed in the composer

Helpers injected into the namespace: `send(...)` posts to the playground
channel, `print(...)` writes to the event console, `client` is a mock client,
`Session` is the playground session itself.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import io
import logging
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands

log = logging.getLogger(__name__)

USER_ID = 123456789012345678
USER_NAME = "You"
BOT_ID = 987654321098765432
GUILD_ID = 900000000000000001
CHANNEL_ID = 900000000000000002
MEMBER_IDS = {"Alice": 111111111111111111, "Bob": 222222222222222222, "Carol": 333333333333333333}
ROLE_NAMES = ["Members", "Moderators", "Admins"]


class ScriptStuck(Exception):
    """The script's thread never answered within the timeout (likely a wedged loop)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- mock discord


class MockRole:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name
        self.position = ROLE_NAMES.index(name) + 1 if name in ROLE_NAMES else 0
        self.permissions = discord.Permissions.none()
        self.colour = self.color = discord.Color.blurple()
        self.hoist = name != "Members"

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    def __str__(self) -> str:
        return self.name


class MockMember:
    def __init__(self, guild: MockGuild, member_id: int, name: str, *, bot: bool = False):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.global_name = name
        self.bot = bot
        self.guild = guild
        self.roles = [guild.roles[0]]
        self.joined_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.status = discord.Status.online
        self.avatar = None
        self.permission_override: discord.Permissions | None = None

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    @property
    def guild_permissions(self) -> discord.Permissions:
        if self.guild.owner_id == self.id:
            return discord.Permissions.all()
        if self.permission_override is not None:
            return discord.Permissions(self.permission_override.value)
        if self.bot:
            return discord.Permissions.all()
        permissions = discord.Permissions(view_channel=True, send_messages=True,
                                          embed_links=True, manage_channels=True)
        for role in self.roles:
            permissions |= role.permissions
        return permissions

    def __str__(self) -> str:
        return self.name


class MockGuild:
    def __init__(self, session: Session) -> None:
        self._session = session
        self.id = GUILD_ID
        self.name = "Playground Server"
        self.member_count = 128
        self.owner_id = USER_ID
        self.roles = [MockRole(GUILD_ID, "@everyone")]
        self.roles += [MockRole(GUILD_ID + i + 1, name) for i, name in enumerate(ROLE_NAMES)]
        self.members = [
            self._member(USER_ID, USER_NAME),
            self._member(BOT_ID, "Playground Bot", bot=True),
        ]
        self.members += [self._member(uid, name) for name, uid in MEMBER_IDS.items()]
        self.me = self.members[1]

    def _member(self, member_id: int, name: str, *, bot: bool = False) -> MockMember:
        return MockMember(self, member_id, name, bot=bot)

    def get_member(self, member_id: int) -> MockMember | None:
        return next((m for m in self.members if m.id == member_id), None)

    def get_role(self, role_id: int) -> MockRole | None:
        return next((r for r in self.roles if r.id == role_id), None)

    @property
    def text_channels(self) -> list[MockChannel]:
        return list(self._session.channels.values())

    def get_channel(self, channel_id: int) -> MockChannel | None:
        return self._session.channels.get(str(channel_id))

    async def create_text_channel(self, name: str, *args, **kwargs) -> MockChannel:
        return self._session.make_channel(str(name))

    def __str__(self) -> str:
        return self.name


class _FakeResponse:
    """Minimal stand-in for an aiohttp response so discord exceptions construct."""

    status = 404
    reason = "Not Found"

    def __init__(self) -> None:
        self.headers = {}

    def json(self):
        return {"message": "Not Found"}


_NOT_FOUND = _FakeResponse()


class _ForbiddenResponse(_FakeResponse):
    status = 403
    reason = "Forbidden"


_FORBIDDEN = _ForbiddenResponse()


class MockMessage:
    """Handle returned from sends; edits/deletes go through the session."""

    def __init__(self, session: Session, message_id: str, author: MockMember | None = None):
        self._session = session
        self.id = message_id
        msg = session.messages.get(message_id) or {}
        self.channel = session.channels.get(msg.get("channel") or "", session.channel)
        self.guild = session.guild
        self.author = author or session.guild.me
        self.created_at = datetime.now(timezone.utc)
        self.referenced_message = None

    @property
    def content(self) -> str:
        return (self._session.messages.get(self.id) or {}).get("content") or ""

    @property
    def embeds(self) -> list[discord.Embed]:
        return [discord.Embed.from_dict(e) for e in (self._session.messages.get(self.id) or {}).get("embeds", [])]

    async def edit(self, **kwargs):
        self._session.update_message(self.id, **kwargs)
        return self

    async def delete(self, delay: float | None = None) -> None:
        if delay:
            await asyncio.sleep(delay)
        if not self.channel.permissions_for(self.guild.me).manage_messages:
            raise discord.Forbidden(_FORBIDDEN, "missing manage_messages permission")
        self._session.delete_message(self.id)

    async def reply(self, content=None, **kwargs) -> MockMessage:
        return await self.channel.send(content, **kwargs)

    async def add_reaction(self, emoji) -> None:
        self._session.log("➕", f"reacted {emoji} to message #{self.id}")

    async def pin(self, **kwargs) -> None:
        self._session.log("📌", f"message #{self.id} pinned")

    def __str__(self) -> str:
        return self.content or "(embed)"


class MockChannel:
    def __init__(self, session: Session, channel_id: int, name: str):
        self._session = session
        self.id = channel_id
        self.name = name
        self.guild = session.guild
        self.mention = f"<#{channel_id}>"
        self.overwrites: dict[int, discord.PermissionOverwrite] = {}

    def __str__(self) -> str:
        return f"#{self.name}"

    def permissions_for(self, member: MockMember) -> discord.Permissions:
        permissions = member.guild_permissions
        if permissions.administrator:
            return discord.Permissions.all()
        allow, deny = self.overwrites.get(self.guild.id, discord.PermissionOverwrite()).pair()
        permissions.handle_overwrite(allow.value, deny.value)
        role_allow = role_deny = 0
        for role in member.roles:
            allow, deny = self.overwrites.get(role.id, discord.PermissionOverwrite()).pair()
            role_allow |= allow.value
            role_deny |= deny.value
        permissions.handle_overwrite(role_allow, role_deny)
        allow, deny = self.overwrites.get(member.id, discord.PermissionOverwrite()).pair()
        permissions.handle_overwrite(allow.value, deny.value)
        return permissions

    async def send(self, content=None, **kwargs) -> MockMessage:
        if self._session.channels.get(str(self.id)) is not self:
            raise discord.NotFound(_NOT_FOUND, "channel was deleted")
        author = kwargs.get("author") or self.guild.me
        if not self.permissions_for(author).send_messages:
            raise discord.Forbidden(_FORBIDDEN, "missing send_messages permission")
        msg = self._session.add_message(channel_id=self.id, content=content, **kwargs)
        return MockMessage(self._session, msg["id"], msg.get("author_obj"))

    async def delete(self) -> None:
        if not self.permissions_for(self.guild.me).manage_channels:
            raise discord.Forbidden(_FORBIDDEN, "missing manage_channels permission")
        self._session.delete_channel(self.id)

    async def fetch_message(self, message_id) -> MockMessage:
        if str(message_id) not in self._session.messages:
            raise discord.NotFound(_NOT_FOUND, "message not found")
        return MockMessage(self._session, str(message_id))

    def get_partial_message(self, message_id) -> MockMessage:
        return MockMessage(self._session, str(message_id))

    @property
    def created_at(self) -> datetime:
        return datetime(2024, 1, 1, tzinfo=timezone.utc)

    def typing(self):
        return _NullAsyncCtx()


class _NullAsyncCtx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class MockClient:
    """Just enough of discord.Client for scripts to introspect."""

    def __init__(self, session: Session):
        self._session = session

    @property
    def user(self) -> MockMember:
        return self._session.guild.me

    @property
    def guilds(self) -> list[MockGuild]:
        return [self._session.guild]

    @property
    def latency(self) -> float:
        return 0.042

    def is_ready(self) -> bool:
        return True

    def get_user(self, user_id: int) -> MockMember | None:
        return self._session.guild.get_member(user_id)

    def get_guild(self, guild_id: int) -> MockGuild | None:
        return self._session.guild if guild_id == self._session.guild.id else None

    def get_channel(self, channel_id: int) -> MockChannel | None:
        return self._session.channels.get(str(channel_id))


class MockResponse:
    """interaction.response: first answer to an interaction."""

    def __init__(self, interaction: MockInteraction):
        self._interaction = interaction
        self._done = False

    @property
    def is_done(self) -> bool:
        return self._done

    def _capture(self, kwargs: dict) -> dict:
        return self._interaction._session.add_message(**kwargs)

    async def send_message(self, content=None, **kwargs) -> MockMessage:
        if self._done:
            raise RuntimeError("This interaction has already been responded to.")
        session = self._interaction._session
        kwargs["author"] = session.guild.me
        msg = await self._interaction.channel.send(content, **kwargs)
        self._done = True
        self._interaction._last = msg.id
        return msg

    async def defer(self, thinking: bool = False, ephemeral: bool = False, **kwargs) -> None:
        self._done = True
        self._interaction._ephemeral_followups = ephemeral
        self._interaction._session.log("💭", "interaction deferred (thinking)" if thinking else "interaction deferred")

    async def edit_message(self, content=None, **kwargs) -> MockMessage:
        self._done = True
        session = self._interaction._session
        session.update_message(self._interaction._last, content=content, **kwargs)
        return MockMessage(session, self._interaction._last)

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._done = True
        self._interaction._session.open_modal(modal, self._interaction._last)

    async def pong(self) -> None:
        self._done = True


class MockFollowup:
    def __init__(self, interaction: MockInteraction):
        self._interaction = interaction

    async def send(self, content=None, **kwargs) -> MockMessage:
        session = self._interaction._session
        if "ephemeral" not in kwargs and self._interaction._ephemeral_followups:
            kwargs["ephemeral"] = True
        kwargs["author"] = session.guild.me
        msg = await self._interaction.channel.send(content, **kwargs)
        self._interaction._last = msg.id
        return msg

    async def edit_message(self, content=None, **kwargs) -> MockMessage:
        session = self._interaction._session
        session.update_message(self._interaction._last, content=content, **kwargs)
        return MockMessage(session, self._interaction._last)


class MockInteraction:
    """Stands in for discord.Interaction: responses are captured, not sent."""

    def __init__(self, session: Session, source_message_id: str | None = None,
                 custom_id: str | None = None, values: list | None = None,
                 interaction_type: discord.InteractionType = discord.InteractionType.application_command):
        self._session = session
        self._interaction_type = interaction_type
        guild = session.guild
        self.user = session.active_user
        self.author = self.user
        self.guild = guild
        self.guild_id = guild.id
        self.channel = session.channel
        self.channel_id = session.channel.id
        self.client = session.client
        self.message = MockMessage(session, source_message_id) if source_message_id else None
        # an interaction happens in the channel its message lives in
        src = session.messages.get(source_message_id or "") or {}
        self.channel = session.channels.get(src.get("channel") or "", session.channel)
        self.channel_id = self.channel.id
        self.permissions = self.channel.permissions_for(self.user)
        self.app_permissions = self.channel.permissions_for(guild.me)
        self.command = _CommandRef("playground")
        self.data = {"custom_id": custom_id, "values": values or []} if custom_id else {}
        self.namespace = _Namespace()
        self.token = "mock-token"
        self.application_id = BOT_ID
        self.locale = "en-US"
        self._last = source_message_id or (session.order[-1] if session.order else None)
        self._ephemeral_followups = False
        self.response = MockResponse(self)
        self.followup = MockFollowup(self)

    @property
    def type(self) -> discord.InteractionType:
        return self._interaction_type

    def is_done(self) -> bool:
        return self.response.is_done

    async def delete_original_response(self) -> None:
        self._session.delete_message(self._last)

    async def edit_original_response(self, content=None, **kwargs) -> MockMessage:
        self._session.update_message(self._last, content=content, **kwargs)
        return MockMessage(self._session, self._last)

    async def original_response(self) -> MockMessage:
        return MockMessage(self._session, self._last)


class _CommandRef:
    def __init__(self, name: str):
        self.name = name
        self.qualified_name = name


class _Namespace:
    """interaction.namespace: no bound params in the playground."""


# --------------------------------------------------------------- serialization

_KINDS = {
    "Button": "button",
    "Select": "select",
    "RoleSelect": "role_select",
    "UserSelect": "user_select",
    "ChannelSelect": "channel_select",
    "MentionableSelect": "mentionable_select",
    "TextInput": "text_input",
}

# Classic layout items inside V2 trees serialize through _v2_item_to_json; the
# detection itself is just isinstance(view, LayoutView).


def _stash_view(view) -> tuple[list[dict] | None, list[dict] | None]:
    """Serialize a view into (classic_components, v2_components).

    Classic discord.ui.View -> the flat row/kind dicts; ui.LayoutView
    (Components V2 — not a View subclass) -> the recursive v2 tree.
    """
    if isinstance(view, discord.ui.View):
        return [_item_to_json(child) for child in view.children], None
    if isinstance(view, discord.ui.LayoutView):
        return None, [_v2_item_to_json(child) for child in view.children]
    return None, None


def _emoji(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, discord.PartialEmoji) and not value.is_unicode_emoji():
        return f":{value.name}:"
    return str(value)


def _item_to_json(item: discord.ui.Item) -> dict:
    kind = _KINDS.get(type(item).__name__)
    row = getattr(item, "row", None) or 0
    if kind == "button":
        return {
            "kind": "button",
            "row": row,
            "custom_id": item.custom_id,
            "label": _label_of(item),
            "style": int(item.style),
            "disabled": item.disabled,
            "emoji": _emoji(item.emoji),
            "url": item.url,
        }
    if kind in ("select", "role_select", "user_select", "channel_select", "mentionable_select"):
        return {
            "kind": kind,
            "row": row,
            "custom_id": item.custom_id,
            "placeholder": item.placeholder,
            "min_values": item.min_values,
            "max_values": item.max_values,
            "disabled": item.disabled,
            "options": [
                {
                    "label": o.label,
                    "value": o.value,
                    "description": o.description,
                    "emoji": _emoji(o.emoji),
                }
                for o in getattr(item, "options", [])
            ],
        }
    if kind == "text_input":
        return {
            "kind": "text_input",
            "row": row,
            "custom_id": item.custom_id,
            "label": _label_of(item),
            "style": int(item.style),
            "placeholder": item.placeholder,
            "value": getattr(item, "default", None),
            "required": item.required,
            "min_length": item.min_length,
            "max_length": item.max_length,
        }
    return {"kind": "unknown", "label": _label_of(item) or type(item).__name__}


def _label_of(item: discord.ui.Item) -> str | None:
    """Read .label without discord.py's TextInput deprecation noise."""
    underlying = getattr(item, "_underlying", None)
    if underlying is not None and hasattr(underlying, "label"):
        return underlying.label
    return getattr(item, "label", None)


_IMAGE_MIMES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp"}
_MAX_INLINE_FILE = 262_144  # 256 KiB: cap what we inline as a data URI
_MAX_MESSAGES = 2_000
_MAX_EVENTS = 1_000


def _file_info(f) -> dict:
    """Serialize a discord.File: name plus an inline data URI for small images
    (so the UI can show real thumbnails). The stream is restored after reading."""
    name = getattr(f, "filename", "file")
    info = {"name": name}
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _IMAGE_MIMES:
        fp = getattr(f, "fp", None)
        if fp is not None and hasattr(fp, "read") and hasattr(fp, "seek"):
            try:
                pos = fp.tell()
            except Exception:  # noqa: BLE001 - exotic stream; reading is best-effort
                pos = None
            try:
                data = fp.read()
                if pos is not None:
                    fp.seek(pos)
                if len(data) <= _MAX_INLINE_FILE:
                    uri = f"data:{_IMAGE_MIMES[ext]};base64,{base64.b64encode(data).decode()}"
                    info["data_uri"] = uri
            except Exception as err:  # noqa: BLE001 - unreadable stream: chip-only fallback
                log.debug("could not inline %s: %s", name, err)
    return info


def _v2_item_to_json(item) -> dict:
    """Recursive serializer for Components V2 items (LayoutView trees).

    Interactive leaves (buttons/selects) reuse the classic item dicts so the
    UI's click paths keep working; layout nodes get a "v2" discriminator.
    """
    name = type(item).__name__
    if name == "Container":
        return {
            "v2": "container",
            "accent_color": item.accent_color,
            "spoiler": bool(item.spoiler),
            "children": [_v2_item_to_json(c) for c in item.children],
        }
    if name == "Section":
        acc = item.accessory
        return {
            "v2": "section",
            "children": [_v2_item_to_json(c) for c in item.children],
            "accessory": _v2_item_to_json(acc) if acc is not None else None,
        }
    if name == "TextDisplay":
        return {"v2": "text", "content": item.content or ""}
    if name == "MediaGallery":
        return {
            "v2": "gallery",
            "items": [
                {
                    "url": g.media.url,
                    "description": g.description,
                    "spoiler": bool(g.spoiler),
                }
                for g in getattr(item, "items", [])
            ],
        }
    if name == "Thumbnail":
        return {
            "v2": "thumbnail",
            "url": item.media.url,
            "description": item.description,
            "spoiler": bool(item.spoiler),
        }
    if name == "File":
        return {"v2": "file", "url": item.media.url, "spoiler": bool(item.spoiler)}
    if name == "Separator":
        visible = getattr(item, "visible", None)
        spacing = getattr(item, "spacing", None)
        spacing_val = getattr(spacing, "value", spacing) if spacing is not None else None  # enum -> int
        return {"v2": "separator", "visible": True if visible is None else bool(visible),
                "spacing": int(spacing_val) if spacing_val is not None else None}
    if name == "ActionRow":
        return {"v2": "actionrow", "children": [_v2_item_to_json(c) for c in item.children]}
    kind = _KINDS.get(name)
    if kind is not None:  # buttons/selects — including Section accessory buttons
        return _item_to_json(item)
    return {"v2": "unknown", "label": _label_of(item) or name}


def _embeds_to_json(kwargs: dict) -> list[dict]:
    if kwargs.get("embeds") is not None:
        raw = list(kwargs["embeds"])
    elif kwargs.get("embed") is not None:
        raw = [kwargs["embed"]]
    else:
        return []
    out = []
    for e in raw:
        data = e.to_dict() if isinstance(e, discord.Embed) else {"title": str(e), "description": ""}
        data.setdefault("type", "rich")
        for field in data.get("fields", []):
            field.setdefault("inline", False)
        out.append(data)
    return out


# --------------------------------------------------------------- commands

_TYPE_NAMES = {
    discord.AppCommandOptionType.string: "string",
    discord.AppCommandOptionType.integer: "integer",
    discord.AppCommandOptionType.number: "number",
    discord.AppCommandOptionType.boolean: "boolean",
    discord.AppCommandOptionType.user: "user",
    discord.AppCommandOptionType.channel: "channel",
    discord.AppCommandOptionType.role: "role",
    discord.AppCommandOptionType.mentionable: "mentionable",
    discord.AppCommandOptionType.attachment: "attachment",
}


def _serialize_command(cmd: app_commands.Command) -> dict:
    params = []
    for p in cmd.parameters:
        desc = p.description
        if not isinstance(desc, str) or not desc:  # MISSING sentinel or empty
            desc = p.display_name
        params.append(
            {
                "name": p.display_name,
                "description": desc,
                "required": p.required,
                "type": _TYPE_NAMES.get(p.type, "string"),
                "choices": [(c.name, c.value) for c in (p.choices or [])],
            }
        )
    return {"name": cmd.name, "description": cmd.description or "", "params": params}


def _collect_commands(session: Session, env: dict) -> None:
    """Gather @app_commands.command() objects defined in the script namespace."""
    session.reset_commands()
    seen = set()
    for value in list(env.values()):
        if isinstance(value, app_commands.Command) and value.name not in seen:
            seen.add(value.name)
            session.cmd_objects[value.name] = value
            session.commands[value.name] = _serialize_command(value)
    if session.commands:
        session.log("⌨️", f"registered slash commands: {', /'.join(sorted(session.commands))}")


def _coerce_arg(session: Session, param: dict, raw):
    """Turn a UI string into the value discord.py would deliver to the callback."""
    if raw is None or raw == "":
        return None
    kind = param["type"]
    if param["choices"]:
        for name, value in param["choices"]:
            if str(value) == str(raw):
                return app_commands.Choice(name=name, value=value)
        return raw
    if kind == "string":
        return str(raw)
    if kind in ("integer", "number"):
        return int(raw) if kind == "integer" else float(raw)
    if kind == "boolean":
        return str(raw).lower() in ("1", "true", "yes", "on")
    if kind == "user":
        return session.guild.get_member(int(raw)) or raw
    if kind == "role":
        return session.guild.get_role(int(raw)) or raw
    if kind == "channel":
        return session.channels.get(str(raw)) or raw
    return raw


async def _do_command(session: Session, name: str, args: dict) -> None:
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    cmd = session.cmd_objects.get(name)
    if cmd is None:
        session.log("⚠️", f"/{name} is not defined by the current script", "warn")
        return
    spec = session.commands.get(name) or {"params": []}
    params = {p["name"]: p for p in spec["params"]}
    kwargs = {}
    for pname, raw in args.items():
        if pname in params and raw is not None and raw != "":
            kwargs[pname] = _coerce_arg(session, params[pname], raw)
    missing = [p["name"] for p in spec["params"] if p["required"] and p["name"] not in kwargs]
    if missing:
        session.log("⚠️", f"/{name} is missing required argument(s): {', '.join(missing)}", "warn")
        return
    session.pending_command = name
    try:
        interaction = session.build_interaction(
            source_message_id=None,
            interaction_type=discord.InteractionType.application_command,
        )
        interaction.channel = session.channel
        interaction.channel_id = session.channel.id
        interaction.command = _CommandRef(name)
        result = cmd._callback(cmd, interaction, **kwargs) if inspect.ismethod(cmd._callback) else cmd._callback(interaction, **kwargs)
        if inspect.isawaitable(result):
            await result
        if not interaction.is_done():
            session.log("⚠️", "that interaction was never answered — real Discord shows "
                        "'This interaction failed'", "warn")
    finally:
        session.pending_command = None


# --------------------------------------------------------------- session


class SessionRunner:
    """A dedicated thread + event loop per session.

    User code lives here, so a wedged script (infinite CPU loop) can never
    freeze the web UI or other sessions. The server submits work with a
    timeout; a stuck script is reported and the session can be restarted.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.gate = asyncio.Lock()  # one action at a time per session
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def shutdown(self) -> None:
        """Cancel loop work before stopping it, avoiding leaked runner threads."""
        if not self.loop.is_running():
            return

        async def cancel_tasks() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        with contextlib.suppress(RuntimeError, TimeoutError):
            future = asyncio.run_coroutine_threadsafe(cancel_tasks(), self.loop)
            future.result(timeout=1.0)
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=1.0)

    async def run(self, factory, timeout: float):
        """Run factory() (a coroutine factory) on the session loop with a deadline."""
        cf = asyncio.run_coroutine_threadsafe(factory(), self.loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(cf), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            cf.cancel()
            raise ScriptStuck(
                "The script did not respond in time (likely an infinite loop or a "
                "stuck await). Hit Restart to boot a fresh one."
            ) from None


class Session:
    """One playground session: captured timeline + the script's live runtime."""

    def __init__(self, sid: str):
        self.sid = sid
        self.guild = MockGuild(self)
        self.channels: dict[str, MockChannel] = {}
        self.next_channel_id = 1
        self.revision = 0  # monotonic state key for cheap browser render checks
        self.channel = self.make_channel("playground")
        self.client = MockClient(self)
        self.user_id = USER_ID
        self.user_name = USER_NAME
        self.events: list[dict] = []
        self.messages: dict[str, dict] = {}
        self.order: list[str] = []
        self.modals: list[dict] = []
        self.next_index = 1
        self.next_modal_id = 1
        self.last_run: dict | None = None
        self.runner = SessionRunner()
        self.env: dict | None = None
        self.main_task: asyncio.Task | None = None
        self.commands: dict[str, dict] = {}  # serialized, for the UI
        self.cmd_objects: dict[str, app_commands.Command] = {}
        self.pending_command: str | None = None  # chip attached to messages sent during dispatch

    # -- timeline ----------------------------------------------------------

    def _touch(self) -> None:
        self.revision += 1

    def log(self, icon: str, text: str, cls: str | None = None) -> None:
        event = {"icon": icon, "text": text}
        if cls:
            event["cls"] = cls
        self.events.append(event)
        self._touch()
        if len(self.events) > _MAX_EVENTS:
            del self.events[:-_MAX_EVENTS]

    def add_message(self, content=None, **kwargs) -> dict:
        mid = f"m{self.next_index}"
        self.next_index += 1
        files = kwargs.get("files") or ([kwargs["file"]] if kwargs.get("file") else [])
        author = kwargs.get("author") or self.guild.me
        author_obj = author if isinstance(author, MockMember) else None
        command_chip = self.pending_command if author.bot else None
        channel_key = str(kwargs.get("channel_id") or self.channel.id)
        classic, v2 = _stash_view(kwargs.get("view"))
        msg = {
            "id": mid,
            "index": len(self.order) + 1,
            "author": {"id": author.id, "name": author.name, "bot": author.bot},
            "channel": channel_key,
            "command": command_chip,
            "content": content if content is not None else "",
            "embeds": _embeds_to_json(kwargs),
            "components": classic,
            "v2": v2,
            "ephemeral": bool(kwargs.get("ephemeral")),
            "files": [_file_info(f) for f in files],
            "revision": 0,
            "deleted": False,
            "timestamp": _now(),
        }
        msg["author_obj"] = author_obj  # not serialized; used for MockMessage.author
        self.messages[mid] = msg
        self.order.append(mid)
        self._touch()
        if len(self.order) > _MAX_MESSAGES:
            old_id = self.order.pop(0)
            self.messages.pop(old_id, None)
        return msg

    def update_message(self, message_id: str | None, content=None, **kwargs) -> None:
        msg = self.messages.get(message_id or "")
        if msg is None or msg["deleted"]:
            self.log("⚠️", "edit_message targeted a missing message; ignored", "warn")
            return
        if content is not None and not isinstance(content, str):
            content = str(content)
        msg["content"] = content or ""
        embeds = _embeds_to_json(kwargs)
        if kwargs.get("embed") is not None or kwargs.get("embeds") is not None:
            msg["embeds"] = embeds
        if "view" in kwargs:
            classic, v2 = _stash_view(kwargs.get("view"))
            msg["components"], msg["v2"] = classic, v2
        msg["revision"] += 1
        self._touch()

    def delete_message(self, message_id: str | None) -> None:
        msg = self.messages.get(message_id or "")
        if msg is None or msg["deleted"]:
            return
        msg["deleted"] = True
        if message_id in self.order:
            self.order.remove(message_id)
        self._touch()
        self.log("🗑️", f"message #{msg['index']} deleted")

    def open_modal(self, modal: discord.ui.Modal, source_message_id: str | None) -> None:
        title = modal.title if isinstance(modal.title, str) else "Modal"
        self.modals.append(
            {
                "id": f"mo{self.next_modal_id}",
                "title": title,
                "items": [_item_to_json(child) for child in modal.children],
                "source": source_message_id,
            }
        )
        self.next_modal_id += 1
        self._touch()
        self.log("📋", f"modal opened: {title!r}")

    def clear_timeline(self) -> None:
        self.messages.clear()
        self.order.clear()
        self.modals.clear()
        self.next_index = 1
        self.next_modal_id = 1
        self._touch()

    def reset_commands(self) -> None:
        self.commands.clear()
        self.cmd_objects.clear()

    def make_channel(self, name: str) -> MockChannel:
        """Create a text channel (Discord-style name normalization; dupes get -2, -3…)."""
        base = re.sub(r"[^a-z0-9-]", "-", name.strip().lower()).strip("-") or "channel"
        taken = {c.name for c in self.channels.values()}
        final, n = base, 1
        while final in taken:
            n += 1
            final = f"{base}-{n}"
        if not self.channels:
            cid = CHANNEL_ID  # the default #playground keeps its well-known id
        else:
            cid = GUILD_ID + 100 + self.next_channel_id
            self.next_channel_id += 1
        ch = MockChannel(self, cid, final)
        self.channels[str(cid)] = ch
        self._touch()
        return ch

    def delete_channel(self, channel_id) -> None:
        key = str(channel_id)
        ch = self.channels.get(key)
        if ch is None:
            return
        if len(self.channels) <= 1:
            self.log("⚠️", "can't delete the last remaining channel", "warn")
            return
        del self.channels[key]
        for mid in [m for m, msg in self.messages.items() if msg.get("channel") == key]:
            msg = self.messages.pop(mid)
            msg["deleted"] = True
            if mid in self.order:
                self.order.remove(mid)
        self.log("🗑️", f"channel #{ch.name} deleted")

    def reset_channels(self) -> None:
        """Back to just #playground — a fresh Run bootstraps its own world."""
        self.channels.clear()
        self.next_channel_id = 1
        self.channel = self.make_channel("playground")

    # -- runtime -----------------------------------------------------------

    @property
    def active_user(self) -> MockMember:
        return self.guild.get_member(self.user_id) or self.guild.get_member(USER_ID)

    def set_user(self, user_id: int) -> None:
        if self.guild.get_member(user_id) is None or user_id == BOT_ID:
            raise ValueError("unknown simulated user")
        self.user_id = user_id
        self.user_name = self.active_user.name
        self._touch()

    def build_interaction(self, source_message_id: str | None = None,
                          custom_id: str | None = None, values: list | None = None,
                          interaction_type: discord.InteractionType = discord.InteractionType.application_command) -> MockInteraction:
        return MockInteraction(self, source_message_id, custom_id, values, interaction_type)

    def restart(self) -> None:
        """Boot a fresh runtime (fresh thread + loop); the timeline is cleared."""
        self.runner.shutdown()
        self.runner = SessionRunner()
        self.env = None
        self.main_task = None
        self.reset_commands()
        self.reset_channels()
        self.clear_timeline()
        self.events.clear()
        self.last_run = None

    def close(self) -> None:
        self.runner.shutdown()


# --------------------------------------------------------------- script running


def _build_env(session: Session) -> dict:
    def playground_print(*args, sep: str = " ", **_ignored) -> None:
        session.log("🖨️", sep.join(str(a) for a in args))

    async def playground_send(content=None, **kwargs):
        return await session.channel.send(content, **kwargs)

    return {
        "discord": discord,
        "Session": session,
        "client": session.client,
        "print": playground_print,
        "send": playground_send,
        "__name__": "playground",
    }


def _flush_stdout(buffer: io.StringIO, session: Session) -> None:
    text = buffer.getvalue().strip()
    if text:
        session.log("🖨️", text)


def _script_trace(deadline: float):
    """Interrupt CPU-bound user code; asyncio cancellation cannot stop a busy loop."""
    def trace(frame, event, _arg):
        if event == "line" and frame.f_code.co_filename == "<playground>" \
                and time.monotonic() >= deadline:
            raise ScriptStuck("The script exceeded its execution deadline; hit Restart to recover.")
        return trace

    return trace


def _call(handler, *args):
    """Call a sync or async handler with only as many args as it accepts."""
    params = inspect.signature(handler).parameters
    args = args[: len(params)] if not any(
        p.kind is inspect.Parameter.VAR_POSITIONAL for p in params.values()
    ) else args
    result = handler(*args)
    if inspect.isawaitable(result):
        return result
    return None


async def _stop_main(session: Session) -> None:
    task = session.main_task
    session.main_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _do_run(session: Session, code: str) -> None:
    await _stop_main(session)
    session.clear_timeline()
    session.reset_channels()  # a fresh Run bootstraps its own channels
    env = _build_env(session)
    buffer = io.StringIO()
    started = time.perf_counter()
    previous_trace = sys.gettrace()
    sys.settrace(_script_trace(getattr(session, "user_deadline", time.monotonic() + 20)))
    try:
        with contextlib.redirect_stdout(buffer):
            exec(compile(code, "<playground>", "exec"), env)  # noqa: S102 - the whole point
    except BaseException:  # noqa: BLE001 - user code may raise anything, incl. SystemExit
        _flush_stdout(buffer, session)
        tb = traceback.format_exc(limit=6)
        session.log("💥", tb, "error")
        session.last_run = {"ok": False, "error": tb, "ms": (time.perf_counter() - started) * 1000}
        return
    finally:
        sys.settrace(previous_trace)
    _flush_stdout(buffer, session)

    session.env = env
    _collect_commands(session, env)
    main_fn = env.get("main")
    if main_fn is None:
        session.last_run = {"ok": True, "error": None,
                            "ms": (time.perf_counter() - started) * 1000}
        session.log("ℹ️", "Module ran; define `async def main():` to boot startup logic.")
    else:
        # Wait until main() has actually started (or fully finished, if it never
        # suspends) so Run's return is deterministic and output is visible.
        began = asyncio.Event()
        session.main_task = session.runner.loop.create_task(
            _run_main(main_fn, session, began)
        )
        await began.wait()
        # A main() that crashed before its first suspension already set failure.
        if not (session.last_run and not session.last_run["ok"]):
            session.last_run = {"ok": True, "error": None, "ms": (time.perf_counter() - started) * 1000}
            session.log("✅", "Run complete.")


async def _run_main(main_fn, session: Session, started: asyncio.Event) -> None:
    started.set()
    try:
        result = main_fn()
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        session.log("🛑", "main() was cancelled.")
    except BaseException:  # noqa: BLE001 - background task must swallow-and-report
        tb = "".join(traceback.format_exception(*sys.exc_info()))
        session.log("💥", tb, "error")
        session.last_run = {"ok": False, "error": tb, "ms": 0.0}


async def _do_click(session: Session, message_id: str, custom_id: str, values: list) -> None:
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    handler = env.get("on_click")
    if not callable(handler):
        session.log("⚠️", f"clicked {custom_id!r} but no `on_click` handler is defined", "warn")
        return
    interaction = session.build_interaction(
        message_id, custom_id, values, discord.InteractionType.component
    )
    result = _call(handler, interaction, custom_id, values)
    if inspect.isawaitable(result):
        await result
    if not interaction.is_done():
        session.log(
            "⚠️",
            "that interaction was never answered — real Discord shows "
            "'This interaction failed'",
            "warn",
        )


async def _do_submit(session: Session, modal_id: str, values: dict) -> None:
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    handler = env.get("on_submit")
    if not callable(handler):
        session.log("⚠️", f"modal {modal_id} submitted but no `on_submit` handler is defined", "warn")
        return
    modal = next((m for m in session.modals if m["id"] == modal_id), None)
    if modal is not None:
        session.modals.remove(modal)
    interaction = session.build_interaction(
        modal["source"] if modal else None,
        interaction_type=discord.InteractionType.modal_submit,
    )
    result = _call(handler, interaction, values, modal_id)
    if inspect.isawaitable(result):
        await result
    if not interaction.is_done():
        session.log(
            "⚠️",
            "that interaction was never answered — real Discord shows "
            "'This interaction failed'",
            "warn",
        )


async def _do_message(session: Session, content: str, channel_id=None) -> None:
    """The composer sent a chat message; hand it to on_message if defined."""
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    channel = session.channels.get(str(channel_id), session.channel)
    if not channel.permissions_for(session.active_user).send_messages:
        session.log("🚫", "message blocked: missing send_messages permission", "warn")
        return
    msg = session.add_message(channel_id=channel_id, content=content,
                              author=session.active_user)
    handle = MockMessage(session, msg["id"], msg.get("author_obj"))
    handler = env.get("on_message")
    if not callable(handler):
        session.log("⚠️", "message received but no `on_message` handler is defined", "warn")
        return
    result = _call(handler, handle)
    if inspect.isawaitable(result):
        await result


async def _gated(session: Session, coro_fn) -> None:
    async with session.runner.gate:
        await coro_fn()


async def run_script(session: Session, code: str, timeout: float = 20.0) -> dict:
    if len(code) > 1_000_000:
        message = "Script is too large (maximum 1 MB)."
        session.log("⚠️", message, "error")
        session.last_run = {"ok": False, "error": message, "ms": 0.0}
        return {"ok": False, "ms": 0.0}
    started = time.perf_counter()
    session.user_deadline = time.monotonic() + timeout
    try:
        await session.runner.run(lambda: _gated(session, lambda: _do_run(session, code)), timeout)
    except ScriptStuck as exc:
        session.log("🛑", str(exc), "error")
        session.last_run = {"ok": False, "error": str(exc), "ms": (time.perf_counter() - started) * 1000}
    return {"ok": bool(session.last_run and session.last_run["ok"]),
            "ms": round(session.last_run["ms"], 1) if session.last_run else 0.0}


async def dispatch_click(session: Session, message_id: str, custom_id: str,
                         values: list, timeout: float = 10.0) -> dict:
    try:
        await session.runner.run(
            lambda: _gated(session, lambda: _do_click(session, message_id, custom_id, values)), timeout
        )
    except ScriptStuck as exc:
        session.log("🛑", str(exc), "error")
    return state(session)


async def dispatch_submit(session: Session, modal_id: str, values: dict,
                          timeout: float = 10.0) -> dict:
    try:
        await session.runner.run(
            lambda: _gated(session, lambda: _do_submit(session, modal_id, values)), timeout
        )
    except ScriptStuck as exc:
        session.log("🛑", str(exc), "error")
    return state(session)


async def dispatch_message(session: Session, content: str, channel_id=None,
                           timeout: float = 10.0) -> dict:
    try:
        await session.runner.run(
            lambda: _gated(session, lambda: _do_message(session, content, channel_id)), timeout
        )
    except ScriptStuck as exc:
        session.log("🛑", str(exc), "error")
    return state(session)


async def dispatch_command(session: Session, name: str, args: dict,
                           timeout: float = 10.0) -> dict:
    try:
        await session.runner.run(
            lambda: _gated(session, lambda: _do_command(session, name, args)), timeout
        )
    except ScriptStuck as exc:
        session.log("🛑", str(exc), "error")
    return state(session)


# --------------------------------------------------------------- state


def _members_json(session: Session) -> dict:
    names = {str(m.id): m.name for m in session.guild.members}
    names.update({f"&{r.id}": r.name for r in session.guild.roles})
    names.update({f"#{c.id}": c.name for c in session.channels.values()})
    return names


def state(session: Session) -> dict:
    msgs = []
    for mid in session.order:
        msg = {k: v for k, v in session.messages[mid].items() if k != "author_obj"}
        msgs.append(msg)
    return {
        "ok": True,
        "sid": session.sid,
        "user": {"id": str(session.user_id), "name": session.user_name},
        "users": [{"id": str(member.id), "name": member.name}
                  for member in session.guild.members if not member.bot],
        "guild": {"id": session.guild.id, "name": session.guild.name},
        "channel": {"id": str(session.channel.id), "name": session.channel.name},
        "revision": session.revision,
        # ids as strings: 18-digit snowflakes lose precision as JS numbers
        "channels": [{"id": str(c.id), "name": c.name} for c in session.channels.values()],
        "bot": {"id": session.guild.me.id, "name": session.guild.me.name},
        "members": _members_json(session),
        "messages": msgs,
        "modals": session.modals,
        "commands": session.commands,
        "events": session.events[-300:],
        "last_run": session.last_run,
        "running": session.env is not None,
    }
