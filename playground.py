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
import contextvars
import importlib.abc
import importlib.util
import inspect
import io
import json
import logging
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import discord
from discord import app_commands

log = logging.getLogger(__name__)
_ACTIVE_EVENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("playground_active_event", default=None)
_CURRENT_SESSION: contextvars.ContextVar[Session | None] = contextvars.ContextVar("playground_current_session", default=None)

USER_ID = 123456789012345678
USER_NAME = "You"
BOT_ID = 987654321098765432
GUILD_ID = 900000000000000001
CHANNEL_ID = 900000000000000002
MEMBER_IDS = {"Alice": 111111111111111111, "Bob": 222222222222222222, "Carol": 333333333333333333}
CUSTOM_USER_ID = 444444444444444444
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
        self.custom = False
        self.bio = ""
        self.avatar_url = None
        self.banner_url = None
        self.accent_color = None
        self.guild = guild
        self.roles = [guild.roles[0]]
        self.joined_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.status = discord.Status.online
        self.avatar = None
        self.permission_override: discord.Permissions | None = None
        self.banned = False
        self.timeout_until: datetime | None = None

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
                                          embed_links=True, manage_channels=True,
                                          add_reactions=True)
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


_VOICE_ACTIONS = {
    "join": ("🔊 joined the voice channel", "join"),
    "leave": ("👋 left the voice channel", None),
    "mute": ("🔇 muted (simulated)", None),
    "unmute": ("🎙 unmuted (simulated)", None),
    "deafen": ("🔇 deafened (simulated)", None),
    "undeafen": ("🎧 undeafened (simulated)", None),
}


class _ForbiddenResponse(_FakeResponse):
    status = 403
    reason = "Forbidden"


class _BadRequestResponse(_FakeResponse):
    status = 400
    reason = "Bad Request"

    def json(self):
        return {"message": "Invalid Form Body", "code": 50035}


_FORBIDDEN = _ForbiddenResponse()
_BAD_REQUEST = _BadRequestResponse()


class _MockHTTPException(discord.HTTPException):
    """Real discord.HTTPException (catchable by bot code) with a mock response."""

    def __init__(self, detail: str):
        # discord.py renders str(error) from the dict's "message" value and
        # reads .code from it, mirroring the real API's error JSON where the
        # "errors" details are flattened into "In <field>: <why>" lines.
        super().__init__(_BAD_REQUEST, {"message": "Invalid Form Body\n" + detail, "code": 50035})


def _invalid_form_body(errors: list[str]) -> discord.HTTPException:
    """The 400 the real API returns when a payload breaks its limits (code 50035)."""
    return _MockHTTPException("In " + "\nIn ".join(errors))


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

    @property
    def attachments(self) -> list[dict]:
        """Uploaded files on this message (name + optional inline data_uri)."""
        return [dict(f) for f in (self._session.messages.get(self.id) or {}).get("files", [])]

    async def edit(self, **kwargs):
        self._session.update_message(self.id, **kwargs)
        return self

    async def delete(self, delay: float | None = None) -> None:
        if delay:
            await asyncio.sleep(delay)
        allowed, reason = self.channel.permission_check(self.guild.me, "manage_messages")
        if not allowed:
            self._session.log("🚫", f"message.delete blocked: missing manage_messages permission ({reason})", "warn",
                              kind="action", details={"operation": "message.delete", "message_id": self.id,
                                                       "status": "denied", "permission": "manage_messages",
                                                       "reason": reason})
            raise discord.Forbidden(_FORBIDDEN, f"missing manage_messages permission ({reason})")
        self._session.delete_message(self.id)

    async def reply(self, content=None, **kwargs) -> MockMessage:
        return await self.channel.send(content, **kwargs)

    async def add_reaction(self, emoji) -> None:
        self._session.toggle_reaction(self.id, str(emoji), user_id=self._session.guild.me.id, actor="bot")

    async def remove_reaction(self, emoji, member=None) -> None:
        stored = self._session.messages.get(self.id) or {}
        user_id = member.id if member is not None else self._session.guild.me.id
        entry = next((r for r in stored.get("reactions", []) if r["emoji"] == str(emoji)), None)
        if entry is None or str(user_id) not in entry["users"]:
            self._session.log("⚠️", "remove_reaction targeted a reaction that is not present", "warn", kind="action",
                              details={"operation": "message.remove_reaction", "message_id": self.id,
                                       "emoji": str(emoji), "status": "missing_reaction"})
            return
        self._session.toggle_reaction(self.id, str(emoji), user_id=user_id, actor="bot")

    async def clear_reactions(self) -> None:
        stored = self._session.messages.get(self.id) or {}
        for reaction in list(stored.get("reactions", [])):
            for user_id in list(reaction["users"]):
                self._session.toggle_reaction(self.id, reaction["emoji"], user_id=user_id, actor="bot")

    async def pin(self, **kwargs) -> None:
        self._session.log("📌", f"message #{self.id} pinned", kind="action",
                          details={"operation": "message.pin", "message_id": self.id})

    def __str__(self) -> str:
        return self.content or "(embed)"


class MockChannel:
    def __init__(self, session: Session, channel_id: int, name: str):
        self._session = session
        self.id = channel_id
        self.name = name
        self.topic: str | None = None
        self.guild = session.guild
        self.mention = f"<#{channel_id}>"
        self.overwrites: dict[int, discord.PermissionOverwrite] = {}

    def __str__(self) -> str:
        return f"#{self.name}"

    def _resolve_permissions(self, member: MockMember, permission: str | None = None) -> tuple[discord.Permissions, str]:
        permissions = member.guild_permissions
        if permissions.administrator:
            return discord.Permissions.all(), "administrator bypass"
        bit = discord.Permissions(**{permission: True}).value if permission else 0
        reason = "resolved permissions"

        def apply(overwrite: discord.PermissionOverwrite, label: str) -> None:
            nonlocal reason
            allow, deny = overwrite.pair()
            permissions.handle_overwrite(allow.value, deny.value)
            if bit and deny.value & bit and not allow.value & bit:
                reason = label

        apply(self.overwrites.get(self.guild.id, discord.PermissionOverwrite()), "@everyone overwrite")
        role_allow = role_deny = 0
        for role in member.roles:
            if role.id == self.guild.id:
                continue
            allow, deny = self.overwrites.get(role.id, discord.PermissionOverwrite()).pair()
            role_allow |= allow.value
            role_deny |= deny.value
        permissions.handle_overwrite(role_allow, role_deny)
        if bit and role_deny & bit and not role_allow & bit:
            reason = "role overwrite"
        apply(self.overwrites.get(member.id, discord.PermissionOverwrite()), "member overwrite")
        return permissions, reason

    def permissions_for(self, member: MockMember) -> discord.Permissions:
        return self._resolve_permissions(member)[0]

    def permission_check(self, member: MockMember, permission: str) -> tuple[bool, str]:
        resolved, reason = self._resolve_permissions(member, permission)
        return bool(getattr(resolved, permission)), reason

    def require_permission(self, member: MockMember, permission: str) -> None:
        allowed, reason = self.permission_check(member, permission)
        if not allowed:
            raise discord.Forbidden(_FORBIDDEN, f"missing {permission} permission ({reason})")

    async def send(self, content=None, **kwargs) -> MockMessage:
        if self._session.channels.get(str(self.id)) is not self:
            self._session.log("⚠️", f"channel.send blocked: #{self.name} was deleted", "warn",
                              kind="action", details={"operation": "channel.send", "channel": self.name,
                                                       "status": "missing_channel"})
            raise discord.NotFound(_NOT_FOUND, "channel was deleted")
        author = kwargs.get("author") or self.guild.me
        allowed, reason = self.permission_check(author, "send_messages")
        if not allowed:
            self._session.log("🚫", f"channel.send blocked: missing send_messages permission ({reason})", "warn",
                              kind="action", details={"operation": "channel.send", "channel": self.name,
                                                       "status": "denied", "permission": "send_messages",
                                                       "reason": reason, "actor": author.name})
            raise discord.Forbidden(_FORBIDDEN, f"missing send_messages permission ({reason})")
        msg = self._session.add_message(channel_id=self.id, content=content, **kwargs)
        self._session.log("↗️", f"channel.send → message #{msg['index']}", kind="action",
                          details={"operation": "channel.send", "channel": self.name,
                                   "actor": author.name, "message_id": msg["id"], "content": content or "",
                                   "status": "success"})
        return MockMessage(self._session, msg["id"], msg.get("author_obj"))

    async def delete(self) -> None:
        allowed, reason = self.permission_check(self.guild.me, "manage_channels")
        if not allowed:
            self._session.log("🚫", f"channel.delete blocked: missing manage_channels permission ({reason})", "warn",
                              kind="action", details={"operation": "channel.delete", "channel": self.name,
                                                       "status": "denied", "permission": "manage_channels",
                                                       "reason": reason})
            raise discord.Forbidden(_FORBIDDEN, f"missing manage_channels permission ({reason})")
        self._session.delete_channel(self.id)

    async def fetch_message(self, message_id) -> MockMessage:
        if str(message_id) not in self._session.messages:
            self._session.log("⚠️", f"message.fetch blocked: #{message_id} was not found", "warn",
                              kind="action", details={"operation": "message.fetch", "message_id": str(message_id),
                                                       "status": "missing_message"})
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
    """Just enough of discord.Client for scripts to introspect.

    Cogs follow the discord.py convention: ``await bot.add_cog(MyCog(bot))``
    registers the instance, and its app commands surface in the composer via
    _collect_commands. Listeners are *stored* here (Slice 1); dispatch is not
    wired yet.
    """

    def __init__(self, session: Session):
        self._session = session
        self.cogs: dict[str, object] = {}
        self._listeners: dict[str, list] = {}

    def _record_cog_listeners(self, cog) -> None:
        for event_name, attr in getattr(type(cog), "__cog_listeners__", ()):
            self._listeners.setdefault(event_name, []).append(getattr(cog, attr))

    async def add_cog(self, cog) -> None:
        name = type(cog).__name__
        if name in self.cogs:
            raise RuntimeError(f"Cog named {name!r} is already registered.")
        self.cogs[name] = cog
        self._record_cog_listeners(cog)
        self._session.log("🧩", f"cog loaded: {name}", details={"cog": name})

    def get_cog(self, name: str):
        return self.cogs.get(name)

    async def remove_cog(self, name: str) -> None:
        cog = self.cogs.pop(name, None)
        if cog is not None:
            for event_name, attr in getattr(type(cog), "__cog_listeners__", ()):
                self._listeners.get(event_name, []).remove(getattr(cog, attr))
            self._session.log("🧩", f"cog unloaded: {name}", details={"cog": name})

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
        self._ephemeral_message_id: str | None = None

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
        if kwargs.get("ephemeral"):
            kwargs["ephemeral_user_id"] = self._interaction.user.id
        else:
            kwargs.pop("ephemeral_user_id", None)
        msg = await self._interaction.channel.send(content, **kwargs)
        self._done = True
        self._interaction._last = msg.id
        self._interaction._original_response_id = msg.id
        if msg._session.messages[msg.id].get("ephemeral"):
            self._ephemeral_message_id = msg.id
        return msg

    async def defer(self, thinking: bool = False, ephemeral: bool = False, **kwargs) -> None:
        interaction = self._interaction
        self._done = True
        interaction._ephemeral_followups = ephemeral
        if interaction.message is not None and not thinking:
            interaction._original_response_id = interaction.message.id
        else:
            placeholder = await interaction.channel.send(
                "Thinking…", ephemeral=ephemeral, author=interaction._session.guild.me,
                **({"ephemeral_user_id": interaction.user.id} if ephemeral else {}),
            )
            interaction._original_response_id = placeholder.id
            interaction._last = placeholder.id
        interaction._session.log("💭", "interaction deferred (thinking)" if thinking else "interaction deferred")

    async def edit_message(self, content=None, **kwargs) -> MockMessage:
        self._done = True
        session = self._interaction._session
        target = self._interaction.message.id if self._interaction.message else self._interaction._last
        session.update_message(target, content=content, **kwargs)
        self._interaction._original_response_id = target
        return MockMessage(session, target)

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._done = True
        self._interaction._session.open_modal(
            modal, self._interaction._last, channel_id=self._interaction.channel.id,
            user_id=self._interaction.user.id,
        )

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
        if kwargs.get("ephemeral"):
            kwargs["ephemeral_user_id"] = self._interaction.user.id
        else:
            kwargs.pop("ephemeral_user_id", None)
        msg = await self._interaction.channel.send(content, **kwargs)
        if msg._session.messages[msg.id].get("ephemeral"):
            self._interaction._ephemeral_message_ids.add(msg.id)
        self._interaction._last = msg.id
        return msg

    async def edit_message(self, content=None, **kwargs) -> MockMessage:
        session = self._interaction._session
        target = self._interaction._last
        message = session.messages.get(target or "")
        if message and message.get("ephemeral") and message.get("ephemeral_user_id") != str(self._interaction.user.id):
            raise RuntimeError("ephemeral message is only visible to its interaction user")
        session.update_message(target, content=content, **kwargs)
        return MockMessage(session, target)


class MockInteraction:
    """Stands in for discord.Interaction: responses are captured, not sent."""

    def __init__(self, session: Session, source_message_id: str | None = None,
                 custom_id: str | None = None, values: list | None = None,
                 interaction_type: discord.InteractionType = discord.InteractionType.application_command,
                 channel_id=None):
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
        # An interaction happens in the channel its message or invocation lives in.
        src = session.messages.get(source_message_id or "") or {}
        self.channel = session.channels.get(str(src.get("channel") or channel_id or ""), session.channel)
        self.channel_id = self.channel.id
        self.permissions = self.channel.permissions_for(self.user)
        self.app_permissions = self.channel.permissions_for(guild.me)
        self.command = _CommandRef("playground")
        self.data = {"custom_id": custom_id, "values": values or []} if custom_id else {}
        self.namespace = _Namespace()
        self.token = "mock-token"
        self.application_id = BOT_ID
        self.locale = "en-US"
        self._last = source_message_id
        self._original_response_id: str | None = None
        self._ephemeral_followups = False
        self._ephemeral_message_ids: set[str] = set()
        self.response = MockResponse(self)
        self.followup = MockFollowup(self)

    @property
    def type(self) -> discord.InteractionType:
        return self._interaction_type

    def is_done(self) -> bool:
        return self.response.is_done

    async def delete_original_response(self) -> None:
        target = self._original_response_id
        if target is None:
            raise RuntimeError("This interaction has no original response.")
        self._session.delete_message(target, actor_id=self.user.id)

    async def edit_original_response(self, content=None, **kwargs) -> MockMessage:
        target = self._original_response_id
        if target is None:
            raise RuntimeError("This interaction has no original response.")
        msg = self._session.messages.get(target)
        if msg and msg.get("ephemeral") and msg.get("ephemeral_user_id") != str(self.user.id):
            raise RuntimeError("ephemeral message is only visible to its interaction user")
        self._session.update_message(target, content=content, **kwargs)
        return MockMessage(self._session, target)

    async def original_response(self) -> MockMessage:
        target = self._original_response_id
        if target is None:
            raise RuntimeError("This interaction has no original response.")
        msg = self._session.messages.get(target)
        if msg and msg.get("ephemeral") and msg.get("ephemeral_user_id") != str(self.user.id):
            raise RuntimeError("ephemeral message is only visible to its interaction user")
        return MockMessage(self._session, target)


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
        classic, v2 = [_item_to_json(child) for child in view.children], None
    elif isinstance(view, discord.ui.LayoutView):
        classic, v2 = None, [_v2_item_to_json(child) for child in view.children]
    else:
        return None, None
    errors = _component_errors(classic, v2)
    if errors:
        raise _invalid_form_body(errors)
    return classic, v2


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
    if isinstance(f, dict):  # pre-serialized upload from the browser composer
        return f
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


# Discord payload limits (developers/resources/message#embed-object-embed-limits
# and developers/components/reference in discord-api-docs). The mock rejects
# payloads the real API would reject with 400 Invalid Form Body (code 50035).
_EMBED_LIMITS = {
    "title": 256,
    "description": 4096,
    "footer.text": 2048,
    "author.name": 256,
    "field.name": 256,
    "field.value": 1024,
}
_EMBED_TOTAL_LIMIT = 6000
_EMBED_FIELD_LIMIT = 25
_CONTENT_LIMIT = 2000
_BUTTON_LABEL_LIMIT = 80
_SELECT_PLACEHOLDER_LIMIT = 150
_SELECT_OPTION_LIMIT = 25
_V2_COMPONENT_LIMIT = 40


def _flatten_v2(items: list[dict]):
    """Every node of a serialized V2 tree (children + section accessories)."""
    for item in items:
        yield item
        yield from _flatten_v2(item.get("children") or [])
        yield from _flatten_v2([item["accessory"]]) if item.get("accessory") else ()


def _component_errors(classic: list[dict] | None, v2: list[dict] | None) -> list[str]:
    errors: list[str] = []
    buttons: dict[int, int] = {}
    for item in classic or []:
        if item.get("kind") == "button":
            if len(item.get("label") or "") > _BUTTON_LABEL_LIMIT:
                errors.append(f"components: button {item.get('custom_id')!r} label: Must be {_BUTTON_LABEL_LIMIT} or fewer in length")
            buttons[item.get("row", 0)] = buttons.get(item.get("row", 0), 0) + 1
        elif item.get("options") is not None:
            if len(item["options"]) > _SELECT_OPTION_LIMIT:
                errors.append(f"components: select {item.get('custom_id')!r} options: Must be {_SELECT_OPTION_LIMIT} or fewer in length")
            if len(item.get("placeholder") or "") > _SELECT_PLACEHOLDER_LIMIT:
                errors.append(f"components: select {item.get('custom_id')!r} placeholder: Must be {_SELECT_PLACEHOLDER_LIMIT} or fewer in length")
    errors += [f"components[{row}]: buttons: Must be 5 or fewer in length"
               for row, count in buttons.items() if count > 5]
    if v2:
        flat = list(_flatten_v2(v2))
        if len(flat) > _V2_COMPONENT_LIMIT:
            errors.append(f"components: Must be {_V2_COMPONENT_LIMIT} or fewer in length")
        for item in flat:
            if item.get("kind") == "button" and len(item.get("label") or "") > _BUTTON_LABEL_LIMIT:
                errors.append(f"components: button {item.get('custom_id')!r} label: Must be {_BUTTON_LABEL_LIMIT} or fewer in length")
    return errors


def _check_embed(data: dict, index: int) -> None:
    """Raise the real-API 400 when an embed breaks per-field or 6000-char limits."""
    checks = [(f"embeds.{index}.{key}", data.get(key.split(".")[0]) if "." not in key
               else (data.get(key.split(".")[0]) or {}).get(key.split(".")[1]), limit)
              for key, limit in _EMBED_LIMITS.items()
              if not key.startswith("field.")]
    fields = data.get("fields") or []
    if len(fields) > _EMBED_FIELD_LIMIT:
        raise _invalid_form_body([f"embeds.{index}.fields: Must be between 0 and {_EMBED_FIELD_LIMIT} in length"])
    for i, field in enumerate(fields):
        checks.append((f"embeds.{index}.fields[{i}].name", field.get("name"), 256))
        checks.append((f"embeds.{index}.fields[{i}].value", field.get("value"), 1024))
    errors = [f"{key}: Must be {limit} or fewer in length"
              for key, value, limit in checks if len(value or "") > limit]
    if sum(len(value or "") for _, value, _ in checks) > _EMBED_TOTAL_LIMIT:
        errors.append(f"embeds: total content length: Must be {_EMBED_TOTAL_LIMIT} or fewer in length")
    if errors:
        raise _invalid_form_body(errors)


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
        _check_embed(data, len(out))
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
    """Gather app commands from the script namespace and registered cogs.

    Module-level @app_commands.command functions land in env.values(); cog
    commands are bound to their cog instance, so client.cogs is walked too.
    Cog listeners were already stored by add_cog (dispatch is Slice 2).
    """
    session.reset_commands()
    seen = set()
    for value in list(env.values()):
        if isinstance(value, app_commands.Command) and value.name not in seen:
            seen.add(value.name)
            session.cmd_objects[value.name] = value
            session.commands[value.name] = _serialize_command(value)
    for cog in session.client.cogs.values():
        # get_app_commands() (unlike the class's __cog_app_commands__) returns
        # the commands rebound to this instance, so cmd.binding is the cog and
        # dispatch through _do_command calls the callback correctly.
        for cmd in cog.get_app_commands():
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            session.cmd_objects[cmd.name] = cmd
            session.commands[cmd.name] = _serialize_command(cmd)
    if session.commands:
        session.log("⌨️", f"registered slash commands: {', /'.join(sorted(session.commands))}")
    listeners = getattr(session.client, "_listeners", {})
    if listeners:
        names = sorted(listeners)
        session.log("🧩", f"cog listeners registered: {', '.join(names)}", details={"listeners": names})


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


async def _do_command(session: Session, name: str, args: dict, channel_id=None) -> None:
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    channel = session.channels.get(str(channel_id or ""), session.channel)
    if name in session.cmd_objects:
        allowed, reason = channel.permission_check(session.active_user, "view_channel")
        if not allowed:
            session.log("🚫", f"command blocked: missing view_channel permission ({reason})", "warn",
                        kind="event", details={"operation": "interaction.command", "command": name,
                                                 "status": "denied", "permission": "view_channel",
                                                 "reason": reason})
            return
    command_details = {"operation": "interaction.command", "command": name, "arguments": args}
    event = session.log("⌨️", f"command invoked: /{name}", kind="event",
                        details={**command_details, "interaction": "application_command",
                                 "actor": session.active_user.name, "channel": channel.name,
                                 "status": "attempted"})
    event_token = _ACTIVE_EVENT.set(event["id"])
    try:
        cmd = session.cmd_objects.get(name)
        if cmd is None:
            session.log("⚠️", f"/{name} is not defined by the current script", "warn",
                        kind="action", details={**command_details, "status": "missing_command"})
            return
        spec = session.commands.get(name) or {"params": []}
        params = {p["name"]: p for p in spec["params"]}
        kwargs = {}
        for pname, raw in args.items():
            if pname in params and raw is not None and raw != "":
                kwargs[pname] = _coerce_arg(session, params[pname], raw)
        missing = [p["name"] for p in spec["params"] if p["required"] and p["name"] not in kwargs]
        if missing:
            session.log("⚠️", f"/{name} is missing required argument(s): {', '.join(missing)}", "warn",
                        kind="action", details={**command_details, "status": "missing_arguments",
                                                 "missing": missing})
            return
        session.pending_command = name
        try:
            session.log("⚡", f"interaction.command → /{name}", kind="action",
                        details={**command_details, "actor": session.active_user.name,
                                 "channel": channel.name, "status": "dispatched"})
            interaction = session.build_interaction(
                source_message_id=None,
                interaction_type=discord.InteractionType.application_command,
                channel_id=channel.id,
            )
            interaction.command = _CommandRef(name)
            # Match discord.py's Command._do_call exactly: a bound cog command
            # passes its binding (the cog instance); a module-level command's
            # _callback is already rebound and takes no slot self.
            if getattr(cmd, "binding", None) is not None:
                result = cmd._callback(cmd.binding, interaction, **kwargs)
            else:
                result = cmd._callback(interaction, **kwargs)
            if inspect.isawaitable(result):
                await result
            if not interaction.is_done():
                session.log("⚠️", "that interaction was never answered — real Discord shows "
                            "'This interaction failed'", "warn", kind="event",
                            details={**command_details, "status": "unanswered"})
        finally:
            session.pending_command = None
    finally:
        _ACTIVE_EVENT.reset(event_token)


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
        self.workspace: str | None = None       # active workspace name (None = single-file)
        self.workspace_root: Path | None = None  # resolved absolute path, or None
        self.workspace_file: str | None = None  # relative file Run File executed (None = entry bot.py)
        self.user_id = USER_ID
        self.voice_channel: str | None = None   # MockChannel.id of the joined voice room
        self.voice_self_mute = False
        self.voice_self_deaf = False
        self.voice_speaking: set[int] = set()   # member ids currently "speaking"
        self.uploads: list[dict] = []
        self.banned: dict[int, str] = {}        # banned member ids -> names (absent from the guild)
        self.user_name = USER_NAME
        self.next_custom_user_id = CUSTOM_USER_ID
        self.events: list[dict] = []
        self.messages: dict[str, dict] = {}
        self.order: list[str] = []
        self.modals: list[dict] = []
        self.next_index = 1
        self.next_modal_id = 1
        self.last_run: dict | None = None
        self.next_event_id = 1
        self.next_action_id = 1
        self.runner = SessionRunner()
        self.env: dict | None = None
        self.main_task: asyncio.Task | None = None
        self.commands: dict[str, dict] = {}  # serialized, for the UI
        self.cmd_objects: dict[str, app_commands.Command] = {}
        self.pending_command: str | None = None  # chip attached to messages sent during dispatch

    # -- timeline ----------------------------------------------------------

    def _touch(self) -> None:
        self.revision += 1

    def log(self, icon: str, text: str, cls: str | None = None, *,
            kind: str = "event", details: dict | None = None) -> dict:
        if kind == "action":
            event = {"id": f"a{self.next_action_id}", "icon": icon, "text": text, "kind": kind}
            self.next_action_id += 1
        else:
            event = {"id": f"e{self.next_event_id}", "icon": icon, "text": text, "kind": kind,
                     "action_ids": []}
            self.next_event_id += 1
        event["timestamp"] = _now()
        if cls:
            event["cls"] = cls
        if details is not None:
            event["details"] = dict(details)
        related_event_id = _ACTIVE_EVENT.get()
        if kind == "action" and related_event_id:
            event["event_id"] = related_event_id
            related = next((item for item in reversed(self.events) if item["id"] == related_event_id), None)
            if related is not None:
                related["action_ids"].append(event["id"])
        self.events.append(event)
        self._touch()
        if len(self.events) > _MAX_EVENTS:
            del self.events[:-_MAX_EVENTS]
        return event

    def add_message(self, content=None, **kwargs) -> dict:
        if content is not None and len(str(content)) > _CONTENT_LIMIT:
            raise _invalid_form_body([f"content: Must be {_CONTENT_LIMIT} or fewer in length"])
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
            "author": {"id": author.id, "name": author.display_name, "display_name": author.display_name,
                       "username": author.name, "avatar_url": author.avatar_url,
                       "banner_url": author.banner_url, "bio": author.bio,
                       "accent_color": author.accent_color, "bot": author.bot},
            "channel": channel_key,
            "command": command_chip,
            "content": content if content is not None else "",
            "embeds": _embeds_to_json(kwargs),
            "components": classic,
            "v2": v2,
            "ephemeral": bool(kwargs.get("ephemeral")),
            "ephemeral_user_id": str(kwargs["ephemeral_user_id"])
            if kwargs.get("ephemeral") and kwargs.get("ephemeral_user_id") is not None else None,
            "banner_url": author.banner_url, "bio": author.bio,
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
            self.log("⚠️", "edit_message targeted a missing message; ignored", "warn", kind="action",
                     details={"operation": "message.edit", "message_id": message_id, "status": "missing_message"})
            return
        if content is not None:
            text = content if isinstance(content, str) else str(content)
            if len(text) > _CONTENT_LIMIT:
                raise _invalid_form_body([f"content: Must be {_CONTENT_LIMIT} or fewer in length"])
            msg["content"] = text
        embeds = _embeds_to_json(kwargs)
        if kwargs.get("embed") is not None or kwargs.get("embeds") is not None:
            msg["embeds"] = embeds
        if "view" in kwargs:
            classic, v2 = _stash_view(kwargs.get("view"))
            msg["components"], msg["v2"] = classic, v2
        msg["revision"] += 1
        self._touch()
        self.log("✏️", f"message #{msg['index']} edited", kind="action",
                 details={"operation": "message.edit", "message_id": msg["id"],
                          "content": msg["content"]})

    # -- reactions ---------------------------------------------------------

    def toggle_reaction(self, message_id: str, emoji: str, *, user_id: int, actor: str = "user") -> bool:
        """Toggle one user's reaction on a message; returns True if it was added.

        Raises KeyError when the message is missing or deleted, ValueError when
        the emoji is malformed, and discord.Forbidden when the actor lacks the
        add_reactions permission.
        """
        msg = self.messages.get(message_id or "")
        if msg is None or msg["deleted"]:
            raise KeyError(message_id)
        emoji = str(emoji or "").strip()
        if not emoji or len(emoji) > 32:
            raise ValueError("invalid emoji")
        member = self.guild.get_member(int(user_id))
        if member is not None:
            channel = self.channels.get(msg.get("channel") or "", self.channel)
            allowed, reason = channel.permission_check(member, "add_reactions")
            if not allowed:
                if actor == "user":
                    self.log("🚫", f"reaction {emoji} blocked: missing add_reactions permission ({reason})",
                             "warn", kind="action",
                             details={"operation": "message.toggle_reaction", "emoji": emoji,
                                      "message_id": message_id, "status": "denied",
                                      "permission": "add_reactions", "reason": reason,
                                      "actor": member.name})
                    raise discord.Forbidden(_FORBIDDEN,
                                            f"missing add_reactions permission ({reason})")
                return False
        reactions = msg.setdefault("reactions", [])
        for reaction in reactions:
            if reaction["emoji"] == emoji:
                if str(user_id) in reaction["users"]:
                    reaction["users"].remove(str(user_id))
                    if not reaction["users"]:
                        reactions.remove(reaction)
                    added = False
                else:
                    reaction["users"].append(str(user_id))
                    added = True
                break
        else:
            reactions.append({"emoji": emoji, "users": [str(user_id)]})
            added = True
        msg["revision"] += 1
        self._touch()
        self.log("➕" if added else "➖", f"reaction {emoji} on message #{msg['index']}", kind="action",
                 details={"operation": "message.toggle_reaction", "emoji": emoji, "message_id": message_id,
                          "user_id": str(user_id), "status": "success"})
        return added

    # -- voice (pure simulation; no audio is captured or transmitted) --------

    def voice_action(self, action: str, *, channel_id: str | None = None) -> dict:
        """Simulated voice state change for the active user; returns the new state."""
        if action not in _VOICE_ACTIONS:
            raise ValueError(f"unknown voice action {action!r}")
        user = self.active_user
        if user.bot:
            raise ValueError("the bot cannot join a voice channel")
        if action == "join":
            channel = self.channels.get(str(channel_id or ""))
            if channel is None:
                raise ValueError("unknown voice channel")
            self.voice_channel = channel.id
        elif action == "leave":
            self.voice_channel = None
            self.voice_speaking.clear()
        elif action == "mute":
            self.voice_self_mute = True
            self.voice_speaking.discard(user.id)
        elif action == "unmute":
            self.voice_self_mute = False
            self.voice_speaking.add(user.id)  # unmuted = talking, in simulation
        elif action == "deafen":
            self.voice_self_deaf = True
            self.voice_self_mute = True
            self.voice_speaking.clear()
        elif action == "undeafen":
            self.voice_self_deaf = False
            self.voice_self_mute = False
        text, sound = _VOICE_ACTIONS[action]
        self.log("🎧", text, kind="event",
                 details={"operation": "voice.simulate", "action": action,
                          "actor": user.name, "channel":
                          (self.channels.get(str(self.voice_channel)).name
                           if self.voice_channel is not None else None),
                          "status": "success"})
        self._touch()
        return {"action": action, "sound": sound}

    # -- uploads (local pass-through; data stays in the session) -------------

    def add_upload(self, name: str, size: int, content_type: str, data_uri: str | None) -> dict:
        """Register a browser-side upload; small images are inlined as data URIs."""
        clean = re.sub(r"[^A-Za-z0-9_. ()-]", "_", (name or "file").strip())[:80] or "file"
        entry = {"name": clean, "size": int(size), "content_type": content_type or "",
                 "data_uri": data_uri, "timestamp": _now()}
        self.uploads.append(entry)
        if len(self.uploads) > 25:
            del self.uploads[:-25]
        image = data_uri is not None and content_type.startswith("image/")
        self.log("📎", f"uploaded {clean} ({int(size) / 1024:.1f} KiB)", kind="action",
                 details={"operation": "message.attachment", "file": clean,
                          "size": int(size), "inlined": bool(image), "status": "success"})
        self._touch()
        return entry

    def delete_upload(self, index: int) -> None:
        if not 0 <= index < len(self.uploads):
            raise KeyError(index)
        removed = self.uploads.pop(index)
        self.log("🗑", f"removed upload {removed['name']}", kind="action",
                 details={"operation": "message.attachment", "file": removed["name"],
                          "status": "deleted"})
        self._touch()

    # -- moderation (kick / ban / timeout) -----------------------------------

    def _moderator_check(self, permission: str) -> MockMember:
        actor = self.active_user
        allowed, reason = self.channel.permission_check(actor, permission)
        if not allowed:
            self.log("🚫", f"moderation blocked: missing {permission} permission ({reason})", "warn",
                     kind="event", details={"operation": "member.moderate", "permission": permission,
                                              "actor": actor.name, "status": "denied"})
            raise discord.Forbidden(_FORBIDDEN, f"missing {permission} permission ({reason})")
        return actor

    def kick_member(self, user_id: int) -> None:
        """Remove a simulated member; add_member recreates them from scratch."""
        actor = self._moderator_check("kick_members")
        member = self.guild.get_member(int(user_id))
        if member is None or member.bot:
            raise ValueError("unknown simulated user")
        if member.id == USER_ID:
            raise ValueError("the server owner cannot be kicked")
        member.banned = False
        self.guild.members.remove(member)
        self.log("👢", f"{member.name} was kicked by {actor.name}", kind="event",
                 details={"operation": "member.kick", "actor": actor.name,
                          "target": member.name, "status": "success"})
        self._touch()

    def ban_member(self, user_id: int) -> None:
        actor = self._moderator_check("ban_members")
        member = self.guild.get_member(int(user_id))
        if member is None or member.bot:
            raise ValueError("unknown simulated user")
        if member.id == USER_ID:
            raise ValueError("the server owner cannot be banned")
        member.banned = True
        member.timeout_until = None
        self.banned[int(user_id)] = member.name
        self.guild.members.remove(member)
        self.log("🔨", f"{member.name} was banned by {actor.name}", kind="event",
                 details={"operation": "member.ban", "actor": actor.name,
                          "target": member.name, "status": "success"})
        self._touch()

    def timeout_member(self, user_id: int, minutes: int) -> None:
        actor = self._moderator_check("moderate_members")
        member = self.guild.get_member(int(user_id))
        if member is None or member.bot:
            raise ValueError("unknown simulated user")
        minutes = int(minutes or 0)
        if minutes <= 0:  # clearing an existing timeout
            member.timeout_until = None
            self.log("⏳", f"{member.name}'s timeout was removed by {actor.name}", kind="event",
                     details={"operation": "member.timeout", "actor": actor.name, "target": member.name,
                              "minutes": 0, "status": "success"})
            self._touch()
            return
        minutes = min(minutes, 40320)  # real API caps at 28 days
        member.timeout_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.log("⏳", f"{member.name} was timed out for {minutes} min by {actor.name}", kind="event",
                 details={"operation": "member.timeout", "actor": actor.name, "target": member.name,
                          "minutes": minutes, "status": "success"})
        self._touch()

    def unban_member(self, user_id: int) -> None:
        """Remove a ban; absent members are recreated fresh (custom profiles reset)."""
        actor = self._moderator_check("ban_members")
        member = self.guild.get_member(int(user_id))
        if member is not None:
            member.banned = False
            member.timeout_until = None
            target, name = member, member.name
        else:
            name = self.banned.pop(int(user_id), None)
            if name is None:
                raise ValueError("that user is not banned")
            target = self.guild._member(int(user_id), name)
            self.guild.members.append(target)
        target.banned = False
        target.timeout_until = None
        self.log("🕊", f"{name} was unbanned by {actor.name}", kind="event",
                 details={"operation": "member.unban", "actor": actor.name,
                          "target": name, "status": "success"})
        self._touch()

    def create_text_channel_ui(self, name: str, topic: str | None = None) -> MockChannel:
        """User-driven channel creation (same normalization as bot-side make_channel)."""
        actor = self._moderator_check("manage_channels")
        channel = self.make_channel(name)
        channel.topic = (topic or "").strip()[:1024] or None
        self.log("📋", f"#{channel.name} was created by {actor.name}", kind="event",
                 details={"operation": "channel.create", "actor": actor.name,
                          "channel": channel.name, "status": "success"})
        self._touch()
        return channel

    def delete_message(self, message_id: str | None, *, actor_id: int | None = None) -> None:
        msg = self.messages.get(message_id or "")
        if msg is None or msg["deleted"]:
            self.log("⚠️", "delete_message targeted a missing message; ignored", "warn", kind="action",
                     details={"operation": "message.delete", "message_id": message_id, "status": "missing_message"})
            return
        if msg.get("ephemeral") and actor_id is not None \
                and msg.get("ephemeral_user_id") != str(actor_id):
            self.log("🚫", "ephemeral message is only visible to its interaction user", "warn",
                     kind="action", details={"operation": "message.delete", "message_id": message_id,
                                              "status": "denied", "permission": "ephemeral_owner_only"})
            return
        msg["deleted"] = True
        if message_id in self.order:
            self.order.remove(message_id)
        self._touch()
        self.log("🗑️", f"message #{msg['index']} deleted", kind="action",
                 details={"operation": "message.delete", "message_id": message_id})

    def open_modal(self, modal: discord.ui.Modal, source_message_id: str | None,
                   channel_id=None, user_id=None, custom_id=None) -> dict:
        title = modal.title if isinstance(modal.title, str) else "Modal"
        items = [_item_to_json(child) for child in modal.children]
        return self.open_modal_payload(
            title, items, source_message_id, channel_id=channel_id, user_id=user_id,
            custom_id=custom_id or getattr(modal, "custom_id", None),
        )

    def open_modal_payload(self, title: str, items: list[dict], source_message_id: str | None,
                           *, channel_id=None, custom_id=None, user_id=None) -> dict:
        modal = {
            "id": f"mo{self.next_modal_id}", "title": title,
            "items": items, "source": source_message_id,
            "channel_id": str(channel_id or (self.messages.get(source_message_id or "") or {}).get("channel")
                               or self.channel.id),
            "user_id": str(self.active_user.id if user_id is None else user_id),
        }
        if custom_id:
            modal["custom_id"] = str(custom_id)
        self.modals.append(modal)
        self.next_modal_id += 1
        self._touch()
        self.log("📋", f"modal opened: {title!r}", kind="action",
                 details={"operation": "interaction.response.send_modal", "title": title,
                          "source_message_id": source_message_id})
        return modal

    def dismiss_modal(self, modal_id: str, user_id: int) -> bool:
        modal = next((item for item in self.modals if item["id"] == str(modal_id)), None)
        if modal is None:
            return False
        if modal.get("user_id") not in (None, str(user_id)):
            raise ValueError("modal is only visible to the user who opened it")
        self.modals.remove(modal)
        self._touch()
        return True

    def visible_messages(self, user_id: int) -> list[dict]:
        return [self.messages[mid] for mid in self.order if mid in self.messages
                and not self.messages[mid].get("deleted")
                and (not self.messages[mid].get("ephemeral")
                     or self.messages[mid].get("ephemeral_user_id") == str(user_id))]

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
            self.log("⚠️", f"channel.delete targeted missing channel #{key}", "warn", kind="action",
                     details={"operation": "channel.delete", "channel_id": key, "status": "missing_channel"})
            return
        if len(self.channels) <= 1:
            self.log("⚠️", "can't delete the last remaining channel", "warn", kind="action",
                     details={"operation": "channel.delete", "channel": ch.name, "status": "blocked_last_channel"})
            return
        del self.channels[key]
        for mid in [m for m, msg in self.messages.items() if msg.get("channel") == key]:
            msg = self.messages.pop(mid)
            msg["deleted"] = True
            if mid in self.order:
                self.order.remove(mid)
        self.log("🗑️", f"channel #{ch.name} deleted", kind="action",
                 details={"operation": "channel.delete", "channel": ch.name, "status": "ok"})

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

    def add_member(self, username: str, profile: dict | None = None) -> MockMember:
        username = username.strip() if isinstance(username, str) else ""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", username):
            raise ValueError("username must be 1–32 letters, numbers, dots, dashes, or underscores")
        if any(not member.bot and member.name.casefold() == username.casefold()
               for member in self.guild.members):
            raise ValueError("that username already exists")
        if sum(member.custom for member in self.guild.members) >= 20:
            raise ValueError("a simulation can have at most 20 added users")
        member = self.guild._member(self.next_custom_user_id, username)
        member.custom = True
        self.guild.members.append(member)
        try:
            if profile:
                self.update_member_profile(member.id, {**profile, "username": username})
        except (TypeError, ValueError):
            self.guild.members.remove(member)
            raise
        self.next_custom_user_id += 1
        self._touch()
        self.log("➕", f"simulated user added: {username}", details={"user_id": str(member.id)})
        return member

    def update_member_profile(self, user_id: int, profile: dict) -> None:
        member = self.guild.get_member(user_id)
        if member is None or member.bot:
            raise ValueError("unknown simulated user")
        if not isinstance(profile, dict):
            raise TypeError("profile must be an object")
        allowed = {"user_id", "username", "display_name", "bio", "avatar_url",
                   "banner_url", "accent_color", "status"}
        if set(profile) - allowed:
            raise ValueError("profile contains an unsupported field")

        def text_field(key: str, default: str, maximum: int) -> str:
            value = profile.get(key, default)
            if not isinstance(value, str):
                raise TypeError(f"{key} must be text")
            value = value.strip()
            if len(value) > maximum:
                raise ValueError(f"{key} must be at most {maximum} characters")
            return value

        username = text_field("username", member.name, 32)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", username):
            raise ValueError("username must be 1–32 letters, numbers, dots, dashes, or underscores")
        if any(other is not member and not other.bot and other.name.casefold() == username.casefold()
               for other in self.guild.members):
            raise ValueError("that username already exists")
        display_name = text_field("display_name", member.display_name, 32) or username
        bio = text_field("bio", member.bio, 190)

        def image_url(key: str) -> str | None:
            value = profile.get(key, getattr(member, key))
            if value is None or value == "":
                return None
            if not isinstance(value, str) or len(value) > 1000:
                raise ValueError(f"{key} must be an http(s) image URL under 1000 characters")
            try:
                parsed = urlsplit(value.strip())
            except ValueError as error:
                raise ValueError(f"{key} must be an http(s) image URL") from error
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"{key} must be an http(s) image URL")
            return value.strip()

        avatar_url = image_url("avatar_url")
        banner_url = image_url("banner_url")
        accent_color = profile.get("accent_color", member.accent_color)
        if accent_color in (None, ""):
            accent_color = None
        elif not isinstance(accent_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent_color):
            raise ValueError("accent_color must be a six-digit hex color")
        else:
            accent_color = accent_color.lower()
        status = profile.get("status", getattr(member.status, "name", "online"))
        if not isinstance(status, str) or status.lower() not in {
            "online", "idle", "dnd", "invisible", "offline",
        }:
            raise ValueError("status must be online, idle, dnd, invisible, or offline")

        member.name = username
        member.global_name = display_name
        member.display_name = display_name
        member.bio = bio
        member.avatar_url = avatar_url
        member.banner_url = banner_url
        member.accent_color = accent_color
        member.status = getattr(discord.Status, status.lower())
        if self.user_id == member.id:
            self.user_name = username
        self._touch()
        self.log("🪪", f"profile updated for {username}", details={"user_id": str(member.id)})

    def build_interaction(self, source_message_id: str | None = None,
                          custom_id: str | None = None, values: list | None = None,
                          interaction_type: discord.InteractionType = discord.InteractionType.application_command,
                          channel_id=None) -> MockInteraction:
        return MockInteraction(self, source_message_id, custom_id, values, interaction_type, channel_id)

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
        self.next_event_id = 1
        self.next_action_id = 1
        self.last_run = None
        _unload_workspace_modules(self)
        self.client.cogs.clear()
        self.client._listeners.clear()
        self.workspace = None
        self.workspace_root = None
        self.workspace_file = None

    def close(self) -> None:
        self.runner.shutdown()


# --------------------------------------------------------------- script running


def _unload_workspace_modules(session: Session) -> None:
    """Remove workspace-derived entries from sys.modules (re-run isolation)."""
    root = getattr(session, "workspace_root", None)
    if root is None:
        return
    prefix = str(Path(root))
    for name in [name for name, module in sys.modules.items()
                 if getattr(module, "__file__", None)
                 and str(Path(module.__file__).resolve()).startswith(prefix)]:
        sys.modules.pop(name, None)


def _workspace_allowlist(root: Path) -> set[str] | None:
    """Relative posix paths allowed by an optional workspace.json manifest.

    Returns None when no manifest exists (any non-hidden .py under the folder
    is importable). Rejects malformed manifests with a clear error.
    """
    manifest = root / "workspace.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        files = data["files"]
    except (OSError, ValueError, KeyError) as error:
        raise ImportError(f"invalid workspace.json: {error}") from error
    if not isinstance(files, list) or not all(isinstance(entry, str) for entry in files):
        raise ImportError("workspace.json: 'files' must be an array of relative paths")
    for entry in files:
        rel = Path(entry)
        if (entry != rel.as_posix() or rel.is_absolute() or rel.suffix != ".py"
                or any(part in {"", ".", ".."} for part in rel.parts)):
            raise ImportError(f"workspace.json rejects entry {entry!r}: "
                              "use relative posix paths ending in .py")
    return set(files)


def _resolve_importable(session: Session, relative: str) -> Path:
    """Resolve a workspace-relative import target, enforcing the manifest.

    Raises ImportError (a catchable import-time error) with a clear message
    for absolute paths, `..` escapes, symlinks leaving the workspace,
    non-.py files, and files outside an allowlist.
    """
    rel = Path(relative)
    root = Path(session.workspace_root)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ImportError(f"workspace import rejected: {relative!r} escapes the workspace folder")
    if rel.suffix != ".py":
        raise ImportError(f"workspace import rejected: {relative!r} is not a .py file")
    resolved = (root / rel).resolve()
    if not resolved.is_relative_to(root) or (resolved.exists() and resolved.is_symlink()):
        raise ImportError(f"workspace import rejected: {relative!r} points outside the workspace")
    allowlist = _workspace_allowlist(root)
    posix = rel.as_posix()
    if allowlist is not None and posix not in allowlist:
        raise ImportError(f"workspace import rejected: {relative!r} is not in the workspace.json allowlist")
    return resolved


def _install_workspace_import_hook(session: Session) -> None:
    """Route workspace-file imports through the manifest checks.

    Every import in the entry module resolves through this finder first; it
    only handles paths inside the workspace folder, so discord.py and the
    standard library are untouched. Rejections are raised only when the
    workspace actually contains a file for the module being probed — a miss
    for discord.py etc. must stay a silent None so normal imports proceed.
    The synthetic parent package prefix (playground_ws_<sid>) is stripped so
    `from .helpers import greet` resolves to helpers.py in the workspace.
    """
    root = Path(session.workspace_root)
    package_name = f"playground_ws_{session.sid}"

    class _WorkspaceFinder(importlib.abc.MetaPathFinder):
        @staticmethod
        def find_spec(fullname, path=None, target=None):
            if session.workspace_root is None:
                return None
            parts = fullname.split(".")
            if parts[0] == package_name:
                parts = parts[1:]
            candidates = ["/".join(parts[:i + 1]) + ".py" for i in range(len(parts))]
            rejection = None
            for candidate in candidates:
                if not (root / candidate).exists():
                    continue
                try:
                    resolved = _resolve_importable(session, candidate)
                except ImportError as error:
                    rejection = rejection or error
                    continue
                if resolved.is_file():
                    return importlib.util.spec_from_file_location(fullname, resolved)
            if rejection is not None:
                raise rejection
            return None

    sys.meta_path.insert(0, _WorkspaceFinder())


def _run_entry_module(session: Session, env: dict, buffer: io.StringIO,
                      code: str | None = None, rel_file: str | None = None) -> None:
    """Exec the workspace's bot.py (or a workspace buffer) as `__main__`.

    `__package__` is set to the workspace package name so `from .helpers
    import greet` resolves like a package-style entry (manifest-supported).
    With `code`, the given buffer runs instead of bot.py (Run File / watcher
    reload); its compile filename is the saved file when `rel_file` is known,
    else `<playground>` so error mapping stays on the editor buffer.
    """
    root = Path(session.workspace_root)
    entry = root / "bot.py"
    resolved_entry = entry.resolve()
    if code is None:
        source, filename = entry.read_text(encoding="utf-8"), str(resolved_entry)
        dunder_file = str(resolved_entry)
    elif rel_file:
        source = code
        filename = dunder_file = str((root / rel_file).resolve())
    else:
        source, filename = code, "<playground>"
        dunder_file = str(resolved_entry)
    package_name = f"playground_ws_{session.sid}"
    for finder in list(sys.meta_path):
        if type(finder).__name__ == "_WorkspaceFinder":
            sys.meta_path.remove(finder)
    _install_workspace_import_hook(session)
    previous_path = list(sys.path)
    modules_before = set(sys.modules)
    sys.path.insert(0, str(root))
    parent = importlib.util.module_from_spec(
        importlib.machinery.ModuleSpec(package_name, None, is_package=True))
    parent.__path__ = [str(root)]  # relative imports resolve against the workspace
    sys.modules[package_name] = parent
    previous_trace = sys.gettrace()
    sys.settrace(_script_trace(getattr(session, "user_deadline", time.monotonic() + 20)))
    try:
        with contextlib.redirect_stdout(buffer):
            env["__file__"] = dunder_file
            env["__name__"] = "__main__"
            env["__package__"] = package_name
            env["__spec__"] = None
            exec(compile(source, filename, "exec"), env)  # noqa: S102 - the whole point
    finally:
        sys.settrace(previous_trace)
        for finder in list(sys.meta_path):
            if type(finder).__name__ == "_WorkspaceFinder":
                sys.meta_path.remove(finder)
        sys.path[:] = previous_path
        sys.modules.pop(package_name, None)
        for name in set(sys.modules) - modules_before:
            module = sys.modules.get(name)
            module_file = getattr(module, "__file__", None)
            if module_file and str(Path(module_file).resolve()).startswith(str(root)):
                sys.modules.pop(name, None)


async def _scan_setup(session: Session, env: dict) -> None:
    """Await the cog-convention `setup(bot)` once: module namespace, then cogs."""
    client = session.client
    seen = []
    setup = env.get("setup")
    if inspect.iscoroutinefunction(setup):
        seen.append("bot.py")
        await setup(client)
    for cog in client.cogs.values():
        setup = getattr(cog, "setup", None)
        if inspect.iscoroutinefunction(setup) and not getattr(setup, "_playground_ran", False):
            setup._playground_ran = True
            seen.append(type(cog).__name__)
            await setup(client)
    if seen:
        session.log("🧩", f"setup() ran for: {', '.join(seen)}")


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
    """Interrupt CPU-bound user code; asyncio cancellation cannot stop a busy loop.

    Applies to the entry exec (<playground>) and to frames whose code lives
    under the active workspace, so an infinite loop in an imported helper is
    interrupted by the deadline instead of wedging the session thread.
    """
    workspace_root = getattr(_CURRENT_SESSION.get(), "workspace_root", None)
    prefix = str(Path(workspace_root)) if workspace_root else None

    def trace(frame, event, _arg):
        filename = frame.f_code.co_filename
        if event == "line" and (
            filename == "<playground>"
            or (prefix is not None and filename.startswith(prefix))
        ) and time.monotonic() >= deadline:
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


def _script_error_details(error: BaseException) -> dict:
    """Map a script exception to the last user-code frame for editor navigation."""
    session = _CURRENT_SESSION.get()
    roots = ["<playground>"]
    if getattr(session, "workspace_root", None) is not None:
        roots.append(str(Path(session.workspace_root)))
    filename = None
    line = None
    if isinstance(error, SyntaxError) and error.filename in roots:
        filename, line = error.filename, error.lineno
    else:
        frame = next((frame for frame in reversed(traceback.extract_tb(error.__traceback__ or None))
                      if frame.filename in roots), None)
        if frame is not None:
            filename, line = frame.filename, frame.lineno
            if roots[-1] != "<playground>" and filename.startswith(roots[-1]):
                # Workspace file: report the workspace-relative path so the
                # Problems panel can open workspace/file:line directly.
                filename = Path(filename).relative_to(roots[-1]).as_posix()
    message = error.msg if isinstance(error, SyntaxError) else str(error)
    return {"type": type(error).__name__, "message": message, "file": filename, "line": line}


def _record_script_error(session: Session, error: Exception, started: float) -> None:
    """Expose callback failures through the same error panel as startup failures."""
    details = _script_error_details(error)
    summary = f"{type(error).__name__}: {error}"
    traceback_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    current = session.last_run or {}
    session.log("💥", summary, "error", details={
        **details, "operation": "script.callback", "status": "script_error",
    })
    session.last_run = {
        "ok": False, "error": traceback_text, "exception": details,
        "ms": (time.perf_counter() - started) * 1000 + current.get("ms", 0.0),
    }


async def _do_run(session: Session, code: str) -> None:
    await _stop_main(session)
    session.env = None
    session.reset_commands()
    session.last_run = None
    session.clear_timeline()
    session.reset_channels()  # a fresh Run boots the saved bot.py; Run File re-runs the buffer
    _unload_workspace_modules(session)  # a re-run must not see the last run's modules
    session.client.cogs.clear()  # re-running a cog bot must not hit duplicate-cog errors
    session.client._listeners.clear()
    env = _build_env(session)
    buffer = io.StringIO()
    started = time.perf_counter()
    token = _CURRENT_SESSION.set(session)
    previous_trace = sys.gettrace()
    sys.settrace(_script_trace(getattr(session, "user_deadline", time.monotonic() + 20)))
    try:
        if session.workspace_root is not None:
            rel_file = getattr(session, "workspace_file", None)
            _run_entry_module(session, env, buffer,
                              code if rel_file else None, rel_file)
        else:
            with contextlib.redirect_stdout(buffer):
                exec(compile(code, "<playground>", "exec"), env)  # noqa: S102 - the whole point
    except BaseException as error:  # noqa: BLE001 - user code may raise anything, incl. SystemExit
        _flush_stdout(buffer, session)
        tb = traceback.format_exc(limit=6)
        session.log("💥", tb, "error")
        session.last_run = {"ok": False, "error": tb, "exception": _script_error_details(error),
                            "ms": (time.perf_counter() - started) * 1000}
        return
    finally:
        sys.settrace(previous_trace)
        _CURRENT_SESSION.reset(token)
    _flush_stdout(buffer, session)

    try:
        await _scan_setup(session, env)
    except BaseException as error:  # noqa: BLE001 - setup() is user code too
        tb = traceback.format_exc(limit=6)
        session.log("💥", tb, "error")
        session.last_run = {"ok": False, "error": tb, "exception": _script_error_details(error),
                            "ms": (time.perf_counter() - started) * 1000}
        return
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
    except BaseException as error:  # noqa: BLE001 - background task must swallow-and-report
        tb = "".join(traceback.format_exception(*sys.exc_info()))
        session.log("💥", tb, "error")
        session.last_run = {"ok": False, "error": tb, "exception": _script_error_details(error), "ms": 0.0}


def _find_component(components: list[dict], v2: list[dict], custom_id: str) -> dict | None:
    for item in components:
        if item.get("custom_id") == custom_id:
            return item
    stack = list(v2)
    while stack:
        item = stack.pop()
        if item.get("custom_id") == custom_id:
            return item
        stack.extend(item.get("children") or [])
        if item.get("accessory"):
            stack.append(item["accessory"])
    return None


async def _do_click(session: Session, message_id: str, custom_id: str, values: list) -> None:
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    message = session.messages.get(message_id) or {}
    if message.get("ephemeral") and message.get("ephemeral_user_id") != str(session.active_user.id):
        session.log("🚫", "ephemeral message is only visible to its interaction user", "warn", kind="event",
                    details={"operation": "interaction.component", "message_id": message_id,
                             "actor": session.active_user.name, "status": "denied"})
        return
    handler = env.get("on_click")
    channel = session.channels.get(str(message.get("channel") or ""), session.channel)
    click_details = {"interaction": "component", "custom_id": custom_id,
                     "message_id": message_id, "values": values,
                     "actor": session.active_user.name, "channel": channel.name}
    event = session.log("🖱️", f"component used: {custom_id!r}", kind="event",
                        details={**click_details, "status": "attempted"})
    allowed, reason = channel.permission_check(session.active_user, "view_channel")
    if not allowed:
        session.log("🚫", f"component use blocked: missing view_channel permission ({reason})", "warn",
                    kind="event", details={**click_details, "status": "denied", "permission": "view_channel",
                                           "reason": reason})
        return
    component = _find_component(message.get("components") or [], message.get("v2") or [], custom_id)
    if not component or component.get("disabled") or message.get("deleted"):
        session.log("⚠️", f"component {custom_id!r} is not available on this message", "warn", kind="event",
                    details={**click_details, "status": "invalid_component"})
        return
    if not callable(handler):
        session.log("⚠️", f"clicked {custom_id!r} but no `on_click` handler is defined", "warn",
                    kind="event", details={**click_details, "status": "missing_handler"})
        return
    event_token = _ACTIVE_EVENT.set(event["id"])
    try:
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
                kind="event",
                details={**click_details, "status": "unanswered"},
            )
    finally:
        _ACTIVE_EVENT.reset(event_token)


def _modal_value(values: dict, item: dict) -> str:
    """Read a field by its Discord custom ID, with a label alias for older clients."""
    custom_id = item.get("custom_id")
    if custom_id in values:
        return str(values[custom_id] or "")
    label = item.get("label")
    if isinstance(label, str):
        key = re.sub(r"[^a-z0-9]+", "", label.casefold())
        for name, value in values.items():
            if re.sub(r"[^a-z0-9]+", "", str(name).casefold()) == key:
                return str(value or "")
    return ""


async def _do_submit(session: Session, modal_id: str, values: dict) -> None:
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    handler = env.get("on_submit")
    modal = next((m for m in session.modals if m["id"] == modal_id), None)
    if modal is None:
        session.log("⚠️", f"modal {modal_id!r} is no longer open", "warn", kind="event",
                    details={"operation": "interaction.modal_submit", "modal_id": modal_id,
                             "status": "missing_modal"})
        return
    if modal.get("user_id") not in (None, str(session.active_user.id)):
        session.log("🚫", "modal is only visible to the user who opened it", "warn", kind="event",
                    details={"operation": "interaction.modal_submit", "modal_id": modal_id,
                             "actor": session.active_user.name, "status": "denied"})
        return
    for item in modal.get("items", []):
        value = _modal_value(values, item)
        if item.get("required") and not value.strip():
            session.log("⚠️", f"modal field {item.get('label') or item.get('custom_id')!r} is required", "warn",
                        kind="event", details={"operation": "interaction.modal_submit", "modal_id": modal_id,
                                                 "status": "invalid_form"})
            return
        if len(value) < (item.get("min_length") or 0) or len(value) > (item.get("max_length") or 4000):
            session.log("⚠️", f"modal field {item.get('label') or item.get('custom_id')!r} has invalid length", "warn",
                        kind="event", details={"operation": "interaction.modal_submit", "modal_id": modal_id,
                                                 "status": "invalid_form"})
            return
    source = session.messages.get((modal or {}).get("source") or "") or {}
    channel = session.channels.get(
        (modal or {}).get("channel_id") or source.get("channel") or "", session.channel
    )
    submit_details = {"interaction": "modal_submit", "modal_id": modal_id, "values": values,
                      "actor": session.active_user.name, "channel": channel.name}
    event = session.log("📝", f"modal submitted: {modal_id!r}", kind="event",
                        details={**submit_details, "status": "attempted"})
    session.modals.remove(modal)
    session._touch()
    if not callable(handler):
        session.log("⚠️", f"modal {modal_id} submitted but no `on_submit` handler is defined", "warn",
                    kind="event", details={**submit_details, "status": "missing_handler"})
        return
    event_token = _ACTIVE_EVENT.set(event["id"])
    try:
        interaction = session.build_interaction(
            modal["source"], interaction_type=discord.InteractionType.modal_submit,
            channel_id=channel.id,
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
                kind="event",
                details={**submit_details, "status": "unanswered"},
            )
    finally:
        _ACTIVE_EVENT.reset(event_token)


async def _do_message(session: Session, content: str, channel_id=None,
                      files: list[dict] | None = None) -> None:
    """The composer sent a chat message; hand it to on_message if defined."""
    env = session.env
    if not env:
        raise RuntimeError("Nothing is running yet — press Run first.")
    channel = session.channels.get(str(channel_id), session.channel)
    allowed, reason = channel.permission_check(session.active_user, "send_messages")
    if not allowed:
        session.log("🚫", f"message blocked: missing send_messages permission ({reason})", "warn",
                    kind="event", details={"interaction": "message_create", "operation": "message.send",
                                           "channel": channel.name, "actor": session.active_user.name,
                                           "permission": "send_messages", "reason": reason, "status": "denied"})
        return
    msg = session.add_message(channel_id=channel.id, content=content,
                              author=session.active_user, files=files or None)
    handle = MockMessage(session, msg["id"], msg.get("author_obj"))
    handler = env.get("on_message")
    message_details = {"interaction": "message_create", "operation": "message.send",
                       "message_id": msg["id"],        "channel": channel.name, "channel_id": str(channel.id), "actor": session.active_user.name,
                       "content": content, "status": "dispatched" if callable(handler) else "missing_handler"}
    if files:
        message_details["files"] = [f.get("name") for f in files]

    event = session.log("💬", f"message received from {session.active_user.name}",
                        None if callable(handler) else "warn", kind="event", details=message_details)
    if not callable(handler):
        return
    event_token = _ACTIVE_EVENT.set(event["id"])
    try:
        result = _call(handler, handle)
        if inspect.isawaitable(result):
            await result
    finally:
        _ACTIVE_EVENT.reset(event_token)


async def _gated(session: Session, coro_fn) -> None:
    async with session.runner.gate:
        await coro_fn()


async def run_script(session: Session, code: str, timeout: float = 20.0,
                     *, workspace: str | None = None, workspace_root=None,
                     workspace_file: str | None = None) -> dict:
    """Run a bot: `code` as a single-file module, or the workspace's bot.py.

    With `workspace` set, the saved bot.py on disk is the entry point and
    `code` is advisory (run-file keeps passing the editor buffer; Run passes
    the loaded file). Missing bot.py falls back to single-file exec of `code`.
    """
    if len(code) > 1_000_000:
        message = "Script is too large (maximum 1 MB)."
        session.log("⚠️", message, "error")
        session.last_run = {"ok": False, "error": message,
                            "exception": {"type": "ScriptLimitError", "message": message,
                                          "file": None, "line": None}, "ms": 0.0}
        return {"ok": False, "ms": 0.0}
    if workspace is not None and workspace_root is not None and (Path(workspace_root) / "bot.py").is_file():
        session.workspace = workspace
        session.workspace_root = Path(workspace_root).resolve()
        session.workspace_file = workspace_file  # None = boot the saved entry
    else:
        session.workspace = None
        session.workspace_root = None
        session.workspace_file = None
    started = time.perf_counter()
    session.user_deadline = time.monotonic() + timeout
    try:
        await session.runner.run(lambda: _gated(session, lambda: _do_run(session, code)), timeout)
    except ScriptStuck as exc:
        session.log("🛑", str(exc), "error")
        session.last_run = {"ok": False, "error": str(exc), "exception": _script_error_details(exc),
                            "ms": (time.perf_counter() - started) * 1000}
    return {"ok": bool(session.last_run and session.last_run["ok"]),
            "ms": round(session.last_run["ms"], 1) if session.last_run else 0.0}


def _restore_cogs(session: Session, cogs: dict, listeners: dict) -> None:
    """Roll cogs/listeners back after a failed exec so the old env stays coherent."""
    session.client.cogs.clear()
    session.client.cogs.update(cogs)
    session.client._listeners.clear()
    session.client._listeners.update(listeners)


async def _do_reload(session: Session, code: str, run_main: bool) -> None:
    """Exec `code` into a fresh namespace and swap session.env; keep the timeline.

    On any exec failure the previous session.env stays in place, so the running
    bot and its timeline survive a broken edit. Workspace sessions reload
    through the same import machinery as Run, so a watcher reload of a file
    that imports helpers resolves them instead of failing.
    """
    await _stop_main(session)  # a running main() belongs to the old module
    env = _build_env(session)
    buffer = io.StringIO()
    started = time.perf_counter()
    rel_file = getattr(session, "workspace_file", None)  # file-mode reload (Run File / watcher)
    cogs_before = dict(session.client.cogs)
    listeners_before = {name: list(hooks) for name, hooks in session.client._listeners.items()}
    previous_trace = sys.gettrace()
    sys.settrace(_script_trace(getattr(session, "user_deadline", time.monotonic() + 20)))
    try:
        if session.workspace_root is not None:
            _run_entry_module(session, env, buffer, code, rel_file)
        else:
            with contextlib.redirect_stdout(buffer):
                exec(compile(code, "<playground>", "exec"), env)  # noqa: S102 - the whole point
    except BaseException as error:  # noqa: BLE001 - user code may raise anything, incl. SystemExit
        _flush_stdout(buffer, session)
        _restore_cogs(session, cogs_before, listeners_before)
        tb = traceback.format_exc(limit=6)
        session.log("💥", tb, "error")
        session.last_run = {"ok": False, "error": tb, "exception": _script_error_details(error),
                            "ms": (time.perf_counter() - started) * 1000}
        return  # previous session.env untouched
    finally:
        sys.settrace(previous_trace)
    _flush_stdout(buffer, session)

    try:
        await _scan_setup(session, env)
    except BaseException as error:  # noqa: BLE001 - setup() is user code too
        _restore_cogs(session, cogs_before, listeners_before)
        tb = traceback.format_exc(limit=6)
        session.log("💥", tb, "error")
        session.last_run = {"ok": False, "error": tb, "exception": _script_error_details(error),
                            "ms": (time.perf_counter() - started) * 1000}
        return  # previous session.env untouched
    session.env = env
    _collect_commands(session, env)
    session.log("🔄", "script reloaded — timeline, channels, and events kept")
    main_fn = env.get("main")
    if run_main and main_fn is not None:
        began = asyncio.Event()
        session.main_task = session.runner.loop.create_task(
            _run_main(main_fn, session, began)
        )
        await began.wait()
        if not (session.last_run and not session.last_run["ok"]):
            session.last_run = {"ok": True, "error": None, "ms": (time.perf_counter() - started) * 1000}
            session.log("✅", "main() re-run complete.")
    else:
        if main_fn is None:
            session.log("ℹ️", "Reloaded; define `async def main():` to boot startup logic.")
        session.last_run = {"ok": True, "error": None, "ms": (time.perf_counter() - started) * 1000}


async def reload_script(session: Session, code: str, *, run_main: bool = False,
                        timeout: float = 20.0) -> dict:
    """Live-reload: same exec path as Run, but nothing is cleared.

    Handlers are looked up from session.env on every dispatch, so messages
    already on the timeline route to the newly loaded callbacks.
    """
    if len(code) > 1_000_000:
        message = "Script is too large (maximum 1 MB)."
        session.log("⚠️", message, "error")
        session.last_run = {"ok": False, "error": message,
                            "exception": {"type": "ScriptLimitError", "message": message,
                                          "file": None, "line": None}, "ms": 0.0}
        return {"ok": False, "ms": 0.0}
    started = time.perf_counter()
    session.user_deadline = time.monotonic() + timeout
    try:
        await session.runner.run(
            lambda: _gated(session, lambda: _do_reload(session, code, run_main)), timeout)
    except ScriptStuck as exc:
        session.log("🛑", str(exc), "error")
        session.last_run = {"ok": False, "error": str(exc), "exception": _script_error_details(exc),
                            "ms": (time.perf_counter() - started) * 1000}
    return {"ok": bool(session.last_run and session.last_run["ok"]),
            "ms": round(session.last_run["ms"], 1) if session.last_run else 0.0}


async def _dispatch(session: Session, callback, timeout: float) -> dict:
    started = time.perf_counter()
    token = _CURRENT_SESSION.set(session)
    try:
        await session.runner.run(lambda: _gated(session, callback), timeout)
    except ScriptStuck as error:
        message = str(error)
        session.log("🛑", message, "error", details={
            "operation": "script.callback", "status": "script_error",
            "type": type(error).__name__, "message": message,
        })
        session.last_run = {
            "ok": False, "error": message,
            "exception": {"type": type(error).__name__, "message": message,
                          "file": None, "line": None},
            "ms": (time.perf_counter() - started) * 1000,
        }
    except Exception as error:  # noqa: BLE001 - report callback errors to the workbench
        _record_script_error(session, error, started)
    finally:
        _CURRENT_SESSION.reset(token)
    return state(session)


async def dispatch_click(session: Session, message_id: str, custom_id: str,
                         values: list, timeout: float = 10.0) -> dict:
    return await _dispatch(session, lambda: _do_click(session, message_id, custom_id, values), timeout)


async def dispatch_submit(session: Session, modal_id: str, values: dict,
                          timeout: float = 10.0) -> dict:
    return await _dispatch(session, lambda: _do_submit(session, modal_id, values), timeout)


async def dispatch_message(session: Session, content: str, channel_id=None,
                           timeout: float = 10.0, files: list[dict] | None = None) -> dict:
    return await _dispatch(session, lambda: _do_message(session, content, channel_id, files), timeout)


async def dispatch_command(session: Session, name: str, args: dict, channel_id=None,
                           timeout: float = 10.0) -> dict:
    return await _dispatch(session, lambda: _do_command(session, name, args, channel_id), timeout)


# --------------------------------------------------------------- state


def _members_json(session: Session) -> dict:
    names = {str(m.id): m.display_name for m in session.guild.members}
    names.update({f"&{r.id}": r.name for r in session.guild.roles})
    names.update({f"#{c.id}": c.name for c in session.channels.values()})
    return names


def _member_details_json(session: Session) -> list[dict]:
    details = []
    for member in session.guild.members:
        role = next((item for item in reversed(member.roles) if item.name != "@everyone"), None)
        color = getattr(getattr(role, "color", None), "value", None)
        status = getattr(member.status, "name", str(member.status))
        details.append({
            "id": str(member.id),
            "name": member.display_name,
            "username": member.name,
            "display_name": member.display_name,
            "bio": member.bio,
            "avatar_url": member.avatar_url,
            "banner_url": member.banner_url,
            "accent_color": member.accent_color,
            "custom": member.custom,
            "bot": member.bot,
            "status": status,
            "banned": member.banned,
            "timed_out": bool(member.timeout_until and member.timeout_until > datetime.now(timezone.utc)),
            "role": role.name if role else None,
            "role_color": f"#{color:06x}" if color is not None else None,
        })
    return details


def state(session: Session) -> dict:
    msgs = []
    for mid in session.order:
        msg = {k: v for k, v in session.messages[mid].items() if k != "author_obj"}
        msgs.append(msg)
    permission_names = ("view_channel", "send_messages", "manage_messages", "manage_channels")
    events = session.events[-300:]
    permissions = {
        label: {
            name: {"allowed": allowed, "reason": reason}
            for name in permission_names
            for allowed, reason in [session.channel.permission_check(member, name)]
        }
        for label, member in (("user", session.active_user), ("bot", session.guild.me))
    }
    return {
        "ok": True,
        "sid": session.sid,
        "user": {"id": str(session.user_id), "name": session.active_user.display_name},
        "users": [{"id": str(member.id), "name": member.display_name,
                   "display_name": member.display_name, "username": member.name,
                   "custom": member.custom}
                  for member in session.guild.members if not member.bot],
        "guild": {"id": session.guild.id, "name": session.guild.name},
        "channel": {"id": str(session.channel.id), "name": session.channel.name,
                    "topic": session.channel.topic},
        "revision": session.revision,
        # ids as strings: 18-digit snowflakes lose precision as JS numbers
        "channels": [{"id": str(c.id), "name": c.name, "topic": c.topic}
                     for c in session.channels.values()],
        "bot": {"id": session.guild.me.id, "name": session.guild.me.name},
        "permissions": permissions,
        "members": _members_json(session),
        "member_details": _member_details_json(session),
        "messages": [msg for msg in msgs if not msg.get("ephemeral")
                     or msg.get("ephemeral_user_id") == str(session.active_user.id)],
        "modals": [modal for modal in session.modals
                   if modal.get("user_id") in (None, str(session.active_user.id))],
        "commands": session.commands,
        "events": events,
        "last_run": session.last_run,
        "running": session.env is not None,
        "voice": {"channel": str(session.voice_channel) if session.voice_channel is not None else None,
                  "name": (session.channels.get(str(session.voice_channel)).name
                           if session.voice_channel is not None else None),
                  "self_mute": session.voice_self_mute,
                  "self_deaf": session.voice_self_deaf,
                  "speaking": [str(member_id) for member_id in session.voice_speaking]},
        "uploads": session.uploads,
        "banned": [{"id": str(member_id), "name": name} for member_id, name in session.banned.items()],
    }


_SCENARIO_PROFILE_FIELDS = {
    "username", "display_name", "bio", "avatar_url", "banner_url", "accent_color", "status",
}
_SCENARIO_PERMISSION_FIELDS = frozenset(discord.Permissions.VALID_FLAGS)
_SCENARIO_ACTIONS = {
    "message": {"action", "content", "as", "channel"},
    "click": {"action", "message_id", "custom_id", "values", "as"},
    "submit": {"action", "modal_id", "values", "as"},
    "command": {"action", "name", "args", "channel", "as"},
    "profile": {"action", "user", "profile"},
    "roles": {"action", "user", "add", "remove"},
    "permissions": {"action", "channel", "target", "overwrites"},
}


_SCENARIO_ASSERTIONS = {
    "message_exists": {"assert", "message_id", "content", "channel"},
    "content": {"assert", "message_id", "equals"},
    "embed_field": {"assert", "message_id", "embed", "name", "value"},
    "embed_description": {"assert", "message_id", "embed", "description"},
    "component_exists": {"assert", "message_id", "custom_id"},
    "member_profile": {"assert", "user", *_SCENARIO_PROFILE_FIELDS},
    "channel_exists": {"assert", "name"},
    "channel_missing": {"assert", "name"},
    "event_occurred": {"assert", "text", "interaction", "operation", "custom_id", "actor", "status"},
}


def validate_scenario(scenario: dict) -> dict:
    """Validate and return the small, versioned JSON scenario format."""
    if not isinstance(scenario, dict) or set(scenario) != {"version", "name", "steps"}:
        raise ValueError("scenario must contain exactly version, name, and steps")
    if type(scenario["version"]) is not int or scenario["version"] != 1:
        raise ValueError("scenario version must be 1")
    name = scenario["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 50 \
            or not re.fullmatch(r"[A-Za-z0-9_ -]{1,50}", name.strip()):
        raise ValueError("scenario name must use letters, numbers, spaces, dashes, or underscores (max 50)")
    steps = scenario["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ValueError("scenario steps must be an array containing 1 to 100 steps")
    normalized = []
    for index, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            raise TypeError(f"step {index} must be an object")
        discriminator = raw.get("action")
        if discriminator is not None:
            allowed = _SCENARIO_ACTIONS.get(discriminator)
            if allowed is None:
                raise ValueError(f"step {index}: unsupported action {discriminator!r}")
            if set(raw) - allowed:
                raise ValueError(f"step {index}: unknown action fields: {', '.join(sorted(set(raw) - allowed))}")
            required = {
                "message": ("content",), "click": ("message_id", "custom_id"),
                "submit": ("modal_id",), "command": ("name",),
                "profile": ("user", "profile"), "roles": ("user",),
                "permissions": ("channel", "target", "overwrites"),
            }[discriminator]
            if any(key not in raw for key in required):
                raise ValueError(f"step {index}: {discriminator} requires {', '.join(required)}")
            for key in ("content", "message_id", "custom_id", "modal_id", "name"):
                if key in raw and (not isinstance(raw[key], str) or not raw[key].strip()):
                    raise ValueError(f"step {index}: {key} must be a non-empty string")
            if discriminator == "click" and "values" in raw and not isinstance(raw["values"], list):
                raise ValueError(f"step {index}: click values must be an array")
            if discriminator == "submit" and "values" in raw and not isinstance(raw["values"], dict):
                raise ValueError(f"step {index}: submit values must be an object")
            if "args" in raw and not isinstance(raw["args"], dict):
                raise ValueError(f"step {index}: args must be an object")
            if "channel" in raw and discriminator in {"message", "command", "permissions"} \
                    and (not isinstance(raw["channel"], str) or not raw["channel"].strip()):
                raise ValueError(f"step {index}: channel must be a channel name or ID")
            if discriminator == "roles":
                if not any(raw.get(key) for key in ("add", "remove")):
                    raise ValueError(f"step {index}: roles needs a non-empty add or remove list")
                for key in ("add", "remove"):
                    if key in raw and (not isinstance(raw[key], list) or any(
                        isinstance(role, bool) or not isinstance(role, (str, int))
                        or isinstance(role, str) and not role.strip() for role in raw[key]
                    )):
                        raise ValueError(f"step {index}: roles.{key} must be an array of role names or IDs")
            if discriminator == "permissions":
                overwrites = raw["overwrites"]
                if not isinstance(overwrites, dict):
                    raise ValueError(f"step {index}: overwrites must be an object")
                if set(overwrites) - _SCENARIO_PERMISSION_FIELDS:
                    raise ValueError(f"step {index}: overwrites contains an unknown permission")
                if any(value is not None and type(value) is not bool for value in overwrites.values()):
                    raise ValueError(f"step {index}: overwrite values must be true, false, or null")
            if discriminator == "profile":
                profile = raw["profile"]
                if not isinstance(profile, dict) or not profile:
                    raise ValueError(f"step {index}: profile must be a non-empty object")
                if set(profile) - _SCENARIO_PROFILE_FIELDS:
                    raise ValueError(f"step {index}: profile contains unsupported fields")
                for key, value in profile.items():
                    if value is None and key in {"avatar_url", "banner_url", "accent_color"}:
                        continue
                    if not isinstance(value, str):
                        # ValueError by contract: the HTTP layer maps it to a 400.
                        raise ValueError(f"step {index}: profile.{key} must be text")  # noqa: TRY004
            for key in ("as", "channel", "user", "target"):
                if key in raw and (isinstance(raw[key], bool) or not isinstance(raw[key], (str, int))
                                   or isinstance(raw[key], str) and not raw[key].strip()):
                    raise ValueError(f"step {index}: {key} must be a user, role, or channel name or ID")
            if any(key in raw for key in ("message_id", "modal_id")):
                reference = raw.get("message_id", raw.get("modal_id"))
                if not isinstance(reference, str) or not reference.strip():
                    raise ValueError(f"step {index}: target ID must be a non-empty string or 'latest'")
            step = dict(raw)
        else:
            assertion = raw.get("assert")
            allowed = _SCENARIO_ASSERTIONS.get(assertion)
            if allowed is None:
                raise ValueError(f"step {index}: unsupported assertion {assertion!r}")
            if set(raw) - allowed:
                raise ValueError(f"step {index}: unknown assertion fields: {', '.join(sorted(set(raw) - allowed))}")
            required = {
                "message_exists": (), "content": ("equals",), "embed_field": ("name", "value"),
                "embed_description": ("description",), "member_profile": ("user",),
                "component_exists": ("custom_id",), "channel_exists": ("name",),
                "channel_missing": ("name",), "event_occurred": (),
            }[assertion]
            if any(key not in raw for key in required):
                raise ValueError(f"step {index}: {assertion} requires {', '.join(required)}")
            if assertion == "event_occurred" and not any(
                raw.get(key) not in (None, "") for key in ("text", "interaction", "operation", "custom_id")
            ):
                raise ValueError(f"step {index}: event_occurred needs text, interaction, operation, or custom_id")
            if "user" in raw and (isinstance(raw["user"], bool) or not isinstance(raw["user"], (str, int))
                                   or isinstance(raw["user"], str) and not raw["user"].strip()):
                raise ValueError(f"step {index}: user must be a simulated user name or ID")
            if assertion == "event_occurred" and "actor" in raw and (
                isinstance(raw["actor"], bool) or not isinstance(raw["actor"], (str, int))
                or isinstance(raw["actor"], str) and not raw["actor"].strip()
            ):
                raise ValueError(f"step {index}: actor must be a simulated user name or ID")
            if assertion == "member_profile":
                fields = set(raw) & _SCENARIO_PROFILE_FIELDS
                if not fields:
                    raise ValueError(f"step {index}: member_profile needs at least one profile field")
            if "message_id" in raw and not isinstance(raw["message_id"], str):
                raise TypeError(f"step {index}: message_id must be a string")
            if "message_id" in raw and (not isinstance(raw["message_id"], str) or not raw["message_id"].strip()):
                raise ValueError(f"step {index}: message_id must be 'latest' or an ID")
            if "channel" in raw and (not isinstance(raw["channel"], str) or not raw["channel"].strip()):
                raise ValueError(f"step {index}: channel must be a channel name or ID")
            for key in ("message_id", "content", "equals", "name", "value", "custom_id", "text", "interaction", "operation", "status", "description"):
                if key in raw and not isinstance(raw[key], str):
                    raise ValueError(f"step {index}: {key} must be a string")
            if assertion == "member_profile":
                for key in set(raw) & _SCENARIO_PROFILE_FIELDS:
                    if raw[key] is None and key in {"avatar_url", "banner_url", "accent_color"}:
                        continue
                    if not isinstance(raw[key], str):
                        # ValueError by contract: the HTTP layer maps it to a 400.
                        raise ValueError(f"step {index}: {key} must be text")  # noqa: TRY004
            if "embed" in raw and (type(raw["embed"]) is not int or raw["embed"] < 0):
                # ValueError by contract: the HTTP layer maps it to a 400.
                raise ValueError(f"step {index}: embed must be a zero-based non-negative index")
            step = dict(raw)
            step["assert"] = assertion
        normalized.append(step)
    return {"version": 1, "name": name.strip(), "steps": normalized}


def _scenario_channel(session: Session, reference):
    if reference is None:
        return session.channel
    channel = session.channels.get(str(reference)) or next(
        (item for item in session.channels.values() if item.name.casefold() == str(reference).casefold()), None
    )
    if channel is None:
        raise ValueError(f"channel {reference!r} does not exist")
    return channel


def _scenario_member(session: Session, reference, *, allow_bot: bool = False) -> MockMember:
    if isinstance(reference, int) or str(reference).isdigit():
        user = session.guild.get_member(int(reference))
    else:
        name = str(reference).casefold()
        user = next((member for member in session.guild.members if member.name.casefold() == name), None)
        if user is None:
            user = next((member for member in session.guild.members
                         if member.display_name.casefold() == name), None)
    if user is None or user.bot and not allow_bot:
        raise ValueError(f"simulated user {reference!r} does not exist")
    return user


def _scenario_role(session: Session, reference) -> MockRole:
    if isinstance(reference, int) or str(reference).isdigit():
        role = session.guild.get_role(int(reference))
    else:
        name = str(reference).casefold()
        role = next((item for item in session.guild.roles if item.name.casefold() == name), None)
    if role is None:
        raise ValueError(f"simulated role {reference!r} does not exist")
    return role


def _scenario_permission_target(session: Session, reference) -> tuple[int, str, str]:
    if isinstance(reference, int) or str(reference).isdigit():
        role = session.guild.get_role(int(reference))
    else:
        name = str(reference).casefold()
        role = next((item for item in session.guild.roles if item.name.casefold() == name), None)
    if role is not None:
        return role.id, role.name, "role"
    member = _scenario_member(session, reference, allow_bot=True)
    return member.id, member.display_name, "member"


def _scenario_user(session: Session, reference) -> None:
    if reference is not None:
        session.set_user(_scenario_member(session, reference).id)


def _scenario_message(session: Session, reference="latest") -> dict | None:
    message_id = session.order[-1] if reference == "latest" and session.order else reference
    message = session.messages.get(str(message_id)) if message_id is not None else None
    return message if message and not message.get("deleted") else None


def _scenario_component_ids(tree):
    if isinstance(tree, dict):
        if tree.get("custom_id"):
            yield tree["custom_id"]
        for value in tree.values():
            yield from _scenario_component_ids(value)
    elif isinstance(tree, list):
        for value in tree:
            yield from _scenario_component_ids(value)


def _scenario_component(tree, custom_id: str) -> dict | None:
    if isinstance(tree, dict):
        if tree.get("custom_id") == custom_id:
            return tree
        for value in tree.values():
            component = _scenario_component(value, custom_id)
            if component is not None:
                return component
    elif isinstance(tree, list):
        for value in tree:
            component = _scenario_component(value, custom_id)
            if component is not None:
                return component
    return None


def _scenario_event_value(event: dict, key: str):
    details = event.get("details") or {}
    if key in details:
        return details[key]
    interaction = details.get("interaction")
    operation = details.get("operation")
    if key == "interaction":
        return {"interaction.component": "component", "interaction.command": "application_command",
                "interaction.modal_submit": "modal_submit", "message.send": "message_create"}.get(operation)
    if key == "operation":
        return {"component": "interaction.component", "application_command": "interaction.command",
                "modal_submit": "interaction.modal_submit", "message_create": "message.send"}.get(interaction)
    return None


def _scenario_assertion(session: Session, step: dict) -> tuple[bool, str, str]:
    assertion = step["assert"]
    message = _scenario_message(session, step.get("message_id", "latest"))
    if assertion == "message_exists":
        matches = [session.messages[mid] for mid in session.order if mid in session.messages]
        if step.get("message_id", "latest") == "latest":
            matches = matches[-1:]
        else:
            matches = [candidate for candidate in matches if candidate.get("id") == step["message_id"]]
        if "content" in step:
            matches = [candidate for candidate in matches if step["content"] in candidate.get("content", "")]
        if "channel" in step:
            channel = _scenario_channel(session, step["channel"])
            matches = [candidate for candidate in matches if candidate.get("channel") == str(channel.id)]

        actual = ", ".join(f"{item['id']}: {item['content'][:80]!r}" for item in matches) or "no matching message"
        return bool(matches), "a matching message exists", actual
    if assertion == "content":
        actual = message.get("content", "") if message else "message not found"
        return bool(message and actual == step["equals"]), f"content equals {step['equals']!r}", repr(actual)
    if assertion == "embed_field":
        index = step.get("embed", 0)
        embeds = message.get("embeds", []) if message else []
        fields = embeds[index].get("fields", []) if index < len(embeds) else []
        found = next((field for field in fields if field.get("name") == step["name"]), None)
        actual = found.get("value") if found else "field not found"
        return bool(found and actual == step["value"]), f"embed[{index}] field {step['name']!r} equals {step['value']!r}", repr(actual)
    if assertion == "embed_description":
        index = step.get("embed", 0)
        embeds = message.get("embeds", []) if message else []
        actual = embeds[index].get("description") if index < len(embeds) else None
        return actual == step["description"], f"embed[{index}] description equals {step['description']!r}", repr(actual)
    if assertion == "member_profile":
        member = _scenario_member(session, step["user"])
        actual = {"username": member.name, "display_name": member.display_name,
                  "bio": member.bio, "avatar_url": member.avatar_url, "banner_url": member.banner_url,
                  "accent_color": member.accent_color, "status": getattr(member.status, "name", str(member.status))}
        expected = {key: step[key] for key in _SCENARIO_PROFILE_FIELDS if key in step}
        matched = all(
            actual[key].casefold() == value.casefold()
            if key in {"status", "accent_color"} and isinstance(actual[key], str) and isinstance(value, str)
            else actual[key] == value
            for key, value in expected.items()
        )
        return matched, f"{step['user']!r} profile matches {expected!r}", repr({key: actual[key] for key in expected})
    if assertion == "component_exists":
        present = bool(message and step["custom_id"] in list(_scenario_component_ids(
            [message.get("components"), message.get("v2")]
        )))
        actual = "present" if present else "component not found"
        return present, f"component {step['custom_id']!r} exists", actual
    if assertion in ("channel_exists", "channel_missing"):
        channel = next((item for item in session.channels.values()
                        if item.name.casefold() == step["name"].casefold() or str(item.id) == step["name"]), None)
        exists = channel is not None
        expected = assertion == "channel_exists"
        return exists is expected, f"channel {step['name']!r} {'exists' if expected else 'is missing'}", \
            "present" if exists else "missing"
    criteria = {key: step[key] for key in ("text", "interaction", "operation", "custom_id", "actor", "status") if key in step}
    actor_names = set()
    if "actor" in criteria:
        actor = _scenario_member(session, criteria["actor"])
        actor_names = {actor.name.casefold(), actor.display_name.casefold()}

    def matches_criterion(event: dict, key: str) -> bool:
        value = _scenario_event_value(event, key)
        if key == "text":
            return criteria[key].casefold() in event.get("text", "").casefold()
        if key == "actor":
            return isinstance(value, str) and value.casefold() in actor_names
        return criteria[key] == value

    matched = next((event for event in reversed(session.events) if event.get("kind") == "event" and all(
        matches_criterion(event, key) for key in criteria
    )), None)
    return bool(matched), f"event occurred matching {criteria!r}", \
        f"{matched.get('id')}: {matched.get('text')}" if matched else "no matching event"


def _scenario_runtime_state(session: Session) -> dict:
    return {
        "active_user": session.active_user.name,
        "profiles": _member_details_json(session),
        "channels": [{"id": str(channel.id), "name": channel.name} for channel in session.channels.values()],
        "messages": [{"id": mid, "channel": session.messages[mid].get("channel"),
                      "author": session.messages[mid].get("author", {}).get("name"),
                      "content": session.messages[mid].get("content", "")[:160]}
                     for mid in session.order if mid in session.messages],
        "last_run": session.last_run,
        "open_modals": [{"id": modal["id"], "title": modal["title"]} for modal in session.modals],
        "recent_events": session.events[-8:],
    }


async def run_scenario(session: Session, scenario: dict, runtime=None) -> dict:
    """Replay a validated sequence through the script or hosted-project runtime."""
    definition = validate_scenario(scenario)
    results = []
    for index, step in enumerate(definition["steps"], 1):
        kind = "action" if "action" in step else "assertion"
        label = step.get("action", step.get("assert"))
        try:
            if kind == "assertion":
                passed, expected, actual = _scenario_assertion(session, step)
                result = {"step": index, "kind": kind, "label": label,
                          "passed": passed, "expected": expected, "actual": actual}
            else:
                extra = {}
                event_count = len(session.events)
                if label == "profile":
                    member = _scenario_member(session, step["user"])
                    session.update_member_profile(member.id, step["profile"])
                    extra["user"] = member.name
                elif label == "roles":
                    member = _scenario_member(session, step["user"], allow_bot=True)
                    added = {role.id: role for role in
                             (_scenario_role(session, reference) for reference in step.get("add", []))}
                    removed = {role.id: role for role in
                               (_scenario_role(session, reference) for reference in step.get("remove", []))}
                    if session.guild.id in added or session.guild.id in removed:
                        raise ValueError("@everyone is assigned automatically and cannot be changed")
                    if added.keys() & removed.keys():
                        raise ValueError("the same role cannot be both added and removed")
                    roles = {role.id: role for role in member.roles if role.id != session.guild.id}
                    roles.update(added)
                    for role_id in removed:
                        roles.pop(role_id, None)
                    member.roles = [session.guild.roles[0], *sorted(roles.values(), key=lambda role: role.position)]
                    extra.update(user=member.name, added_roles=[role.name for role in added.values()],
                                 removed_roles=[role.name for role in removed.values()])
                    session._touch()
                    session.log("🎭", f"roles updated for {member.display_name}", kind="action",
                                details={"operation": "member.roles.update", "member": member.name,
                                         "added": extra["added_roles"], "removed": extra["removed_roles"],
                                         "status": "success"})
                elif label == "permissions":
                    channel = _scenario_channel(session, step["channel"])
                    target_id, target_name, target_type = _scenario_permission_target(session, step["target"])
                    changes = step["overwrites"]
                    if not changes:
                        channel.overwrites.pop(target_id, None)
                    else:
                        current = channel.overwrites.get(target_id)
                        overwrite = discord.PermissionOverwrite.from_pair(*current.pair()) \
                            if current is not None else discord.PermissionOverwrite()
                        for permission, value in changes.items():
                            setattr(overwrite, permission, value)
                        if overwrite.is_empty():
                            channel.overwrites.pop(target_id, None)
                        else:
                            channel.overwrites[target_id] = overwrite
                    session._touch()
                    session.log("🔐", f"permissions updated for {target_name} in #{channel.name}", kind="action",
                                details={"operation": "channel.permissions.update", "channel": channel.name,
                                         "target": target_name, "target_type": target_type,
                                         "overwrites": changes, "status": "success"})
                    extra.update(channel=channel.name, target=target_name)
                else:
                    _scenario_user(session, step.get("as"))
                    channel = _scenario_channel(session, step["channel"]) if "channel" in step else session.channel
                    if label == "submit" and step["modal_id"] == "latest":
                        matching = [modal for modal in session.modals
                                    if modal.get("user_id") == str(session.active_user.id)]
                        if not matching:
                            raise ValueError("no modal is open for the active simulated user")
                if label == "message":
                    message_count = len(session.order)
                    if runtime is None:
                        await dispatch_message(session, step["content"], channel_id=channel.id)
                    else:
                        await runtime.dispatch_message(step["content"], channel_id=channel.id)
                    user_message = next((event.get("details", {}).get("message_id")
                                         for event in session.events[event_count:]
                                         if event.get("details", {}).get("interaction") == "message_create"
                                         and event.get("details", {}).get("message_id")), None)
                    if not user_message and len(session.order) > message_count:
                        user_message = session.order[message_count]
                    if user_message:
                        extra["message_id"] = user_message
                elif label == "click":
                    message_id = step["message_id"]
                    if message_id == "latest":
                        message_id = next((mid for mid in reversed(session.order)
                                           if (component := _scenario_component(
                                               [session.messages[mid].get("components"), session.messages[mid].get("v2")],
                                               step["custom_id"],
                                           )) and not component.get("disabled")), None)
                    if message_id is None or str(message_id) not in session.messages:
                        raise ValueError(f"message {step['message_id']!r} with component {step['custom_id']!r} does not exist")
                    message = session.messages[str(message_id)]
                    if message.get("deleted"):
                        raise ValueError(f"message {message_id!r} was deleted")
                    component = _scenario_component(
                        [message.get("components"), message.get("v2")], step["custom_id"]
                    )
                    if component is None:
                        raise ValueError(f"component {step['custom_id']!r} is not on message {message_id!r}")
                    if component.get("disabled"):
                        raise ValueError(f"component {step['custom_id']!r} is disabled")
                    if runtime is None:
                        await dispatch_click(session, str(message_id), step["custom_id"], step.get("values", []))
                    else:
                        await runtime.dispatch_click(str(message_id), step["custom_id"], step.get("values", []))
                    extra["message_id"] = str(message_id)
                elif label == "submit":
                    modal = matching[-1] if step["modal_id"] == "latest" else next(
                        (item for item in session.modals if item["id"] == step["modal_id"]), None
                    )
                    if modal is None:
                        raise ValueError(f"modal {step['modal_id']!r} is not open")
                    modal_id = modal["id"]
                    if runtime is None:
                        await dispatch_submit(session, str(modal_id), step.get("values", {}))
                    elif not await runtime.dispatch_pending_modal(step.get("values", {}), str(modal_id)):
                        raise ValueError(f"modal {step['modal_id']!r} is not pending in the hosted bot")
                    extra["modal_id"] = str(modal_id)
                elif label == "command":
                    if runtime is None:
                        session.channel = channel
                        await dispatch_command(session, step["name"], step.get("args", {}))
                    else:
                        await runtime.dispatch_command(step["name"], step.get("args", {}), channel_id=channel.id)

                failures = [event for event in session.events[event_count:]
                            if event.get("details", {}).get("status") in {
                                "denied", "ephemeral_owner_only", "missing_handler", "missing_command",
                                "missing_arguments", "invalid_component", "invalid_message", "invalid_form",
                                "invalid_modal", "missing_message", "missing_modal", "unanswered",
                                "missing_channel", "blocked_last_channel", "transport_gap", "script_error",
                            }]
                passed = not failures
                actual = failures[-1]["text"] if failures else (
                    f"updated profile for {extra['user']}" if label == "profile" else
                    f"updated roles for {extra['user']}" if label == "roles" else
                    f"updated permissions for {extra['target']} in #{extra['channel']}"
                    if label == "permissions" else f"dispatched {label}"
                )
                if runtime is not None and label in {"profile", "roles", "permissions"}:
                    refresh = getattr(runtime, "refresh_guild_state", None)
                    if not callable(refresh) and label == "profile":
                        refresh = getattr(runtime, "refresh_member_profile", None)
                    if callable(refresh):
                        refresh()
                result = {"step": index, "kind": kind, "label": label, "passed": passed,
                          "expected": "scenario state update succeeds" if label in {"profile", "roles", "permissions"} else
                          "dispatch completes without permission denial, invalid input/target, missing handler, or script/runtime error",
                          "actual": actual, **extra}
        except Exception as error:  # noqa: BLE001 - scenario output needs a failing step, not a 500
            result = {"step": index, "kind": kind, "label": label, "passed": False,
                      "expected": f"{kind} {label} succeeds", "actual": f"{type(error).__name__}: {error}"}
        results.append(result)
        if not result["passed"]:
            return {"ok": False, "name": definition["name"], "failed_step": index,
                    "results": results, "runtime_state": _scenario_runtime_state(session)}
    return {"ok": True, "name": definition["name"], "failed_step": None,
            "results": results, "runtime_state": _scenario_runtime_state(session)}
