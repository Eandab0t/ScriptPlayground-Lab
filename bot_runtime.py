"""Offline project runtime for ScriptPlayground.

Runs real, multi-file `discord.py` bot projects (commands.Bot, cogs, slash
commands, ui Views/Modals, tasks) inside the simulator with zero network
contact. Strategy:

* `HTTPClient.request` and `AsyncWebhookAdapter.request` are intercepted once
  (globally) and served by a per-boot `ProjectTransport` that answers every
  Discord REST route from the mock session world.
* `Client.login` stays ORIGINAL — so `static_login` → `application_info` →
  `setup_hook` (cog loading, tree sync) all run exactly like production.
* `Client.connect` is a no-op (no gateway websocket), `Client.run` is a no-op
  (module-level `bot.run(TOKEN)` can't block the import).
* READY + GUILD_CREATE payloads are injected through the real
  `ConnectionState` parsers, so `on_ready`/`on_guild_join` fire for real.
* Interactions (slash commands, component clicks, modal submits) and user
  messages are injected as raw gateway payloads through
  `parse_interaction_create` / `parse_message_create`.

Wire↔session mapping: playground message ids are "m<N>"; Discord ids must be
18-digit ints (view-store keys, snowflake parsing), so every outgoing payload
carries a synthetic wire id and inbound references are mapped back.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import uuid
from pathlib import Path
from typing import Any, Callable

import discord
from discord.http import HTTPClient, Route
import discord.webhook.async_ as webhook_async

import playground as pg
from playground import MockRole

log = logging.getLogger(__name__)

# Playground world ids (mirror playground.py)
USER_ID = pg.USER_ID
BOT_ID = pg.BOT_ID
GUILD_ID = pg.GUILD_ID
CHANNEL_ID = pg.CHANNEL_ID
MEMBER_IDS = pg.MEMBER_IDS

_PLACEHOLDER_TOKEN = "offline-simulated-token"
_TOKEN_KEYS = ("DISCORD_TOKEN", "BOT_TOKEN", "DISCORD_BOT_TOKEN", "TOKEN")
_SECRET_NAMES = {".env"}
# Source-ish text files get the token scrub; binaries (.db, images) are copied as-is.
_TEXT_SUFFIXES = {".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".json", ".txt", ".md",
                  ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh", ".bat", ".ps1",
                  ".html", ".css", ".env", ".example", ".cfg"}
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "browser-profile", ".venv", "venv", "backups"}
_SANDBOX_ROOT = Path(
    os.getenv("SCRIPTPLAYGROUND_SANDBOX", Path(tempfile.gettempdir()) / "scriptplayground-sandbox")
)
_ENTRY_NAMES = ("main.py", "bot.py", "index.py", "run.py", "app.py")

# "m5" ↔ 18-digit wire id (below every real-looking snowflake used here)
_WIRE_BASE = 600_000_000_000_000_000


def _wire_message_id(session_id: str) -> str:
    n = int(str(session_id).lstrip("m"))
    return str(_WIRE_BASE + n)


def _session_message_id(wire_id: str) -> str | None:
    if not wire_id.isdigit():
        return None
    n = int(wire_id) - _WIRE_BASE
    return f"m{n}" if n > 0 else None


# ------------------------------------------------------------- registries

_ACTIVE_BOOT: contextvars.ContextVar["ProjectRuntime | None"] = contextvars.ContextVar(
    "playground_active_boot", default=None
)
_HTTP_RUNTIMES: dict[int, "ProjectRuntime"] = {}     # id(client.http) -> runtime
_SESSION_RUNTIMES: dict[int, "ProjectRuntime"] = {}  # id(aiohttp session) -> runtime
_BOOT_LOCK = asyncio.Lock()  # one project boot per process (module cache is shared)

_ORIGINAL_REQUEST = HTTPClient.request
_ORIGINAL_STATIC_LOGIN = HTTPClient.static_login
_ORIGINAL_CLIENT_INIT = discord.Client.__init__
_ORIGINAL_ADAPTER_REQUEST = webhook_async.AsyncWebhookAdapter.request
_PATCHED = False


def _http_session(http: HTTPClient):
    """The lazily-created aiohttp session (mangled attr), or None."""
    return getattr(http, "_HTTPClient__session", None)


def _ensure_patches() -> None:
    """Install the global interceptors exactly once."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    def client_init_shim(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _ORIGINAL_CLIENT_INIT(self, *args, **kwargs)
        runtime = _ACTIVE_BOOT.get()
        if runtime is not None:
            _HTTP_RUNTIMES[id(self.http)] = runtime
            with contextlib.suppress(Exception):
                runtime.clients.append(self)

    async def request_shim(self_http, route, *, files=None, **kwargs):  # type: ignore[no-untyped-def]
        runtime = _HTTP_RUNTIMES.get(id(self_http))
        if runtime is None:
            return await _ORIGINAL_REQUEST(self_http, route, files=files, **kwargs)
        return runtime.transport.handle(
            route, payload=kwargs.get("json"), files=files, params=kwargs.get("params")
        )

    async def static_login_shim(self_http, token):  # type: ignore[no-untyped-def]
        """Serve GET /users/@me locally; keep token + session bookkeeping real."""
        self_http.token = _PLACEHOLDER_TOKEN
        if _http_session(self_http) is None:
            import aiohttp

            self_http._HTTPClient__session = aiohttp.ClientSession()
        runtime = _HTTP_RUNTIMES.get(id(self_http))
        session = _http_session(self_http)
        if runtime is not None and session is not None:
            _SESSION_RUNTIMES[id(session)] = runtime
        if runtime is None:
            raise RuntimeError("login outside a project boot is not supported offline")
        route = Route("GET", "/users/@me")
        return runtime.transport.handle(route, payload=None, files=None, params=None)

    async def connect_shim(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # No gateway websocket, ever — but block like a live connection so
        # `await bot.start(token)` doesn't return and trigger cleanup paths.
        runtime = _HTTP_RUNTIMES.get(id(self.http)) or _ACTIVE_BOOT.get()
        if runtime is not None:
            await runtime._connected.wait()
        return None

    def run_shim(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Module-level `bot.run(TOKEN)` must not block the import; boot() finds
        # the client afterwards and drives it on the session loop.
        return None

    async def change_presence_shim(self, *, activity=None, status=None, activities=None):  # type: ignore[no-untyped-def]
        runtime = _HTTP_RUNTIMES.get(id(self.http))
        if runtime is not None:
            label = activity.name if activity is not None else (status or "default")
            runtime.session.log("🪄", f"presence set: {label}", kind="action",
                                details={"operation": "change_presence", "activity": label, "status": "success"})

    async def chunk_shim(self, *, cache=True, **kwargs):  # type: ignore[no-untyped-def]
        # Members are pre-loaded from GUILD_CREATE; no gateway chunking needed.
        return list(self.members)

    async def adapter_request_shim(self_adapter, route, session=None, **kwargs):  # type: ignore[no-untyped-def]
        runtime = _SESSION_RUNTIMES.get(id(session)) if session is not None else None
        if runtime is None:
            return await _ORIGINAL_ADAPTER_REQUEST(self_adapter, route, session=session, **kwargs)
        return runtime.transport.handle(
            route, payload=kwargs.get("payload"), files=kwargs.get("files"), params=kwargs.get("params")
        )

    discord.Client.__init__ = client_init_shim  # type: ignore[method-assign]
    HTTPClient.request = request_shim  # type: ignore[method-assign]
    HTTPClient.static_login = static_login_shim  # type: ignore[method-assign]
    discord.Client.connect = connect_shim  # type: ignore[method-assign]
    discord.Client.run = run_shim  # type: ignore[method-assign]
    discord.Client.change_presence = change_presence_shim  # type: ignore[method-assign]
    discord.Guild.chunk = chunk_shim  # type: ignore[method-assign]
    webhook_async.AsyncWebhookAdapter.request = adapter_request_shim  # type: ignore[method-assign]


# ------------------------------------------------------------- sandbox copy


def _scrub_secret_text(text: str) -> str:
    """Neutralize token values while keeping the surrounding syntax valid."""
    keys = "|".join(_TOKEN_KEYS)
    placeholder = _PLACEHOLDER_TOKEN
    # 1) Whole-line `KEY: = "value"` — keep the quote style so Python/JSON/YAML stay parseable.
    text = re.sub(
        rf"(?m)^(\s*(?:{keys})\b\s*[:=]\s*)([\"'])[^\"']*\2",
        lambda m: m.group(1) + m.group(2) + placeholder + m.group(2),
        text,
    )
    # 2) Whole-line bare literal (.env style): KEY=value — but never rewrite
    # code like `TOKEN = os.getenv("DISCORD_TOKEN")`; only simple literals.
    text = re.sub(
        rf"(?m)^(\s*(?:{keys})\b\s*[:=])\s*[\w.\-+/]{{4,}}\s*$",
        r"\1 " + placeholder,
        text,
    )
    # 3) Inline quoted assignment anywhere: os.environ.setdefault("TOKEN", "…"), {"TOKEN": "…"}
    text = re.sub(
        rf"\b((?:{keys})\b)\s*([:=])\s*([\"'])[^\"']{{10,}}\3",
        lambda m: m.group(1) + m.group(2) + m.group(3) + placeholder + m.group(3),
        text,
    )
    return text


def _copy_workspace(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in _SKIP_DIRS or item.name.endswith(".pyc"):
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*_SKIP_DIRS, "*.pyc"))
        elif item.is_file() and (item.name in _SECRET_NAMES
                                 or item.suffix.lower() in _TEXT_SUFFIXES
                                 or item.name.startswith(".")):
            # Text files are scrubbed, never copied verbatim: bots frequently
            # hardcode live tokens right in the source.
            try:
                target.write_text(_scrub_secret_text(item.read_text(encoding="utf-8")), encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                shutil.copy2(item, target)  # binary-ish despite the name: copy untouched
        elif item.is_file():
            shutil.copy2(item, target)


def _make_sandbox(workspace: Path, tag: str) -> Path:
    sandbox = _SANDBOX_ROOT / f"{workspace.name}-{tag}"
    if sandbox.exists():
        shutil.rmtree(sandbox, ignore_errors=True)
    _copy_workspace(workspace, sandbox)
    (sandbox / ".env").write_text(
        "".join(f"{key}={_PLACEHOLDER_TOKEN}\n" for key in _TOKEN_KEYS), encoding="utf-8"
    )
    return sandbox


def _find_entry(root: Path) -> Path | None:
    for name in _ENTRY_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    top = sorted(
        (p for p in root.glob("*.py") if p.is_file() and not p.name.startswith(("_", "."))),
        key=lambda p: (-p.stat().st_size, p.name.lower()),
    )
    return top[0] if top else None


def _purge_sandbox_modules() -> None:
    """Drop cached modules from any sandbox (fresh boot must re-import).

    Namespace packages have __file__ = None, so their __path__ entries are
    checked too — otherwise a stale package would keep importing its siblings
    from a deleted sandbox of a previous boot.
    """
    for name, module in list(sys.modules.items()):
        file = str(getattr(module, "__file__", "") or "")
        paths = "".join(str(p) for p in (getattr(module, "__path__", None) or []))
        if "scriptplayground-sandbox" in (file + paths + name):
            sys.modules.pop(name, None)


def _neutralize_keep_alive() -> None:
    """Flask dev servers must never listen while simulating a bot."""
    try:
        from flask import Flask
    except ImportError:
        return
    if getattr(Flask, "_playground_patched", False):
        return

    def _quiet_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        log.debug("Flask.run suppressed in simulator")
        return None

    Flask.run = _quiet_run  # type: ignore[method-assign]
    Flask._playground_patched = True  # type: ignore[attr-defined]


def _install_psutil_stub() -> None:
    """Tiny psutil stand-in only when the real package is missing."""
    if "psutil" in sys.modules:
        return
    try:
        import psutil  # noqa: F401
        return
    except ImportError:
        pass
    memory = types.SimpleNamespace(rss=0, vms=0)

    class _Process:
        def memory_info(self):
            return memory

        def memory_percent(self):
            return 0.0

        def cpu_percent(self, interval=None):
            return 0.0

    stub = types.ModuleType("psutil")
    stub.Process = _Process
    stub.virtual_memory = lambda **kwargs: memory
    stub.disk_usage = lambda *a, **k: types.SimpleNamespace(free=10**12, total=2 * 10**12, used=10**11)
    stub.cpu_count = lambda **kwargs: 1
    sys.modules["psutil"] = stub


# ------------------------------------------------------------- view revival


def _revive_classic_view(components: list[dict] | None) -> discord.ui.View | None:
    """Rebuild a dispatchable View from wire component rows (buttons/selects)."""
    if not components:
        return None
    view = discord.ui.View(timeout=None)
    for row_index, entry in enumerate(components):
        if entry.get("type") != 1:
            continue
        row = min(row_index, 4)
        for item in entry.get("components") or []:
            kind = item.get("type")
            custom_id = item.get("custom_id")
            if kind == 2 and custom_id:  # button
                emoji = item.get("emoji")
                if isinstance(emoji, dict):
                    emoji = emoji.get("name")
                view.add_item(discord.ui.Button(
                    style=discord.ButtonStyle(int(item.get("style") or 1)),
                    label=item.get("label"), custom_id=custom_id,
                    disabled=bool(item.get("disabled")), emoji=emoji, row=row,
                ))
            elif kind == 3 and custom_id:  # string select
                view.add_item(discord.ui.Select(
                    placeholder=item.get("placeholder"), custom_id=custom_id,
                    min_values=int(item.get("min_values") or 1),
                    max_values=int(item.get("max_values") or 1),
                    options=[discord.SelectOption(label=o.get("label"), value=o.get("value"))
                             for o in (item.get("options") or [])],
                    row=row,
                ))
    return view if view.children else None


def _embeds_from_payload(raw) -> list[discord.Embed] | None:
    """Wire embed dicts → discord.Embed objects the session serializer accepts."""
    if not raw:
        return None
    out = []
    for item in raw:
        if isinstance(item, discord.Embed):
            out.append(item)
        elif isinstance(item, dict):
            with contextlib.suppress(Exception):
                out.append(discord.Embed.from_dict(item))
    return out or None


# ------------------------------------------------------------- transport


class ProjectTransport:
    """Answers every Discord REST route from the mock session world.

    Routes are matched by their *template* (`route.key` keeps `{placeholders}`),
    so a single entry per endpoint shape suffices. Handlers receive the
    populated regex match for extracting channel/message/member ids.
    """

    def __init__(self, runtime: "ProjectRuntime") -> None:
        self.runtime = runtime
        self._routes: dict[tuple[str, str], Callable] = {
            ("GET", "/users/@me"): self._get_users_at_me,
            ("PATCH", "/users/@me"): self._get_users_at_me,
            ("GET", "/users/@me/guilds"): self._get_users_guilds,
            ("POST", "/users/@me/channels"): self._post_dm_channel,
            ("GET", "/users/{user_id}"): self._get_user,
            ("GET", "/oauth2/applications/@me"): self._get_application,
            ("GET", "/voice/regions"): self._get_voice_regions,
            ("GET", "/guilds/{guild_id}"): self._get_guild,
            ("GET", "/guilds/{guild_id}/channels"): self._get_guild_channels,
            ("POST", "/guilds/{guild_id}/channels"): self._post_guild_channels,
            ("GET", "/guilds/{guild_id}/roles"): self._get_guild_roles,
            ("POST", "/guilds/{guild_id}/roles"): self._post_guild_roles,
            ("PATCH", "/guilds/{guild_id}/roles/{role_id}"): self._patch_guild_role,
            ("DELETE", "/guilds/{guild_id}/roles/{role_id}"): self._delete_guild_role,
            ("GET", "/guilds/{guild_id}/members"): self._get_guild_members,
            ("GET", "/guilds/{guild_id}/members/search"): self._get_guild_members_search,
            ("GET", "/guilds/{guild_id}/members/{user_id}"): self._get_guild_member,
            ("PATCH", "/guilds/{guild_id}/members/@me"): self._patch_member_noop,
            ("PATCH", "/guilds/{guild_id}/members/{user_id}"): self._patch_guild_member,
            ("PUT", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}"): self._put_member_role,
            ("DELETE", "/guilds/{guild_id}/members/{user_id}/roles/{role_id}"): self._delete_member_role,
            ("GET", "/channels/{channel_id}"): self._get_channel,
            ("PATCH", "/channels/{channel_id}"): self._patch_channel,
            ("DELETE", "/channels/{channel_id}"): self._delete_channel,
            ("POST", "/channels/{channel_id}/messages"): self._post_channel_messages,
            ("GET", "/channels/{channel_id}/messages"): self._get_channel_messages,
            ("POST", "/channels/{channel_id}/messages/bulk-delete"): self._post_bulk_delete,
            ("GET", "/channels/{channel_id}/messages/{message_id}"): self._get_channel_message,
            ("PATCH", "/channels/{channel_id}/messages/{message_id}"): self._patch_channel_message,
            ("DELETE", "/channels/{channel_id}/messages/{message_id}"): self._delete_channel_message,
            ("POST", "/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me"): self._post_reaction,
            ("POST", "/channels/{channel_id}/trigger_typing"): self._post_typing,
            ("GET", "/applications/{application_id}/commands"): self._get_application_commands,
            ("POST", "/applications/{application_id}/commands"): self._post_application_command,
            ("PUT", "/applications/{application_id}/commands"): self._put_application_commands,
            ("PATCH", "/applications/{application_id}/commands/{command_id}"): self._patch_application_command,
            ("DELETE", "/applications/{application_id}/commands/{command_id}"): self._delete_application_command,
            ("GET", "/applications/{application_id}/guilds/{guild_id}/commands"): self._get_application_commands,
            ("POST", "/applications/{application_id}/guilds/{guild_id}/commands"): self._post_application_command,
            ("PUT", "/applications/{application_id}/guilds/{guild_id}/commands"): self._put_application_commands,
            ("PATCH", "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}"): self._patch_application_command,
            ("DELETE", "/applications/{application_id}/guilds/{guild_id}/commands/{command_id}"): self._delete_application_command,
            # interaction responses ride the webhook adapter:
            ("POST", "/interactions/{webhook_id}/{webhook_token}/callback"): self._post_interaction_callback,
            ("POST", "/webhooks/{webhook_id}/{webhook_token}"): self._post_webhook_message,
            ("GET", "/webhooks/{webhook_id}/{webhook_token}"): self._get_webhook,
            ("GET", "/webhooks/{webhook_id}/{webhook_token}/messages/@original"): self._get_original,
            ("PATCH", "/webhooks/{webhook_id}/{webhook_token}/messages/@original"): self._patch_original,
            ("DELETE", "/webhooks/{webhook_id}/{webhook_token}/messages/@original"): self._delete_original,
            ("GET", "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"): self._get_webhook_message,
            ("PATCH", "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"): self._patch_webhook_message,
            ("DELETE", "/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}"): self._delete_webhook_message,
        }

    # ------------------------------------------------ dispatch plumbing

    def handle(self, route: Route, *, payload=None, files=None, params=None) -> Any:
        template = route.path
        handler = self._routes.get((route.method, template))
        if handler is None:
            error = RuntimeError(f"transport gap: {route.method} {template}")
            self.session.log("💥", str(error), "error",
                             details={"operation": "transport", "route": f"{route.method} {template}",
                                      "status": "transport_gap"})
            raise error
        match = self._match(route)
        try:
            return handler(match, payload or {}, params or {})
        except Exception as error:  # noqa: BLE001 - surface transport failures in the timeline
            self.session.log("💥", f"REST {route.method} {template} failed: {error}", "error",
                             details={"operation": "transport", "route": f"{route.method} {template}",
                                      "status": "script_error", "type": type(error).__name__,
                                      "message": str(error)})
            raise

    @staticmethod
    def _match(route: Route) -> dict[str, str]:
        pattern = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", re.escape(route.path))
        formatted = route.url[len(Route.BASE):]
        found = re.match(pattern, formatted)
        if found is None:  # pragma: no cover - url is always built from path
            raise RuntimeError(f"transport: cannot parse route url {route.url}")
        return found.groupdict()

    # ------------------------------------------------ payload helpers

    @property
    def session(self) -> pg.Session:
        return self.runtime.session

    def _me(self) -> pg.MockMember:
        return self.session.guild.me

    def _user_payload(self, member: pg.MockMember | None, *, fallback_id=None, fallback_name=None) -> dict:
        if member is not None:
            return {
                "id": str(member.id), "username": member.name,
                "global_name": getattr(member, "global_name", None) or member.name,
                "discriminator": "0", "avatar": None, "bot": bool(member.bot), "public_flags": 0,
            }
        return {"id": str(fallback_id or BOT_ID), "username": fallback_name or "Bot",
                "global_name": fallback_name or "Bot", "discriminator": "0", "avatar": None,
                "bot": True, "public_flags": 0}

    def _member_payload(self, member: pg.MockMember) -> dict:
        return {
            "user": self._user_payload(member), "nick": None, "avatar": None, "banner": None,
            "roles": [str(role.id) for role in member.roles if role.id != GUILD_ID],
            "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False,
            "pending": False, "permissions": str(member.guild_permissions.value),
            "communication_disabled_until": None, "flags": 0,
        }

    def _channel_payload(self, channel: pg.MockChannel) -> dict:
        return {
            "id": str(channel.id), "type": 0, "guild_id": str(GUILD_ID), "name": channel.name,
            "topic": channel.topic, "position": 0, "nsfw": False, "last_message_id": None,
            "rate_limit_per_user": 0, "parent_id": None,
            "permission_overwrites": [
                {"id": str(target_id), "type": 0 if target_id == GUILD_ID else 1,
                 "allow": str(overwrite.allow.value), "deny": str(overwrite.deny.value)}
                for target_id, overwrite in getattr(channel, "overwrites", {}).items()
            ],
        }

    def _role_payload(self, role: pg.MockRole) -> dict:
        return {
            "id": str(role.id), "name": role.name, "color": role.color.value,
            "colors": {"primary_color": role.color.value, "secondary_color": None, "tertiary_color": None},
            "permissions": str(role.permissions.value), "position": role.position,
            "hoist": role.hoist, "managed": False, "mentionable": True,
            "icon": None, "unicode_emoji": None, "flags": 0,
        }

    def _message_payload(self, stored: dict) -> dict:
        author = stored.get("author") or {}
        member = self.session.guild.get_member(int(author.get("id") or BOT_ID))
        return {
            "id": _wire_message_id(stored["id"]),
            "channel_id": str(stored.get("channel") or CHANNEL_ID),
            "guild_id": str(GUILD_ID),
            "author": self._user_payload(member, fallback_id=author.get("id"),
                                         fallback_name=author.get("name")),
            "member": self._member_payload(member) if member else None,
            "content": stored.get("content") or "",
            "timestamp": stored.get("timestamp") or "2024-01-01T00:00:00+00:00",
            "edited_timestamp": None, "tts": False, "mention_everyone": False,
            "mentions": [], "mention_roles": [], "attachments": [], "embeds": stored.get("embeds") or [],
            "pinned": False, "type": 0, "flags": 64 if stored.get("ephemeral") else 0,
        }

    def _store(self, channel: pg.MockChannel, payload: dict, *, ephemeral: bool = False) -> dict:
        """Store an outgoing REST message in the timeline; revive classic views for the UI."""
        view = _revive_classic_view(payload.get("components"))
        stored = self.session.add_message(
            channel_id=channel.id, content=payload.get("content"),
            embeds=_embeds_from_payload(payload.get("embeds")),
            view=view, ephemeral=ephemeral,
        )
        return stored

    def _edit(self, message_id: str | None, payload: dict) -> dict:
        view = payload.get("view") or _revive_classic_view(payload.get("components"))
        self.session.update_message(
            message_id, content=payload.get("content"),
            embeds=_embeds_from_payload(payload.get("embeds")), view=view,
        )
        return self.session.messages.get(message_id) or {}

    def _channel_or_raise(self, key: str) -> pg.MockChannel:
        channel = self.session.channels.get(str(key))
        if channel is None:
            raise RuntimeError(f"transport: unknown channel {key}")
        return channel

    def _message_or_raise(self, key: str) -> dict:
        stored = self.session.messages.get(_session_message_id(key) or "")
        if stored is None or stored.get("deleted"):
            raise RuntimeError(f"transport: unknown message {key}")
        return stored

    def _callback_payload(self, callback_type: int, stored: dict | None, interaction_id: str) -> dict:
        interaction: dict = {"id": interaction_id}
        resource = None
        if stored is not None:
            interaction["response_message_id"] = _wire_message_id(stored["id"])
            interaction["response_message_ephemeral"] = bool(stored.get("ephemeral"))
            resource = {"type": callback_type, "message": self._message_payload(stored)}
        return {"interaction": interaction, "resource": resource}

    # ------------------------------------------------ users / application

    def _get_users_at_me(self, match, payload, params):
        me = self._me()
        return {"id": str(me.id), "username": me.name, "global_name": me.name,
                "discriminator": "0", "avatar": None, "bot": True, "verified": True,
                "mfa_enabled": False, "flags": 0}

    def _get_users_guilds(self, match, payload, params):
        return [{"id": str(GUILD_ID), "name": self.session.guild.name, "icon": None,
                 "owner": False, "permissions": str(discord.Permissions.all().value), "features": []}]

    def _post_dm_channel(self, match, payload, params):
        recipient_id = int((payload.get("recipient_id") or USER_ID))
        user = self.session.guild.get_member(recipient_id)
        channel = self.session.make_channel(f"dm-{getattr(user, 'name', 'user')}")
        self.session.log("📨", f"DM channel opened → #{channel.name}", kind="action",
                         details={"operation": "user.create_dm", "channel": channel.name, "status": "success"})
        return self._channel_payload(channel)

    def _get_user(self, match, payload, params):
        member = self.session.guild.get_member(int(match["user_id"]))
        if member is None:
            raise RuntimeError(f"transport: unknown user {match['user_id']}")
        return self._user_payload(member)

    def _get_application(self, match, payload, params):
        me = self._me()
        return {"id": str(BOT_ID), "name": me.name, "icon": None,
                "description": "Simulated offline bot", "flags": 0,
                "owner": self._user_payload(self.session.guild.members[0]),
                "verify_key": "x" * 64, "interactions_endpoint_url": None,
                "role_connections_verification_url": None, "tags": [],
                "install_params": None, "custom_install_url": None,
                "bot_public": True, "bot_require_code_grant": False,
                "terms_of_service_url": None, "privacy_policy_url": None,
                "rpc_origins": [], "summary": "", "hook": True,
                "monetization_state": 1, "verification_state": 1,
                "store_application_state": 1, "rpc_application_state": 0}

    def _get_voice_regions(self, match, payload, params):
        return [{"id": "us-west", "name": "US West", "optimal": True, "vip": False,
                 "deprecated": False, "custom": False}]

    # ------------------------------------------------ guild

    def _get_guild(self, match, payload, params):
        return self.runtime.guild_payload(include_members=True)

    def _get_guild_channels(self, match, payload, params):
        return [self._channel_payload(channel) for channel in self.session.channels.values()]

    def _post_guild_channels(self, match, payload, params):
        channel = self.session.make_channel(payload.get("name") or "new-channel")
        self.session.log("➕", f"REST create channel → #{channel.name}", kind="action",
                         details={"operation": "guild.create_text_channel", "channel": channel.name,
                                  "status": "success"})
        return self._channel_payload(channel)

    def _get_guild_roles(self, match, payload, params):
        return [self._role_payload(role) for role in self.session.guild.roles]

    def _post_guild_roles(self, match, payload, params):
        role = self.runtime.create_role(payload.get("name") or "new-role")
        if payload.get("permissions") is not None:
            role.permissions = discord.Permissions(int(payload["permissions"]))
        if payload.get("color"):
            role.colour = role.color = discord.Colour(int(payload["color"]))
        self.session.log("➕", f"REST create role → {role.name}", kind="action",
                         details={"operation": "guild.create_role", "role": role.name, "status": "success"})
        return self._role_payload(role)

    def _patch_guild_role(self, match, payload, params):
        role = self.session.guild.get_role(int(match["role_id"]))
        if role is None:
            raise RuntimeError(f"transport: unknown role {match['role_id']}")
        if payload.get("name"):
            role.name = payload["name"]
        if payload.get("permissions") is not None:
            role.permissions = discord.Permissions(int(payload["permissions"]))
        if payload.get("color") is not None:
            role.colour = role.color = discord.Colour(int(payload["color"]))
        self.session._touch()
        return self._role_payload(role)

    def _delete_guild_role(self, match, payload, params):
        guild = self.session.guild
        role = guild.get_role(int(match["role_id"]))
        if role is not None and role.id != guild.id:
            guild.roles.remove(role)
            self.session._touch()
        return {}

    # ------------------------------------------------ members

    def _get_guild_members(self, match, payload, params):
        after = int(params.get("after") or 0)
        members = sorted((m for m in self.session.guild.members if m.id > after), key=lambda m: m.id)
        limit = int(params.get("limit") or len(members) or 1)
        return [self._member_payload(member) for member in members[:limit]]

    def _get_guild_members_search(self, match, payload, params):
        query = (params.get("query") or "").lower()
        found = [m for m in self.session.guild.members if query in m.name.lower()]
        limit = int(params.get("limit") or 1)
        return {"members": [self._member_payload(member) for member in found[:limit]]}

    def _get_guild_member(self, match, payload, params):
        member = self.session.guild.get_member(int(match["user_id"]))
        if member is None:
            raise RuntimeError(f"transport: unknown member {match['user_id']}")
        return self._member_payload(member)

    def _patch_member_noop(self, match, payload, params):
        return {}

    def _patch_guild_member(self, match, payload, params):
        member = self.session.guild.get_member(int(match["user_id"]))
        if member is not None and payload.get("nick") is not None:
            member.display_name = payload["nick"]
            self.session._touch()
        return self._member_payload(member) if member else {}

    def _put_member_role(self, match, payload, params):
        member = self.session.guild.get_member(int(match["user_id"]))
        role = self.session.guild.get_role(int(match["role_id"]))
        if member is not None and role is not None and role not in member.roles:
            member.roles.append(role)
            self.session.log("➕", f"role added: {member.name} += {role.name}", kind="action",
                             details={"operation": "member.add_roles", "member": member.name,
                                      "role": role.name, "status": "success"})
        return {}

    def _delete_member_role(self, match, payload, params):
        member = self.session.guild.get_member(int(match["user_id"]))
        role = self.session.guild.get_role(int(match["role_id"]))
        if member is not None and role is not None and role in member.roles:
            member.roles.remove(role)
            self.session.log("➖", f"role removed: {member.name} -= {role.name}", kind="action",
                             details={"operation": "member.remove_roles", "member": member.name,
                                      "role": role.name, "status": "success"})
        return {}

    # ------------------------------------------------ channels

    def _get_channel(self, match, payload, params):
        return self._channel_payload(self._channel_or_raise(match["channel_id"]))

    def _patch_channel(self, match, payload, params):
        channel = self._channel_or_raise(match["channel_id"])
        if payload.get("name"):
            channel.name = payload["name"]
        if "topic" in payload:
            channel.topic = payload["topic"]
        self.session._touch()
        return self._channel_payload(channel)

    def _delete_channel(self, match, payload, params):
        self.session.delete_channel(match["channel_id"])
        return {}

    def _post_typing(self, match, payload, params):
        channel = self._channel_or_raise(match["channel_id"])
        self.session.log("⌨️", f"bot is typing in #{channel.name}", kind="action",
                         details={"operation": "trigger_typing", "channel": channel.name, "status": "success"})
        return {}

    # ------------------------------------------------ messages

    def _post_channel_messages(self, match, payload, params):
        channel = self._channel_or_raise(match["channel_id"])
        ephemeral = bool(int(payload.get("flags") or 0) & 64)
        stored = self._store(channel, payload, ephemeral=ephemeral)
        self.session.log("↗️", f"REST channel.send → #{channel.name}", kind="action",
                         details={"operation": "channel.send", "channel": channel.name,
                                  "message_id": stored["id"], "transport": "project-rest",
                                  "status": "success"})
        return self._message_payload(stored)

    def _get_channel_messages(self, match, payload, params):
        channel = self._channel_or_raise(match["channel_id"])
        limit = int(params.get("limit") or 50)
        out = []
        for message_id in reversed(self.session.order):
            stored = self.session.messages.get(message_id)
            if stored is None or stored.get("deleted") or stored.get("channel") != str(channel.id):
                continue
            out.append(self._message_payload(stored))
            if len(out) >= limit:
                break
        return out

    def _get_channel_message(self, match, payload, params):
        return self._message_payload(self._message_or_raise(match["message_id"]))

    def _patch_channel_message(self, match, payload, params):
        message_id = _session_message_id(match["message_id"])
        stored = self._edit(message_id, payload)
        self.session.log("✏️", "REST message edited", kind="action",
                         details={"operation": "message.edit", "message_id": message_id,
                                  "transport": "project-rest", "status": "success"})
        return self._message_payload(stored)

    def _delete_channel_message(self, match, payload, params):
        self.session.delete_message(_session_message_id(match["message_id"]))
        return {}

    def _post_bulk_delete(self, match, payload, params):
        for wire_id in payload.get("messages") or []:
            self.session.delete_message(_session_message_id(str(wire_id)))
        self.session.log("🗑️", "REST bulk delete", kind="action",
                         details={"operation": "messages.bulk_delete",
                                  "count": len(payload.get("messages") or []), "status": "success"})
        return {}

    def _post_reaction(self, match, payload, params):
        channel = self._channel_or_raise(match["channel_id"])
        self.session.log("➕", f"bot reacted {match['emoji']!r} in #{channel.name}", kind="action",
                         details={"operation": "message.add_reaction", "emoji": match["emoji"],
                                  "message_id": _session_message_id(match["message_id"]),
                                  "status": "success"})
        return {}

    # ------------------------------------------------ application commands

    def _synced(self) -> list[dict]:
        return self.runtime._synced_commands

    def _get_application_commands(self, match, payload, params):
        return list(self._synced())

    def _put_application_commands(self, match, payload, params):
        body = payload if isinstance(payload, list) else []
        base = 10_000_000_000_000_000_000
        registered = [dict(item, id=str(base + index), application_id=str(BOT_ID))
                      for index, item in enumerate(body)]
        self.runtime._synced_commands = registered
        names = [item.get("name") for item in body]
        self.session.log("⌨️", f"tree.sync registered {len(registered)} slash command(s)", kind="event",
                         details={"operation": "tree.sync", "commands": names,
                                  "transport": "project-rest", "status": "success"})
        return registered

    def _post_application_command(self, match, payload, params):
        self._command_counter = getattr(self, "_command_counter", 10_000_001_000_000_000_000) + 1
        registered = dict(payload, id=str(self._command_counter), application_id=str(BOT_ID))
        self._synced().append(registered)
        return registered

    def _patch_application_command(self, match, payload, params):
        for index, item in enumerate(self._synced()):
            if item.get("id") == match["command_id"]:
                item.update(payload, application_id=str(BOT_ID))
                return item
        raise RuntimeError(f"transport: unknown command {match['command_id']}")

    def _delete_application_command(self, match, payload, params):
        self.runtime._synced_commands = [item for item in self._synced()
                                         if item.get("id") != match["command_id"]]
        return {}

    # ------------------------------------------------ interactions + followups

    def _post_interaction_callback(self, match, payload, params):
        interaction_id = match["webhook_id"]
        callback_type = int(payload.get("type") or 4)
        data = payload.get("data") or {}
        stored: dict | None = None
        if callback_type in (4, 5):  # channel message with source / deferred "thinking"
            stored = self._store(self.session.channel, data,
                                 ephemeral=bool(int(data.get("flags") or 0) & 64))
            self.session.log("💬", f"interaction response → message #{stored['index']}", kind="action",
                             details={"operation": "interaction.response", "message_id": stored["id"],
                                      "callback_type": callback_type, "transport": "project-rest",
                                      "status": "success"})
        elif callback_type == 7:  # component message update
            original = self.runtime._originals.get(interaction_id)
            stored = self._edit(original, data)
            self.session.log("✏️", "interaction updated its message", kind="action",
                             details={"operation": "interaction.update_message",
                                      "message_id": original, "transport": "project-rest",
                                      "status": "success"})
        elif callback_type == 9:
            modal = None
            bot = self.runtime.bot
            store = getattr(bot._connection, "_view_store", None) if bot is not None else None
            if store is not None:
                modal = store._modals.get(data.get("custom_id"))
            if modal is not None:
                source = self.runtime._originals.get(interaction_id)
                self.session.open_modal(modal, source)
                self.runtime._pending_modal = (data.get("custom_id"), source)
            else:
                self.session.log("📋", "modal opened by interaction", kind="action",
                                 details={"operation": "interaction.send_modal",
                                          "custom_id": data.get("custom_id"), "status": "success"})
        elif callback_type == 6:
            self.session.log("💭", "interaction deferred (component update)", kind="action",
                             details={"operation": "interaction.defer", "callback_type": 6,
                                      "status": "success"})
        else:
            self.session.log("💭", "interaction deferred (thinking)", kind="action",
                             details={"operation": "interaction.defer", "callback_type": callback_type,
                                      "status": "success"})
        if stored is not None:
            self.runtime._originals[interaction_id] = stored["id"]
        return self._callback_payload(callback_type, stored, interaction_id)

    def _post_webhook_message(self, match, payload, params):
        stored = self._store(self.session.channel, payload,
                             ephemeral=bool(int(payload.get("flags") or 0) & 64))
        self.session.log("💬", f"followup.send → message #{stored['index']}", kind="action",
                         details={"operation": "interaction.followup", "message_id": stored["id"],
                                  "transport": "project-rest", "status": "success"})
        return self._message_payload(stored)

    def _get_webhook(self, match, payload, params):
        return {"id": match["webhook_id"], "type": 3, "token": match["webhook_token"],
                "application_id": str(BOT_ID), "name": self._me().name,
                "channel_id": str(self.session.channel.id)}

    def _original_target(self, match) -> str | None:
        return self.runtime._originals.get(match["webhook_id"])

    def _get_original(self, match, payload, params):
        stored = self.session.messages.get(self._original_target(match) or "")
        return self._message_payload(stored or {})

    def _patch_original(self, match, payload, params):
        stored = self._edit(self._original_target(match), payload)
        return self._message_payload(stored)

    def _delete_original(self, match, payload, params):
        self.session.delete_message(self._original_target(match))
        return {}

    def _get_webhook_message(self, match, payload, params):
        return self._message_payload(self._message_or_raise(match["message_id"]))

    def _patch_webhook_message(self, match, payload, params):
        stored = self._edit(_session_message_id(match["message_id"]), payload)
        return self._message_payload(stored)

    def _delete_webhook_message(self, match, payload, params):
        self.session.delete_message(_session_message_id(match["message_id"]))
        return {}


# ------------------------------------------------------------- runtime


class ProjectRuntime:
    """Boots a real discord.py bot project offline against one simulator session."""

    def __init__(self, session: pg.Session, workspace: Path, tag: str | None = None) -> None:
        self.session = session
        self.workspace = Path(workspace)
        self.tag = tag or uuid.uuid4().hex[:8]
        self.sandbox = _make_sandbox(self.workspace, self.tag)
        self.bot: discord.Client | None = None
        self.clients: list[discord.Client] = []
        self.transport = ProjectTransport(self)
        self._synced_commands: list[dict] = []
        self._originals: dict[str, str | None] = {}  # interaction id -> original message id
        self._interaction_counter = 9_000_000_000_000_000_000
        self._command_counter = 10_000_000_000_000_000_000
        self._main_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._pending_modal: tuple[str, str | None] | None = None  # (custom_id, source message)
        self.entry: str | None = None

    # ------------------------------------------------ boot

    async def boot(self) -> None:
        _ensure_patches()
        _neutralize_keep_alive()
        _install_psutil_stub()
        entry = _find_entry(self.sandbox)
        if entry is None:
            raise RuntimeError("no Python entrypoint found (main.py / bot.py / …)")
        self.entry = entry.name
        # Stay on sys.path for the whole session: load_extension() and lazy
        # intra-project imports inside callbacks need it long after boot.
        sys.path.insert(0, str(self.sandbox))
        self.session.log("🚀", f"project run: sandbox {self.sandbox.name}", kind="event",
                         details={"operation": "project.run", "workspace": self.workspace.name,
                                  "sandbox": self.sandbox.name, "entry": entry.name,
                                  "status": "starting"})
        async with _BOOT_LOCK:
            _purge_sandbox_modules()
            module = self._import_entry(entry)
            bot = self._scan_for_bot() or (self.clients[-1] if self.clients else None)
            if bot is None:
                main_fn = getattr(module, "main", None)
                if callable(main_fn):
                    await self._run_entry_main(main_fn)
                    bot = self._scan_for_bot() or (self.clients[-1] if self.clients else None)
            if bot is not None:
                self.bot = bot
                if not bot.is_ready():
                    await self._boot_bot(bot)
            self.session.log("🔌", "fake transport installed — no network will be contacted",
                             kind="event", details={"operation": "project.transport",
                                                    "status": "installed"})

    def _import_entry(self, entry: Path):
        saved_cwd = os.getcwd()
        token = None
        try:
            os.chdir(self.sandbox)  # sqlite/env filenames resolve like a real launch
            token = _ACTIVE_BOOT.set(self)
            module = importlib.import_module(entry.stem)
            return module
        finally:
            if token is not None:
                _ACTIVE_BOOT.reset(token)
            os.chdir(saved_cwd)

    def _scan_for_bot(self) -> discord.Client | None:
        for module in list(sys.modules.values()):
            file = str(getattr(module, "__file__", "") or "")
            if not file.startswith(str(self.sandbox)):
                continue
            for value in vars(module).values():
                if isinstance(value, discord.Client):
                    return value
        return None

    async def _run_entry_main(self, main_fn) -> None:
        token = _ACTIVE_BOOT.set(self)
        try:
            result = main_fn()
            if inspect_awaitable(result):
                # Created while the contextvar is set: bots constructed inside
                # main() still register with this runtime.
                self._main_task = asyncio.ensure_future(_wrap_main(result, self))
                await asyncio.sleep(0.5)  # give login/setup_hook a chance to finish
        finally:
            _ACTIVE_BOOT.reset(token)

    async def _boot_bot(self, bot: discord.Client) -> None:
        session = self.session
        with contextlib.suppress(Exception):
            bot.intents.message_content = True  # simulator auto-grants privileged intents
        state = bot._connection
        state.guild_ready_timeout = min(getattr(state, "guild_ready_timeout", 2.0), 0.5)
        with contextlib.suppress(Exception):
            # Bots that never completed login still need loop/http wiring before
            # READY injection can dispatch events.
            await bot._async_setup_hook()
        if bot.user is None:
            # main() may still be mid-login; give it a moment before doing it here.
            for _ in range(100):
                if bot.user is not None or (self._main_task is not None and self._main_task.done()):
                    break
                await asyncio.sleep(0.05)
        if bot.user is None:
            await bot.login(_PLACEHOLDER_TOKEN)  # real login: setup_hook + tree sync run here
        session.log("⚙️", f"logged in as {bot.user} — cogs loaded, tree synced", kind="event",
                    details={"operation": "project.login", "status": "success",
                             "cogs": sorted(getattr(bot, "cogs", {}) or {})})
        state.parse_ready(self._ready_payload())
        state.parse_guild_create(self.guild_payload(include_members=True))
        for _ in range(100):  # _delay_ready fires guild_available + ready
            if bot.is_ready():
                break
            await asyncio.sleep(0.05)
        await self._drain()
        session.log("✅", f"project bot is ready — cogs: {sorted(getattr(bot, 'cogs', {}) or {}) or 'none'}",
                    kind="event", details={"operation": "project.ready",
                                           "bot": str(bot.user), "status": "ready"})

    def _ready_payload(self) -> dict:
        me = self.session.guild.me
        return {
            "v": 10,
            "user": {"id": str(me.id), "username": me.name, "discriminator": "0",
                     "avatar": None, "bot": True, "global_name": me.name},
            "guilds": [],
            "session_id": f"playground-{self.tag}",
            "resume_gateway_url": "wss://offline.invalid",
            "application": {"id": str(BOT_ID), "flags": 0},
            "shard": [0, 1],
            "session_type": "normal",
        }

    def guild_payload(self, *, include_members: bool = False) -> dict:
        session = self.session
        guild = session.guild
        payload = {
            "id": str(GUILD_ID), "name": guild.name, "icon": None, "splash": None,
            "discovery_splash": None, "owner_id": str(guild.owner_id), "afk_channel_id": None,
            "afk_timeout": 300, "widget_enabled": False, "widget_channel_id": None,
            "verification_level": 0, "default_message_notifications": 0,
            "explicit_content_filter": 0,
            "roles": [self.transport._role_payload(role) for role in guild.roles],
            "emojis": [], "features": [], "mfa_level": 0, "application_id": str(BOT_ID),
            "system_channel_id": None, "system_channel_flags": 0, "rules_channel_id": None,
            "max_presences": None, "max_members": 500000, "vanity_url_code": None,
            "description": None, "banner": None, "premium_tier": 0, "subscription_count": 0,
            "preferred_locale": "en-US", "public_updates_channel_id": None, "nsfw_level": 0,
            "premium_progress_bar_enabled": False, "stickers": [],
        }
        if include_members:
            payload.update({
                "members": [self.transport._member_payload(member) for member in guild.members],
                "channels": [self.transport._channel_payload(channel)
                             for channel in session.channels.values()],
                "presences": [], "voice_states": [], "threads": [], "stage_instances": [],
                "guild_scheduled_events": [], "large": False,
                "member_count": len(guild.members),
            })
        return payload

    def create_role(self, name: str) -> pg.MockRole:
        guild = self.session.guild
        self._command_counter += 1  # reuse the counter for unique role ids
        role = MockRole(GUILD_ID + 7000 + (self._command_counter % 100000), name)
        role.position = max((r.position for r in guild.roles), default=0) + 1
        guild.roles.append(role)
        self.session._touch()
        return role

    # ------------------------------------------------ dispatch

    async def _drain(self, limit: int = 60) -> None:
        """Let spawned callback tasks finish so REST side effects land."""
        for _ in range(limit):
            await asyncio.sleep(0.05)
            if not self._pending_tasks():
                break

    def _pending_tasks(self) -> list[asyncio.Task]:
        current = asyncio.current_task()
        pending = []
        for task in asyncio.all_tasks():
            if task is current or task.done():
                continue
            if task.get_name().startswith("discord-ext-tasks"):
                continue  # background task loops run forever by design
            pending.append(task)
        return pending

    def _next_interaction_id(self) -> str:
        self._interaction_counter += 1
        return str(self._interaction_counter)

    def _interaction_payload(self, *, kind: str, message_id: str | None = None,
                             custom_id: str | None = None, values: Any = None,
                             data: dict | None = None) -> dict:
        session = self.session
        channel = session.channel
        interaction_id = self._next_interaction_id()
        payload = {
            "id": interaction_id,
            "application_id": str(BOT_ID),
            "type": 2 if kind == "command" else (3 if kind == "click" else 5),
            "token": _PLACEHOLDER_TOKEN,
            "version": 1,
            "guild_id": str(GUILD_ID),
            "channel": {"id": str(channel.id), "type": 0, "name": channel.name},
            "channel_id": str(channel.id),
            "locale": "en-US",
            "guild_locale": "en-US",
            "app_permissions": str(discord.Permissions.all().value),
            "attachment_size_limit": 26214400,
            "entitlements": [],
            "authorizing_integration_owners": {},
            "member": self.transport._member_payload(session.active_user),
        }
        if message_id is not None:
            stored = session.messages.get(message_id) or {}
            payload["message"] = self.transport._message_payload(stored)
        if kind == "command":
            payload["data"] = data
        elif kind == "click":
            payload["data"] = {"custom_id": custom_id, "component_type": self._component_type(message_id, custom_id),
                               "values": values or []}
        else:  # modal submit
            payload["data"] = {"custom_id": custom_id, "resolved": {}, "components": [
                {"type": 4, "id": index + 1, "custom_id": name, "value": value}
                for index, (name, value) in enumerate((values or {}).items())
            ]}
        if kind == "click" and message_id is not None:
            self._originals[interaction_id] = message_id
        return payload

    _COMPONENT_TYPE_BY_KIND = {"button": 2, "select": 3, "user_select": 5, "role_select": 6,
                               "mentionable_select": 7, "channel_select": 8}

    def _component_type(self, message_id: str | None, custom_id: str | None) -> int:
        if message_id and custom_id:
            stored = self.session.messages.get(message_id) or {}
            for item in stored.get("components") or []:
                if item.get("custom_id") == custom_id:
                    return self._COMPONENT_TYPE_BY_KIND.get(item.get("kind"), 2)
        return 2

    _OPTION_TYPES = {"string": 3, "integer": 4, "boolean": 5, "user": 6, "channel": 7,
                     "role": 8, "mentionable": 9, "number": 10, "attachment": 11}

    def _command_data(self, name: str, args: dict) -> dict:
        spec = (self.commands_payload().get(name) or {}).get("params", [])
        params = {p["name"]: p for p in spec}
        resolved: dict[str, dict] = {"users": {}, "members": {}, "roles": {}, "channels": {}, "messages": {}}
        options = []
        for param_name, raw in (args or {}).items():
            if raw in (None, ""):
                continue
            info = params.get(param_name) or {}
            kind = info.get("type", "string")
            wire_type = self._OPTION_TYPES.get(kind, 3)
            if kind in ("integer",):
                value = int(raw)
            elif kind == "number":
                value = float(raw)
            elif kind == "boolean":
                value = str(raw).lower() in ("1", "true", "yes", "on")
            elif kind in ("user", "channel", "role", "mentionable"):
                value = str(raw)
                member = self.session.guild.get_member(int(raw)) if str(raw).isdigit() else None
                role = self.session.guild.get_role(int(raw)) if str(raw).isdigit() else None
                channel = self.session.channels.get(str(raw))
                if member is not None and kind == "user":
                    resolved["users"][str(member.id)] = self.transport._user_payload(member)
                    resolved["members"][str(member.id)] = self.transport._member_payload(member)
                elif role is not None and kind in ("role", "mentionable"):
                    resolved["roles"][str(role.id)] = self.transport._role_payload(role)
                elif channel is not None and kind in ("channel", "mentionable"):
                    resolved["channels"][str(channel.id)] = self.transport._channel_payload(channel)
            else:
                value = str(raw)
            options.append({"name": param_name, "type": wire_type, "value": value})
        data = {"id": self._next_interaction_id(), "name": name, "type": 1, "resolved": resolved}
        if options:
            data["options"] = options
        return data

    def _call_interaction(self, payload: dict) -> None:
        bot = self.bot
        if bot is None:
            raise RuntimeError("no project bot is running")
        self._originals.setdefault(payload["id"], None)
        bot._connection.parse_interaction_create(payload)

    async def dispatch_command(self, name: str, args: dict) -> None:
        command = None
        if self.bot is not None and getattr(self.bot, "tree", None) is not None:
            command = self.bot.tree.get_command(name)
        if command is None:
            self.session.log("⚠️", f"/{name} is not registered by this bot", "warn", kind="event",
                             details={"operation": "interaction.command", "command": name,
                                      "status": "missing_command"})
            return
        self.session.log("⌨️", f"command invoked: /{name}", kind="event",
                         details={"operation": "interaction.command", "command": name,
                                  "arguments": args, "actor": self.session.active_user.name,
                                  "status": "dispatched"})
        payload = self._interaction_payload(kind="command", data=self._command_data(name, args))
        self._call_interaction(payload)
        await self._drain()

    async def dispatch_click(self, message_id: str, custom_id: str, values: list) -> None:
        self.session.log("🖱️", f"component used: {custom_id!r}", kind="event",
                         details={"operation": "interaction.component", "custom_id": custom_id,
                                  "message_id": message_id, "actor": self.session.active_user.name,
                                  "status": "dispatched"})
        payload = self._interaction_payload(kind="click", message_id=message_id,
                                            custom_id=custom_id, values=values)
        self._call_interaction(payload)
        await self._drain()

    async def dispatch_submit(self, message_id: str, custom_id: str, values: dict) -> None:
        self.session.log("📝", f"modal submitted: {custom_id!r}", kind="event",
                         details={"operation": "interaction.modal_submit", "custom_id": custom_id,
                                  "actor": self.session.active_user.name, "status": "dispatched"})
        payload = self._interaction_payload(kind="submit", message_id=message_id,
                                            custom_id=custom_id, values=values)
        self._call_interaction(payload)
        await self._drain()

    async def dispatch_pending_modal(self, values: dict) -> bool:
        """Submit the UI-filled modal the bot last opened. False if none is open."""
        if self._pending_modal is None:
            return False
        custom_id, source = self._pending_modal
        self._pending_modal = None
        await self.dispatch_submit(source, custom_id, values)
        return True

    async def dispatch_message(self, content: str, channel_id: str | None = None) -> None:
        """User chat message → real MESSAGE_CREATE → on_message + prefix commands."""
        session = self.session
        channel = session.channels.get(str(channel_id or ""), session.channel)
        allowed, reason = channel.permission_check(session.active_user, "send_messages")
        if not allowed:
            session.log("🚫", f"message blocked: missing send_messages permission ({reason})", "warn",
                        kind="event", details={"interaction": "message_create", "operation": "message.send",
                                               "channel": channel.name, "actor": session.active_user.name,
                                               "permission": "send_messages", "reason": reason,
                                               "status": "denied"})
            return
        stored = session.add_message(channel_id=channel.id, content=content, author=session.active_user)
        bot = self.bot
        if bot is not None:
            payload = self.transport._message_payload(stored)
            payload["mentions"] = []
            payload["mention_roles"] = []
            bot._connection.parse_message_create(payload)
        await self._drain()

    # ------------------------------------------------ introspection for the UI

    def commands_payload(self) -> dict:
        tree = getattr(self.bot, "tree", None) if self.bot is not None else None
        out: dict[str, dict] = {}
        if tree is None:
            return out
        for command in tree.get_commands():
            params = []
            for parameter in command.parameters:
                choices = [[choice.name, choice.value] for choice in (parameter.choices or [])]
                params.append({
                    "name": parameter.name,
                    "description": parameter.description or parameter.display_name,
                    "required": parameter.required,
                    "type": self._option_type_name(parameter.type),
                    "choices": choices,
                })
            out[command.name] = {"name": command.name, "description": command.description or "",
                                 "params": params}
        return out

    @staticmethod
    def _option_type_name(option_type) -> str:
        return {
            discord.AppCommandOptionType.string: "string",
            discord.AppCommandOptionType.integer: "integer",
            discord.AppCommandOptionType.number: "number",
            discord.AppCommandOptionType.boolean: "boolean",
            discord.AppCommandOptionType.user: "user",
            discord.AppCommandOptionType.channel: "channel",
            discord.AppCommandOptionType.role: "role",
            discord.AppCommandOptionType.mentionable: "mentionable",
            discord.AppCommandOptionType.attachment: "attachment",
        }.get(option_type, "string")

    mode = "python"

    def status(self) -> dict:
        bot = self.bot
        return {
            "entry": self.entry,
            "sandbox": self.sandbox.name,
            "mode": self.mode,
            "bot": str(bot.user) if bot is not None and bot.user is not None else None,
            "ready": bool(bot.is_ready()) if bot is not None else False,
            "cogs": sorted(getattr(bot, "cogs", {}) or {}) if bot is not None else [],
            "commands": sorted(self.commands_payload()),
            "synced": len(self._synced_commands),
        }

    # ------------------------------------------------ shutdown

    async def shutdown(self) -> None:
        self._connected.set()  # release any bot parked in connect()
        with contextlib.suppress(ValueError):
            sys.path.remove(str(self.sandbox))
        for bot in list(self.clients):
            _HTTP_RUNTIMES.pop(id(bot.http), None)
        session_obj = None
        for bot in self.clients:
            http_session = _http_session(bot.http)
            if http_session is not None:
                _SESSION_RUNTIMES.pop(id(http_session), None)
                session_obj = http_session
        if self.bot is not None and not self.bot.is_closed():
            with contextlib.suppress(Exception):
                await self.bot.close()
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._main_task
        if session_obj is not None:
            with contextlib.suppress(Exception):
                await session_obj.close()
        with contextlib.suppress(Exception):
            shutil.rmtree(self.sandbox, ignore_errors=True)
        _purge_sandbox_modules()


def inspect_awaitable(value) -> bool:  # tiny alias to keep imports honest
    import inspect

    return inspect.isawaitable(value)


async def _wrap_main(awaitable, runtime: "ProjectRuntime") -> None:
    """Entry main() runs as a background task; failures surface as events."""
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except BaseException as error:  # noqa: BLE001
        import traceback

        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        runtime.session.log("💥", f"main() failed: {error}", "error",
                            details={"operation": "project.main", "status": "script_error",
                                     "type": type(error).__name__, "message": str(error),
                                     "traceback": text[-2000:]})


async def run_project(session: pg.Session, workspace: Path, tag: str | None = None):
    """Create + boot a runtime (Python or Node); raises with a logged event on failure."""
    workspace = Path(workspace)
    runtime: ProjectRuntime | NodeProjectRuntime
    if _is_node_project(workspace):
        runtime = NodeProjectRuntime(session, workspace, tag)
    else:
        runtime = ProjectRuntime(session, workspace, tag)
    try:
        await runtime.boot()
    except BaseException as error:  # noqa: BLE001 - surface boot failures in the UI
        import traceback

        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        session.log("💥", f"project boot failed: {error}", "error",
                    details={"operation": "project.run", "status": "script_error",
                             "type": type(error).__name__, "message": str(error),
                             "traceback": text[-4000:]})
        await runtime.shutdown()
        raise
    session.commands = runtime.commands_payload()
    return runtime


def detect_entry(workspace: Path) -> str | None:
    """Entry filename for the UI, detected in the ORIGINAL workspace."""
    entry = _find_entry(Path(workspace))
    return entry.name if entry is not None else None


# ------------------------------------------------------------- node runtime


_SHIM_DIR = Path(__file__).resolve().parent / "node_shim"
_RUNNER_JS = _SHIM_DIR / "runner.js"
_NODE_ENTRY_NAMES = ("index.js", "bot.js", "main.js", "app.js", "shard.js")


def _find_node_entry(root: Path) -> Path | None:
    for name in _NODE_ENTRY_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    top = sorted(
        (p for p in root.glob("*.js") if p.is_file() and not p.name.startswith(("_", "."))),
        key=lambda p: (-p.stat().st_size, p.name.lower()),
    )
    return top[0] if top else None


def _is_node_project(root: Path) -> bool:
    package = root / "package.json"
    try:
        return "discord.js" in package.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


class NodeProjectRuntime:
    """Runs a Node discord.js bot offline through the bundled shim.

    The shim (node_shim/discord.js) is injected via NODE_PATH — no npm install.
    The bot process speaks NDJSON over stdio; every outgoing message lands in
    the simulator timeline. Nothing touches the network.
    """

    mode = "node"

    def __init__(self, session: pg.Session, workspace: Path, tag: str | None = None) -> None:
        self.session = session
        self.workspace = Path(workspace)
        self.tag = tag or uuid.uuid4().hex[:8]
        self.sandbox = _make_sandbox(self.workspace, self.tag)
        self.process: asyncio.subprocess.Process | None = None
        self._synced_commands: list[dict] = []
        self._interaction_counter = 9_000_000_000_000_000_000
        self._ready = asyncio.Event()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._last_reply: str | None = None
        self.entry: str | None = None
        self._closed = False

    async def boot(self) -> None:
        if not Path(sys.executable).exists():  # pragma: no cover - paranoid
            raise RuntimeError("python host missing")
        import shutil as _shutil

        node = _shutil.which("node")
        if node is None:
            raise RuntimeError("node.js is not installed or not on PATH")
        entry = _find_node_entry(self.sandbox)
        if entry is None:
            raise RuntimeError("no Node entrypoint found (index.js / bot.js / main.js / …)")
        self.entry = entry.name
        self.session.log("🚀", f"project run (node): sandbox {self.sandbox.name}", kind="event",
                         details={"operation": "project.run", "workspace": self.workspace.name,
                                  "sandbox": self.sandbox.name, "entry": entry.name,
                                  "runtime": "node-shim", "status": "starting"})
        env = os.environ.copy()
        for key in _TOKEN_KEYS:
            env[key] = _PLACEHOLDER_TOKEN
        env.pop("GROQ_KEY", None)  # never call out to AI providers from the sim
        env["SHIM_DIR"] = str(_SHIM_DIR)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = await asyncio.create_subprocess_exec(
            node, str(_RUNNER_JS), str(entry),
            cwd=str(self.sandbox), env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        self._reader_task = asyncio.create_task(self._pump_stdout())
        self._stderr_task = asyncio.create_task(self._pump_stderr())
        self._reader_task.add_done_callback(self._pump_finished)
        self._stderr_task.add_done_callback(self._pump_finished)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError as error:
            raise RuntimeError("node bot did not become ready within 20s") from error
        me = self.session.guild.me
        self.session.log("✅", f"node bot is ready — {me.name}", kind="event",
                         details={"operation": "project.ready", "bot": me.name,
                                  "runtime": "node-shim", "status": "ready"})

    async def _pump_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            async for raw in self.process.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    self.session.log("🖨️", line)  # bot's own console.log
                    continue
                await self._handle_event(payload)
        except asyncio.CancelledError:
            raise
        except BaseException as error:  # noqa: BLE001 - a dead reader must not stall boot silently
            self.session.log("💥", f"node bridge reader crashed: {error}", "error",
                             details={"operation": "node.reader", "runtime": "node-shim",
                                      "status": "script_error"})
            log.exception("node bridge reader crashed")

    def _pump_finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.session.log("💥", f"node bridge task failed: {error}", "error",
                             details={"operation": "node.pump", "runtime": "node-shim",
                                      "status": "script_error"})
            log.exception("node bridge pump failed")

    async def _pump_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        async for raw in self.process.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.session.log("🖨️", line)

    def _send(self, payload: dict) -> None:
        if self.process is not None and self.process.stdin is not None and not self._closed:
            self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))

    async def _handle_event(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "hello":
            # Bridge announced itself; deliver the ready event immediately.
            me = self.session.guild.me
            self._send({"type": "ready", "user": self._bot_user(),
                        "channel_id": str(self.session.channel.id),
                        "guild_id": str(GUILD_ID)})
        elif kind == "ready":
            self._ready.set()
            self.session.log("⚙️", f"node client ready as {self.session.guild.me.name}", kind="event",
                             details={"operation": "project.login", "runtime": "node-shim",
                                      "status": "success"})
        elif kind == "sync_commands":
            self._synced_commands = list(payload.get("commands") or [])
            names = [c.get("name") for c in self._synced_commands]
            self.session.log("⌨️", f"REST sync registered {len(names)} slash command(s)", kind="event",
                             details={"operation": "tree.sync", "commands": names,
                                      "runtime": "node-shim", "status": "success"})
        elif kind == "send":
            stored = self._store_outgoing(payload)
            self.session.log("↗️", f"channel.send → #{self.session.channel.name}", kind="action",
                             details={"operation": "channel.send", "channel": self.session.channel.name,
                                      "message_id": stored["id"], "runtime": "node-shim",
                                      "status": "success"})
        elif kind == "typing":
            self.session.log("⌨️", "bot is typing…", kind="action",
                             details={"operation": "trigger_typing", "runtime": "node-shim",
                                      "status": "success"})
        elif kind == "presence":
            self.session.log("🪄", f"presence set: {payload.get('activity')}", kind="action",
                             details={"operation": "change_presence", "runtime": "node-shim",
                                      "activity": payload.get("activity"), "status": "success"})
        elif kind == "interaction_reply":
            self._last_reply = payload.get("interaction_id")
            stored = self._store_outgoing(payload)
            self.session.log("💬", f"interaction reply → message #{stored['index']}", kind="action",
                             details={"operation": "interaction.response",
                                      "message_id": stored["id"], "runtime": "node-shim",
                                      "status": "success"})
        elif kind == "interaction_defer":
            self.session.log("💭", "interaction deferred (thinking)", kind="action",
                             details={"operation": "interaction.defer", "runtime": "node-shim",
                                      "status": "success"})
        elif kind == "interaction_edit":
            if self._last_reply is not None:
                self.session.update_message(self._last_reply, content=payload.get("content"),
                                            embeds=self._embeds(payload))
        elif kind == "error":
            self.session.log("💥", str(payload.get("message"))[:1500], "error",
                             details={"operation": "node.error", "runtime": "node-shim",
                                      "status": "script_error"})
        elif kind == "boot_error":
            self.session.log("💥", str(payload.get("message"))[:4000], "error",
                             details={"operation": "project.run", "runtime": "node-shim",
                                      "status": "script_error"})
        elif kind == "bye":
            self.session.log("🛑", "node bot exited", kind="event",
                             details={"operation": "project.shutdown", "runtime": "node-shim",
                                      "status": "stopped"})

    def _bot_user(self) -> dict:
        me = self.session.guild.me
        return {"id": str(me.id), "username": me.name, "global_name": me.name,
                "bot": True, "avatar": None, "discriminator": "0"}

    def _embeds(self, payload: dict) -> list | None:
        raw = payload.get("embeds")
        if not raw:
            return None
        out = []
        for item in raw:
            with contextlib.suppress(Exception):
                out.append(discord.Embed.from_dict(item))
        return out or None

    def _store_outgoing(self, payload: dict) -> dict:
        view = _revive_classic_view(payload.get("components"))
        return self.session.add_message(
            channel_id=self.session.channel.id, content=payload.get("content"),
            embeds=self._embeds(payload), view=view,
            ephemeral=bool(payload.get("ephemeral")),
        )

    # ------------------------------------------------ dispatch

    def commands_payload(self) -> dict:
        out = {}
        for command in self._synced_commands:
            out[command.get("name")] = {
                "name": command.get("name"),
                "description": command.get("description") or "",
                "params": [],
            }
        return out

    def status(self) -> dict:
        return {
            "entry": self.entry,
            "sandbox": self.sandbox.name,
            "mode": self.mode,
            "bot": self.session.guild.me.name,
            "ready": self._ready.is_set(),
            "cogs": [],
            "commands": sorted(self.commands_payload()),
            "synced": len(self._synced_commands),
        }

    def _member_payload_for(self, member: pg.MockMember) -> dict:
        return {"id": str(member.id), "username": member.name,
                "global_name": member.name, "bot": False, "avatar": None,
                "discriminator": "0"}

    async def _settle(self) -> None:
        await asyncio.sleep(0.35)  # give the child a beat to answer

    async def dispatch_command(self, name: str, args: dict) -> None:
        if name not in self.commands_payload():
            self.session.log("⚠️", f"/{name} is not registered by this bot", "warn", kind="event",
                             details={"operation": "interaction.command", "command": name,
                                      "status": "missing_command"})
            return
        self._interaction_counter += 1
        self.session.log("⌨️", f"command invoked: /{name}", kind="event",
                         details={"operation": "interaction.command", "command": name,
                                  "arguments": args, "actor": self.session.active_user.name,
                                  "status": "dispatched"})
        self._send({"type": "interaction", "interaction_id": str(self._interaction_counter),
                    "command_name": name, "options": {k: str(v) for k, v in (args or {}).items()},
                    "user": self._member_payload_for(self.session.active_user),
                    "channel_id": str(self.session.channel.id)})
        await self._settle()

    async def dispatch_message(self, content: str, channel_id: str | None = None) -> None:
        session = self.session
        channel = session.channels.get(str(channel_id or ""), session.channel)
        allowed, reason = channel.permission_check(session.active_user, "send_messages")
        if not allowed:
            session.log("🚫", f"message blocked: missing send_messages permission ({reason})", "warn",
                        kind="event", details={"interaction": "message_create", "operation": "message.send",
                                               "channel": channel.name, "actor": session.active_user.name,
                                               "permission": "send_messages", "reason": reason,
                                               "status": "denied"})
            return
        stored = session.add_message(channel_id=channel.id, content=content, author=session.active_user)
        self._send({"type": "message", "message_id": _wire_message_id(stored["id"]),
                    "content": content, "channel_id": str(channel.id),
                    "author": self._member_payload_for(session.active_user)})
        await self._settle()

    async def dispatch_click(self, message_id: str, custom_id: str, values: list) -> None:
        self.session.log("⚠️", "this Node bot registers no component handlers", "warn", kind="event",
                         details={"operation": "interaction.component", "custom_id": custom_id,
                                  "status": "missing_handler"})

    async def dispatch_submit(self, message_id: str | None, custom_id: str, values: dict) -> None:
        self.session.log("⚠️", "this Node bot registers no modal handlers", "warn", kind="event",
                         details={"operation": "interaction.modal_submit", "custom_id": custom_id,
                                  "status": "missing_handler"})

    async def dispatch_pending_modal(self, values: dict) -> bool:
        return False

    # ------------------------------------------------ shutdown

    async def shutdown(self) -> None:
        self._closed = True
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.process.wait(), timeout=5)
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        with contextlib.suppress(Exception):
            shutil.rmtree(self.sandbox, ignore_errors=True)
