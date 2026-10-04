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
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import types
import typing
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
import discord.webhook.async_ as webhook_async
from discord.http import HTTPClient, Route

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
                  ".html", ".css", ".env", ".example"}
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "browser-profile", ".venv", "venv", "backups"}
_SANDBOX_ROOT = Path(
    os.getenv("SCRIPTPLAYGROUND_SANDBOX", Path(tempfile.gettempdir()) / "scriptplayground-sandbox")
)
_ENTRY_NAMES = ("main.py", "bot.py", "index.py", "run.py", "app.py")

# "m5" ↔ 18-digit wire id (below every real-looking snowflake used here)
_WIRE_BASE = 600_000_000_000_000_000


SANDBOX_META = ".playground.json"
"""Written into every worker sandbox so an orphan sweep can prove ownership."""


def _pid_alive(pid: int) -> bool:
    """True if *pid* is a live process.

    Never use os.kill(pid, 0) here: on Windows that calls TerminateProcess and
    would kill the very worker we are trying to protect.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def write_sandbox_meta(sandbox: Path, *, session_id: str, worker_pid: int,
                       server_instance_id: str) -> None:
    """Record who owns a sandbox so a later server can prove it is orphaned."""
    meta = {"session_id": session_id, "worker_pid": int(worker_pid),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "server_instance_id": server_instance_id}
    with contextlib.suppress(OSError):
        (sandbox / SANDBOX_META).write_text(json.dumps(meta), encoding="utf-8")


def sweep_orphan_sandboxes() -> dict:
    """Delete sandboxes whose recorded worker PID is demonstrably dead.

    Conservative by construction: a sandbox without metadata (older builds, or
    a project copied in by hand) is never touched, and a sandbox whose worker
    PID is still alive is left alone even if a second server is running.
    """
    removed, kept, unknown = [], [], []
    root = _SANDBOX_ROOT
    if not root.exists():
        return {"removed": [], "kept": [], "unknown": [], "root": str(root)}
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / SANDBOX_META
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            pid = int(meta["worker_pid"])
        except (OSError, ValueError, KeyError, TypeError):
            unknown.append(entry.name)
            continue
        if _pid_alive(pid):
            kept.append(entry.name)
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
    return {"removed": removed, "kept": kept, "unknown": unknown, "root": str(root)}


def _wire_message_id(session_id: str) -> str:
    n = int(str(session_id).lstrip("m"))
    return str(_WIRE_BASE + n)


def _session_message_id(wire_id: str) -> str | None:
    if not wire_id.isdigit():
        return None
    n = int(wire_id) - _WIRE_BASE
    return f"m{n}" if n > 0 else None


# ------------------------------------------------------------- registries

_ACTIVE_BOOT: contextvars.ContextVar[ProjectRuntime | None] = contextvars.ContextVar(
    "playground_active_boot", default=None
)
_HTTP_RUNTIMES: dict[int, ProjectRuntime] = {}     # id(client.http) -> runtime
_SESSION_RUNTIMES: dict[int, ProjectRuntime] = {}  # id(aiohttp session) -> runtime
_BOOT_LOCK = asyncio.Lock()  # one project boot per process (module cache is shared)

_ORIGINAL_REQUEST = HTTPClient.request
_ORIGINAL_STATIC_LOGIN = HTTPClient.static_login
_ORIGINAL_CLIENT_INIT = discord.Client.__init__
_ORIGINAL_ADAPTER_REQUEST = webhook_async.AsyncWebhookAdapter.request
_ORIGINAL_ASYNCIO_RUN = asyncio.run  # real asyncio.run, captured once at import
_PATCHED = False


def _install_asyncio_run_patch() -> None:
    """Redirect asyncio.run() onto the running simulator loop during a boot.

    Identity-guarded: install refuses to double-patch, and restore only puts
    back the real asyncio.run if the global still holds the shim — so an
    out-of-order shutdown from a stale runtime can never clobber another
    runtime's active patch (or the real one) with a stale reference.
    """
    if asyncio.run is _ORIGINAL_ASYNCIO_RUN:
        asyncio.run = _runtime_asyncio_run_shim


def _restore_asyncio_run_patch() -> None:
    if asyncio.run is _runtime_asyncio_run_shim:
        asyncio.run = _ORIGINAL_ASYNCIO_RUN


def _runtime_asyncio_run_shim(awaitable, *args, **kwargs):  # type: ignore[no-untyped-def]
    """asyncio.run() inside a project can't spawn a second loop while the
    simulator's loop is running; schedule on it instead and return promptly.
    The project's main() keeps running as a managed background task."""
    runtime = _ACTIVE_BOOT.get()
    if runtime is None:  # direct call outside any boot: behave normally-ish
        return _ORIGINAL_ASYNCIO_RUN(awaitable, *args, **kwargs)
    runtime._main_task = asyncio.ensure_future(_wrap_main(awaitable, runtime))
    runtime.session.log("🪄", "asyncio.run() redirected to the simulator loop", kind="action",
                        details={"operation": "asyncio.run", "status": "adapted"})
    return None


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


def _purge_sandbox_modules(sandbox: Path | None = None) -> None:
    """Drop cached modules from a sandbox, or all sandboxes before a fresh boot."""
    root = sandbox.resolve() if sandbox is not None else None
    for name, module in list(sys.modules.items()):
        paths = [str(getattr(module, "__file__", "") or "")]
        paths.extend(str(path) for path in (getattr(module, "__path__", None) or []))
        if (
            root is not None and any(
                Path(path).resolve().is_relative_to(root) for path in paths if path
            )
        ) or (
            root is None and "scriptplayground-sandbox" in ("".join(paths) + name)
        ):
            sys.modules.pop(name, None)


_CWD_STACK: list[ProjectRuntime] = []  # live runtimes, last = sandbox owning the CWD


def _app_dir() -> Path:
    return Path(__file__).resolve().parent


def _chdir_sandbox(runtime: ProjectRuntime) -> None:
    """Park the process CWD inside a runtime's sandbox — no restore-on-exit.

    Bots open sqlite databases, logs, and exports by relative path long after
    boot (lazily, inside command callbacks); restoring CWD after the boot-time
    import let those files spill into the app directory.
    """
    if runtime in _CWD_STACK:
        _CWD_STACK.remove(runtime)
    _CWD_STACK.append(runtime)
    os.chdir(runtime.sandbox)


def _chdir_active_sandbox() -> None:
    """Re-park CWD in the newest still-running sandbox, else the app dir."""
    while _CWD_STACK and not _CWD_STACK[-1].sandbox.is_dir():
        _CWD_STACK.pop()
    if _CWD_STACK:
        os.chdir(_CWD_STACK[-1].sandbox)
    else:
        os.chdir(_app_dir())


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

    def __init__(self, runtime: ProjectRuntime) -> None:
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
        if self.runtime._shutdown_requested:
            raise RuntimeError("project runtime is shutting down")
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
        except Exception as error:
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
                "discriminator": "0", "avatar": member.avatar_url, "bot": bool(member.bot), "public_flags": 0,
                "avatar_url": member.avatar_url, "banner": member.banner_url,
                "bio": member.bio, "accent_color": int(member.accent_color[1:], 16) if member.accent_color else None,
            }
        return {"id": str(fallback_id or BOT_ID), "username": fallback_name or "Bot",
                "global_name": fallback_name or "Bot", "discriminator": "0", "avatar": None,
                "bot": True, "public_flags": 0}

    def _member_payload(self, member: pg.MockMember) -> dict:
        color = member.roles[-1].color.value if member.roles else 0
        return {
            "user": self._user_payload(member), "nick": member.display_name,
            "avatar": member.avatar_url, "banner": member.banner_url,
            "roles": [str(role.id) for role in member.roles if role.id != GUILD_ID],
            "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False,
            "pending": False, "permissions": str(member.guild_permissions.value), "color": color,
            "premium_since": None,
            "communication_disabled_until": None, "flags": 0,
        }

    def _channel_payload(self, channel: pg.MockChannel) -> dict:
        role_ids = {role.id for role in self.session.guild.roles}
        return {
            "id": str(channel.id), "type": 0, "guild_id": str(GUILD_ID), "name": channel.name,
            "topic": channel.topic, "position": 0, "nsfw": False, "last_message_id": None,
            "rate_limit_per_user": 0, "parent_id": None,
            "permission_overwrites": [
                {"id": str(target_id), "type": 0 if target_id in role_ids else 1,
                 "allow": str(overwrite.pair()[0].value), "deny": str(overwrite.pair()[1].value)}
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

    def _message_payload(self, stored: dict, *, edited: bool = False) -> dict:
        author = stored.get("author") or {}
        member = self.session.guild.get_member(int(author.get("id") or BOT_ID))
        payload = {
            "id": _wire_message_id(stored["id"]),
            "channel_id": str(stored.get("channel") or CHANNEL_ID),
            "guild_id": str(GUILD_ID),
            "author": self._user_payload(member, fallback_id=author.get("id"),
                                         fallback_name=author.get("username") or author.get("name")),
            "member": self._member_payload(member) if member else None,
            "content": stored.get("content") or "",
            "timestamp": stored.get("timestamp") or "2024-01-01T00:00:00+00:00",
            "edited_timestamp": (stored.get("timestamp") or "2024-01-01T00:00:00+00:00") if edited else None,
            "tts": False, "mention_everyone": False,
            "mentions": [], "mention_roles": [], "attachments": [], "embeds": stored.get("embeds") or [],
            "pinned": False, "type": 0, "flags": 64 if stored.get("ephemeral") else 0,
        }
        if stored.get("reference"):
            payload["message_reference"] = {"message_id": _wire_message_id(str(stored["reference"])),
                                            "channel_id": payload["channel_id"], "type": 0}
            ref_stored = self.session.messages.get(str(stored["reference"]))
            if ref_stored is not None and not ref_stored.get("deleted"):
                payload["referenced_message"] = self._message_payload(ref_stored)
                payload["type"] = 19  # MessageType.reply
            else:
                payload["referenced_message"] = None
        reactions = stored.get("reactions") or []
        if reactions:
            payload["reactions"] = [
                {"count": len(entry.get("users", [])), "me": str(self.session.guild.me.id) in entry.get("users", []),
                 "count_details": {"burst": 0, "normal": len(entry.get("users", []))},
                 "emoji": {"name": entry["emoji"], "animated": False, "id": None}}
                for entry in reactions
            ]
        return payload

    def _emoji_payload(self, emoji: str) -> dict:
        return {"id": None, "name": str(emoji or ""), "animated": False}

    def gateway_reaction_payload(self, stored: dict, user_id: int, emoji: str) -> dict:
        """MESSAGE_REACTION_ADD / MESSAGE_REACTION_REMOVE as Discord sends it.

        Ids on the wire are snowflakes, so the simulator's "m12" becomes the
        wire id; `member` is included exactly like a real guild payload, which
        is what fills in RawReactionActionEvent.member.
        """
        actor = self.session.guild.get_member(user_id)
        author = stored.get("author") or {}
        return {
            "user_id": str(user_id),
            "channel_id": str(stored.get("channel") or CHANNEL_ID),
            "message_id": _wire_message_id(stored["id"]),
            "guild_id": str(GUILD_ID),
            "emoji": self._emoji_payload(emoji),
            "message_author_id": str(author.get("id") or BOT_ID),
            "member": self._member_payload(actor) if actor is not None else None,
            "burst": False, "type": 0,
        }

    def gateway_message_delete_payload(self, stored: dict) -> dict:
        return {"id": _wire_message_id(stored["id"]),
                "channel_id": str(stored.get("channel") or CHANNEL_ID),
                "guild_id": str(GUILD_ID)}

    def gateway_member_payload(self, member: pg.MockMember) -> dict:
        """GUILD_MEMBER_ADD / GUILD_MEMBER_UPDATE / GUILD_MEMBER_REMOVE body."""
        return {**self._member_payload(member), "guild_id": str(GUILD_ID)}

    def gateway_voice_state_payload(self, member: pg.MockMember, channel_id: int | None) -> dict:
        """VOICE_STATE_UPDATE body. Event semantics only - no audio transport."""
        return {
            "guild_id": str(GUILD_ID),
            "channel_id": str(channel_id) if channel_id is not None else None,
            "user_id": str(member.id),
            "session_id": f"simulated-{member.id}",
            "deaf": False, "mute": False,
            "self_deaf": False, "self_mute": False,
            "self_stream": False, "self_video": False, "suppress": False,
            "request_to_speak_timestamp": None,
        }

    def _store(self, channel: pg.MockChannel, payload: dict, *, ephemeral: bool = False,
               ephemeral_user_id: int | None = None) -> dict:
        """Store an outgoing REST message in the timeline; revive classic views for the UI."""
        view = _revive_classic_view(payload.get("components"))
        stored = self.session.add_message(
            channel_id=channel.id, content=payload.get("content"),
            embeds=_embeds_from_payload(payload.get("embeds")),
            view=view, ephemeral=ephemeral, ephemeral_user_id=ephemeral_user_id,
        )
        self.runtime._cache_bot_message(stored)
        return stored

    def _edit(self, message_id: str | None, payload: dict) -> dict:
        view = payload.get("view") or _revive_classic_view(payload.get("components"))
        before = dict(self.session.messages.get(message_id or "") or {})
        self.session.update_message(
            message_id, content=payload.get("content"),
            embeds=_embeds_from_payload(payload.get("embeds")), view=view,
        )
        stored = self.session.messages.get(message_id) or {}
        self.runtime._publish_message_update(before, stored)
        return stored

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
        return self._user_payload(me) | {"verified": True, "mfa_enabled": False, "flags": 0}

    def _get_users_guilds(self, match, payload, params):
        return [{"id": str(GUILD_ID), "name": self.session.guild.name, "icon": None,
                 "owner": False, "permissions": str(discord.Permissions.all().value), "features": []}]

    def _post_dm_channel(self, match, payload, params):
        recipient_id = int(payload.get("recipient_id") or USER_ID)
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
            self.session._touch()
            self.session.log("➕", f"role added: {member.name} += {role.name}", kind="action",
                             details={"operation": "member.add_roles", "member": member.name,
                                      "role": role.name, "status": "success"})
        return {}

    def _delete_member_role(self, match, payload, params):
        member = self.session.guild.get_member(int(match["user_id"]))
        role = self.session.guild.get_role(int(match["role_id"]))
        if member is not None and role is not None and role in member.roles:
            member.roles.remove(role)
            self.session._touch()
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
        if "permission_overwrites" in payload:
            channel.overwrites.clear()
            for item in payload.get("permission_overwrites") or []:
                target_id = int(item["id"])
                allow = discord.Permissions(int(item.get("allow") or 0))
                deny = discord.Permissions(int(item.get("deny") or 0))
                channel.overwrites[target_id] = discord.PermissionOverwrite.from_pair(allow, deny)
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
            if (stored is None or stored.get("deleted") or stored.get("ephemeral")
                    or stored.get("channel") != str(channel.id)):
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
        message_id = _session_message_id(match["message_id"])
        self.session.delete_message(message_id)
        self.runtime._publish_message_delete(message_id)
        return {}

    def _post_bulk_delete(self, match, payload, params):
        for wire_id in payload.get("messages") or []:
            message_id = _session_message_id(str(wire_id))
            self.session.delete_message(message_id)
            self.runtime._publish_message_delete(message_id)
        self.session.log("🗑️", "REST bulk delete", kind="action",
                         details={"operation": "messages.bulk_delete",
                                  "count": len(payload.get("messages") or []), "status": "success"})
        return {}

    def _post_reaction(self, match, payload, params):
        from urllib.parse import unquote

        self._channel_or_raise(match["channel_id"])
        # Reactions are stored on the session message so they render as pills
        # in the client, matching how the hosted interaction path behaves.
        self.session.toggle_reaction(
            _session_message_id(match["message_id"]), unquote(match["emoji"]),
            user_id=self.session.guild.me.id, actor="bot")
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
        user_id = self.runtime._interaction_users.get(interaction_id, self.session.active_user.id)
        channel = self.session.channels.get(
            self.runtime._interaction_channels.get(interaction_id, ""), self.session.channel
        )
        stored: dict | None = None
        if callback_type in (4, 5):  # channel message with source / deferred "thinking"
            ephemeral = bool(int(data.get("flags") or 0) & 64)
            stored = self._store(channel, data, ephemeral=ephemeral,
                                 ephemeral_user_id=user_id if ephemeral else None)
            self.session.log("💬", f"interaction response → message #{stored['index']}", kind="action",
                             details={"operation": "interaction.response", "message_id": stored["id"],
                                      "callback_type": callback_type, "transport": "project-rest",
                                      "status": "success"})
        elif callback_type == 7:  # component message update
            original = self.runtime._originals.get(interaction_id)
            self._assert_ephemeral_owner(self.session.messages.get(original or ""), interaction_id)
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
            if modal is None and data.get("custom_id"):
                # discord.py calls state.store_view(modal) *after* this response
                # returns, so the real Modal object does not exist yet. Ask the
                # runtime to pick it up once the callback has finished.
                self.runtime._modal_open_request = {
                    "custom_id": data["custom_id"],
                    "source": self.runtime._originals.get(interaction_id),
                    "channel_id": channel.id, "user_id": user_id,
                }
            if modal is not None:
                source = self.runtime._originals.get(interaction_id)
                opened = self.session.open_modal(
                    modal, source, channel_id=channel.id,
                    user_id=user_id, custom_id=data.get("custom_id"),
                )
                self.runtime._pending_modal = (data.get("custom_id"), source, opened["id"])
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
        interaction_id = match["webhook_id"]
        channel = self.session.channels.get(
            self.runtime._interaction_channels.get(interaction_id, ""), self.session.channel
        )
        ephemeral = bool(int(payload.get("flags") or 0) & 64)
        stored = self._store(channel, payload, ephemeral=ephemeral,
                             ephemeral_user_id=self.runtime._interaction_users.get(
                                 interaction_id, self.session.active_user.id
                             ) if ephemeral else None)
        self.session.log("💬", f"followup.send → message #{stored['index']}", kind="action",
                         details={"operation": "interaction.followup", "message_id": stored["id"],
                                  "transport": "project-rest", "status": "success"})
        return self._message_payload(stored)

    def _get_webhook(self, match, payload, params):
        return {"id": match["webhook_id"], "type": 3, "token": match["webhook_token"],
                "application_id": str(BOT_ID), "name": self._me().name,
                "channel_id": self.runtime._interaction_channels.get(
                    match["webhook_id"], str(self.session.channel.id)
                )}

    def _original_target(self, match) -> str | None:
        return self.runtime._originals.get(match["webhook_id"])

    def _assert_ephemeral_owner(self, stored: dict | None, interaction_id: str) -> None:
        if (stored and stored.get("ephemeral")
                and stored.get("ephemeral_user_id") != str(self.runtime._interaction_users.get(interaction_id))):
            raise RuntimeError("ephemeral message is only visible to its interaction user")

    def _get_original(self, match, payload, params):
        interaction_id = match["webhook_id"]
        stored = self.session.messages.get(self._original_target(match) or "")
        self._assert_ephemeral_owner(stored, interaction_id)
        return self._message_payload(stored or {})

    def _patch_original(self, match, payload, params):
        interaction_id = match["webhook_id"]
        message_id = self._original_target(match)
        self._assert_ephemeral_owner(self.session.messages.get(message_id or ""), interaction_id)
        stored = self._edit(message_id, payload)
        return self._message_payload(stored)

    def _delete_original(self, match, payload, params):
        interaction_id = match["webhook_id"]
        message_id = self._original_target(match)
        stored = self.session.messages.get(message_id or "")
        self._assert_ephemeral_owner(stored, interaction_id)
        self.session.delete_message(message_id, actor_id=self.runtime._interaction_users.get(interaction_id))
        self.runtime._publish_message_delete(message_id)
        return {}

    def _get_webhook_message(self, match, payload, params):
        interaction_id = match["webhook_id"]
        stored = self._message_or_raise(match["message_id"])
        self._assert_ephemeral_owner(stored, interaction_id)
        return self._message_payload(stored)

    def _patch_webhook_message(self, match, payload, params):
        interaction_id = match["webhook_id"]
        message_id = _session_message_id(match["message_id"])
        self._assert_ephemeral_owner(self.session.messages.get(message_id or ""), interaction_id)
        stored = self._edit(message_id, payload)
        return self._message_payload(stored)

    def _delete_webhook_message(self, match, payload, params):
        interaction_id = match["webhook_id"]
        message_id = _session_message_id(match["message_id"])
        self._assert_ephemeral_owner(self.session.messages.get(message_id or ""), interaction_id)
        self.session.delete_message(message_id, actor_id=self.runtime._interaction_users.get(interaction_id))
        self.runtime._publish_message_delete(message_id)
        return {}


# ------------------------------------------------------------- runtime


class ProjectRuntime:
    """Boots a real discord.py bot project offline against one simulator session."""

    def __init__(self, session: pg.Session, workspace: Path, tag: str | None = None,
                 on_exception: Callable[[], None] | None = None) -> None:
        self.session = session
        self.workspace = Path(workspace)
        self.on_exception = on_exception
        self.tag = tag or uuid.uuid4().hex[:8]
        self.sandbox = _make_sandbox(self.workspace, self.tag)
        self.bot: discord.Client | None = None
        self.clients: list[discord.Client] = []
        self.transport = ProjectTransport(self)
        self._synced_commands: list[dict] = []
        self._originals: dict[str, str | None] = {}  # interaction id -> original message id
        self._interaction_counter = 9_000_000_000_000_000_000
        self._command_counter = 10_000_000_000_000_000_000
        self._interaction_users: dict[str, int] = {}
        self._interaction_channels: dict[str, str] = {}
        self._main_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._shutdown_requested = False
        self._connected = asyncio.Event()
        self._log_filters = []
        self._pending_modal: tuple[str, str | None, str] | None = None  # (custom_id, source message, modal id)
        self._wire_channels: dict[str, dict] = {}  # channel id -> wire payload (delete events)
        self._pre_member: pg.MockMember | None = None  # member before the current UI mutation
        self._modal_open_request: dict | None = None  # modal send seen before discord.py stored it
        self.entry: str | None = None

    def _record_exception(self, error: BaseException, operation: str) -> dict:
        if self._shutdown_requested:
            return {}
        import traceback

        if operation == "project.command":
            error = getattr(error, "original", None) or error
        sandbox_root = self.sandbox.resolve()
        source_file = None
        line = None
        if isinstance(error, SyntaxError) and error.filename:
            try:
                relative = Path(error.filename).resolve().relative_to(sandbox_root)
            except (OSError, RuntimeError, ValueError):
                pass
            else:
                source_file, line = relative.as_posix(), error.lineno
        if source_file is None:
            for frame in reversed(traceback.extract_tb(error.__traceback__ or None)):
                try:
                    relative = Path(frame.filename).resolve().relative_to(sandbox_root)
                except (OSError, RuntimeError, ValueError):
                    continue
                source_file, line = relative.as_posix(), frame.lineno
                break

        message = error.msg if isinstance(error, SyntaxError) else str(error)
        for path in {str(sandbox_root), str(sandbox_root).replace("\\", "/"), str(sandbox_root).replace("/", "\\")}:
            message = message.replace(path, self.workspace.name)
        traceback_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        sandbox_path = str(sandbox_root)
        for path in {sandbox_path, sandbox_path.replace("\\", "/"), sandbox_path.replace("/", "\\")}:
            traceback_text = traceback_text.replace(path, self.workspace.name)
        traceback_text = re.sub(
            r'(File "[^"]+")', lambda match: match.group(1).replace("\\", "/"), traceback_text
        )
        exception = {
            "type": type(error).__name__, "message": message, "file": source_file,
            "line": line, "workspace": self.workspace.name, "traceback": traceback_text,
        }
        self.session.last_run = {"ok": False, "error": traceback_text, "exception": exception, "ms": 0.0}
        location = f" at {source_file}:{line}" if source_file and line is not None else ""
        self.session.log("💥", f"{exception['type']}: {exception['message']}{location}", "error",
                         details={key: value for key, value in exception.items() if key != "traceback"}
                         | {"operation": operation, "status": "script_error"})
        if self.on_exception is not None:
            self.on_exception()
        return exception

    def _install_error_reporting(self, bot: discord.Client) -> None:
        import traceback

        logger = logging.getLogger("discord.app_commands.tree")
        sandbox_root = str(self.sandbox.resolve())
        sandbox_paths = (sandbox_root, sandbox_root.replace("\\", "/"),
                         sandbox_root.replace("/", "\\"))

        def sanitize(record):
            if record.exc_info:
                original = "".join(traceback.format_exception(*record.exc_info))
                text = original
                for path in sandbox_paths:
                    text = text.replace(path, self.workspace.name)
                if text != original:
                    record.exc_text = text
                    record.exc_info = None
            message = record.getMessage()
            for path in sandbox_paths:
                message = message.replace(path, self.workspace.name)
            if message != record.getMessage():
                record.msg, record.args = message, ()
            return True

        current = logger
        while current is not None:
            for handler in current.handlers:
                handler.addFilter(sanitize)
                self._log_filters.append((handler, sanitize))
            if not current.propagate:
                break
            current = current.parent
        original_run_event = bot._run_event

        async def report_event(coro, event_method, *args, **kwargs):
            async def capture(*event_args, **event_kwargs):
                try:
                    result = coro(*event_args, **event_kwargs)
                    if inspect_awaitable(result):
                        return await result
                    return result
                except asyncio.CancelledError:
                    raise
                except BaseException as error:  # noqa: BLE001 - preserve hosted callback failures
                    self._record_exception(error, f"project.event.{event_method}")
                    return None

            return await original_run_event(capture, event_method, *args, **kwargs)

        bot._run_event = report_event

        tree = getattr(bot, "tree", None)
        if tree is not None:
            original_tree_error = tree.on_error

            async def report_command(interaction, error):
                self._record_exception(error, "project.command")
                if self.on_exception is not None:
                    return
                result = original_tree_error(interaction, error)
                if inspect_awaitable(result):
                    await result

            tree.on_error = report_command

        original_command_error = getattr(bot, "on_command_error", None)
        if callable(original_command_error):
            async def report_prefix_command(context, error):
                self._record_exception(error, "project.command")
                if self.on_exception is not None:
                    return
                result = original_command_error(context, error)
                if inspect_awaitable(result):
                    await result

            bot.on_command_error = report_prefix_command

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
            _purge_sandbox_modules(self.sandbox)
            # Projects legitimately call asyncio.run() in their entry (a fresh
            # loop is fine standalone, but this server loop is already running).
            # Redirect it for the duration of the boot under the global lock.
            _install_asyncio_run_patch()
            try:
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
            finally:
                _restore_asyncio_run_patch()

    def _import_entry(self, entry: Path):
        token = None
        try:
            # Park CWD in the sandbox for the runtime's whole life (restored in
            # shutdown): relative sqlite/log writes during dispatch must stay
            # inside the emulated folder.
            _chdir_sandbox(self)  # sqlite/env filenames resolve like a real launch
            token = _ACTIVE_BOOT.set(self)
            spec = importlib.util.spec_from_file_location(f"_scriptplayground_{uuid.uuid4().hex}", entry)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
        finally:
            if token is not None:
                _ACTIVE_BOOT.reset(token)

    def _asyncio_run_shim(self, awaitable, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Deprecated: the shim moved to module level. Fail loudly if referenced."""
        raise RuntimeError("_asyncio_run_shim moved to module-level _runtime_asyncio_run_shim")

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
        self._install_error_reporting(bot)
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
            "user": self.transport._user_payload(me),
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

    def refresh_guild_state(self) -> None:
        """Refresh hosted discord.py member and channel caches after scenario mutations."""
        bot = self.bot
        guild = bot.get_guild(GUILD_ID) if bot is not None else None
        if guild is None:
            return
        for role in self.session.guild.roles:
            cached_role = guild.get_role(role.id)
            if cached_role is not None:
                cached_role._update(self.transport._role_payload(role))
        if guild._members is None:
            guild._members = {}
        for member in self.session.guild.members:
            payload = self.transport._member_payload(member)
            cached = guild.get_member(member.id)
            if cached is None:
                cached = discord.Member(data=payload, guild=guild, state=bot._connection)
                guild._add_member(cached)
            else:
                cached._update(payload)
        for channel in self.session.channels.values():
            payload = self.transport._channel_payload(channel)
            if guild.get_channel(channel.id) is None:
                bot._connection.parse_channel_create(payload)
            else:
                bot._connection.parse_channel_update(payload)

    def refresh_member_profile(self, _member: pg.MockMember) -> None:
        """Compatibility hook for scenario profile updates."""
        self.refresh_guild_state()

    def _interaction_payload(self, *, kind: str, message_id: str | None = None,
                             custom_id: str | None = None, values: Any = None,
                             data: dict | None = None, channel_id=None,
                             user_id: int | None = None) -> dict:
        session = self.session
        stored = session.messages.get(message_id or "") or {}
        channel = session.channels.get(str(stored.get("channel") or channel_id or ""), session.channel)
        interaction_id = self._next_interaction_id()
        member = session.guild.get_member(user_id or session.active_user.id)
        payload = {
            "id": interaction_id,
            "application_id": str(BOT_ID),
            "type": 2 if kind == "command" else (3 if kind == "click" else 5),
            "token": _PLACEHOLDER_TOKEN,
            "version": 1,
            "guild_id": str(GUILD_ID),
            "channel": self.transport._channel_payload(channel),
            "channel_id": str(channel.id),
            "locale": "en-US",
            "guild_locale": "en-US",
            "app_permissions": str(channel.permissions_for(session.guild.me).value),
            "attachment_size_limit": 26214400,
            "entitlements": [],
            "authorizing_integration_owners": {},
            "member": self.transport._member_payload(member),
        }
        payload["member"]["permissions"] = str(member.guild_permissions.value)
        payload["permissions"] = str(channel.permissions_for(member).value)
        payload["app_permissions"] = str(channel.permissions_for(session.guild.me).value)
        self._interaction_users[interaction_id] = member.id
        self._interaction_channels[interaction_id] = str(channel.id)
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

    _COMPONENT_TYPE_BY_KIND: typing.ClassVar[dict[str, int]] = {"button": 2, "select": 3, "user_select": 5, "role_select": 6,
                                               "mentionable_select": 7, "channel_select": 8}

    def _component_type(self, message_id: str | None, custom_id: str | None) -> int:
        stored = self.session.messages.get(message_id or "") or {}
        item = pg._find_component(stored.get("components") or [], stored.get("v2") or [], custom_id or "")
        return self._COMPONENT_TYPE_BY_KIND.get((item or {}).get("kind"), 2)

    _OPTION_TYPES: typing.ClassVar[dict[str, int]] = {"string": 3, "integer": 4, "boolean": 5, "user": 6, "channel": 7,
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

    async def dispatch_command(self, name: str, args: dict, channel_id=None) -> None:
        command = None
        if self.bot is not None and getattr(self.bot, "tree", None) is not None:
            command = self.bot.tree.get_command(name)
        if command is None:
            self.session.log("⚠️", f"/{name} is not registered by this bot", "warn", kind="event",
                             details={"operation": "interaction.command", "command": name,
                                      "status": "missing_command"})
            return
        channel = self.session.channels.get(str(channel_id or ""), self.session.channel)
        allowed, reason = channel.permission_check(self.session.active_user, "view_channel")
        if not allowed:
            self.session.log("🚫", f"command blocked: missing view_channel permission ({reason})", "warn",
                             kind="event", details={"operation": "interaction.command", "command": name,
                                                      "status": "denied", "permission": "view_channel",
                                                      "reason": reason})
            return
        event = self.session.log("⌨️", f"command invoked: /{name}", kind="event",
                                 details={"operation": "interaction.command", "command": name,
                                          "arguments": args, "actor": self.session.active_user.name,
                                          "channel": channel.name, "status": "dispatched"})
        from playground import _ACTIVE_EVENT
        token = _ACTIVE_EVENT.set(event["id"])
        try:
            payload = self._interaction_payload(kind="command", data=self._command_data(name, args),
                                                channel_id=channel.id)
            self._call_interaction(payload)
            await self._drain()
        finally:
            _ACTIVE_EVENT.reset(token)

    async def dispatch_click(self, message_id: str, custom_id: str, values: list) -> None:
        message = self.session.messages.get(message_id) or {}
        details = {"operation": "interaction.component", "custom_id": custom_id,
                   "message_id": message_id, "actor": self.session.active_user.name}
        if message.get("deleted"):
            self.session.log("⚠️", "component message is no longer available", "warn", kind="event",
                             details={**details, "status": "invalid_message"})
            return
        if message.get("ephemeral") and message.get("ephemeral_user_id") != str(self.session.active_user.id):
            self.session.log("🚫", "ephemeral message is only visible to its interaction user", "warn", kind="event",
                             details={**details, "status": "denied"})
            return
        channel = self.session.channels.get(str(message.get("channel") or ""), self.session.channel)
        allowed, reason = channel.permission_check(self.session.active_user, "view_channel")
        if not allowed:
            self.session.log("🚫", f"component use blocked: missing view_channel permission ({reason})", "warn",
                             kind="event", details={**details, "status": "denied",
                                                     "permission": "view_channel", "reason": reason})
            return
        component = pg._find_component(message.get("components") or [], message.get("v2") or [], custom_id)
        if message.get("deleted") or component is None or component.get("disabled"):
            self.session.log("⚠️", f"component {custom_id!r} is not available on this message", "warn",
                             kind="event", details={**details, "status": "invalid_component"})
            return
        event = self.session.log("🖱️", f"component used: {custom_id!r}", kind="event",
                                 details={**details, "channel": channel.name, "status": "attempted"})
        from playground import _ACTIVE_EVENT
        token = _ACTIVE_EVENT.set(event["id"])
        try:
            payload = self._interaction_payload(kind="click", message_id=message_id,
                                                custom_id=custom_id, values=values)
            self._call_interaction(payload)
            await self._drain()
            self._settle_modal_open()
        finally:
            _ACTIVE_EVENT.reset(token)
        if not event.get("action_ids"):
            self.session.log("⚠️", "that interaction was never answered — real Discord shows 'This interaction failed'",
                             "warn", kind="event", details={**details, "status": "unanswered"})

    async def dispatch_submit(self, message_id: str, custom_id: str, values: dict) -> None:
        self.session.log("📝", f"modal submitted: {custom_id!r}", kind="event",
                         details={"operation": "interaction.modal_submit", "custom_id": custom_id,
                                  "actor": self.session.active_user.name, "status": "dispatched"})
        payload = self._interaction_payload(kind="submit", message_id=message_id,
                                            custom_id=custom_id, values=values)
        self._call_interaction(payload)
        await self._drain()

    async def dispatch_pending_modal(self, values: dict, modal_id: str | None = None) -> bool:
        """Submit the UI-filled modal the bot last opened. False if none is open."""
        if self._pending_modal is None:
            return False
        custom_id, _source, expected_modal_id = self._pending_modal
        if modal_id and modal_id != expected_modal_id:
            return False
        modal = next((item for item in reversed(self.session.modals)
                      if item.get("id") == expected_modal_id), None)
        if modal is None:
            self._pending_modal = None
            return False
        if modal.get("user_id") != str(self.session.active_user.id):
            self.session.log("🚫", "modal is only visible to the user who opened it", "warn", kind="event",
                             details={"operation": "interaction.modal_submit", "modal_id": expected_modal_id,
                                      "actor": self.session.active_user.name, "status": "denied"})
            return True
        invalid = next((item for item in modal.get("items", [])
                        if (item.get("required") and not pg._modal_value(values, item).strip())
                        or len(pg._modal_value(values, item)) < (item.get("min_length") or 0)
                        or len(pg._modal_value(values, item)) > (item.get("max_length") or 4000)), None)
        if invalid is not None:
            self.session.log("⚠️", "modal submission contains an invalid field", "warn", kind="event",
                             details={"operation": "interaction.modal_submit", "modal_id": expected_modal_id,
                                      "field": invalid.get("custom_id"), "status": "invalid_form"})
            return True
        from playground import _ACTIVE_EVENT
        source = _source
        source_message = self.session.messages.get(source or "") or {}
        channel = self.session.channels.get(modal.get("channel_id") or source_message.get("channel") or "",
                                           self.session.channel)
        event = self.session.log("📝", f"modal submitted: {custom_id!r}", kind="event",
                                 details={"interaction": "modal_submit", "modal_id": expected_modal_id,
                                          "custom_id": custom_id, "actor": self.session.active_user.name,
                                          "channel": channel.name, "status": "attempted"})
        payload = self._interaction_payload(kind="submit", message_id=source,
                                            custom_id=custom_id, values=values, user_id=int(modal["user_id"]))
        payload["channel_id"] = str(channel.id)
        payload["channel"] = {"id": str(channel.id), "type": 0, "name": channel.name}
        self._originals[payload["id"]] = source
        self.session.modals.remove(modal)
        self.session._touch()
        self._pending_modal = None
        token = _ACTIVE_EVENT.set(event["id"])
        try:
            self._call_interaction(payload)
            await self._drain()
            await self._settle_modal_error(event, custom_id, channel)
        finally:
            _ACTIVE_EVENT.reset(token)
        return True

    def _settle_modal_open(self) -> None:
        """Open the simulated modal now that discord.py has stored the real Modal."""
        request = self._modal_open_request
        self._modal_open_request = None
        if request is None:
            return
        bot = self.bot
        store = getattr(bot._connection, "_view_store", None) if bot is not None else None
        modal = store._modals.get(request["custom_id"]) if store is not None else None
        if modal is None:
            self.session.log("⚠️", "modal was sent but no Modal was registered", "warn",
                             kind="event", details={"layer": "simulator", "event": "MODAL_OPEN",
                                                    "operation": "interaction.send_modal",
                                                    "status": "missing_modal",
                                                    "custom_id": request["custom_id"]})
            return
        opened = self.session.open_modal(modal, request["source"], channel_id=request["channel_id"],
                                         user_id=request["user_id"], custom_id=request["custom_id"])
        self._pending_modal = (request["custom_id"], request["source"], opened["id"])
        self.session.log("📋", "modal opened by interaction", kind="action",
                         details={"operation": "interaction.send_modal",
                                  "custom_id": request["custom_id"], "status": "success"})

    async def _settle_modal_error(self, event: dict, custom_id: str, channel: pg.MockChannel) -> None:
        await asyncio.sleep(0)
        if not event.get("action_ids"):
            self.session.log("⚠️", "modal submit received but the bot did not acknowledge it",
                             "warn", kind="event",
                             details={"interaction": "modal_submit", "custom_id": custom_id,
                                      "channel": channel.name, "status": "unanswered"})

    def dismiss_modal(self, modal_id: str) -> bool:
        pending = self._pending_modal
        if pending is None or pending[2] != modal_id:
            self.session.dismiss_modal(modal_id, self.session.active_user.id)
            return False
        result = self.session.dismiss_modal(modal_id, self.session.active_user.id)
        if result:
            self._pending_modal = None
        return result

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
        bot = self.bot
        if bot is None:
            raise RuntimeError("no project bot is running")
        stored = session.add_message(channel_id=channel.id, content=content, author=session.active_user)
        event = session.log("💬", f"message received from {session.active_user.name}", kind="event",
                            details={"interaction": "message_create", "operation": "message.send",
                                     "message_id": stored["id"], "channel": channel.name,
                                     "actor": session.active_user.name, "content": content,
                                     "status": "dispatched"})
        from playground import _ACTIVE_EVENT
        token = _ACTIVE_EVENT.set(event["id"])
        try:
            bot._connection.parse_message_create(self.transport._message_payload(stored))
            await self._drain()
        finally:
            _ACTIVE_EVENT.reset(token)

    _GATEWAY_PARSERS: typing.ClassVar[dict[str, str]] = {
        "raw_reaction_add": "parse_message_reaction_add",
        "raw_reaction_remove": "parse_message_reaction_remove",
        "message_delete": "parse_message_delete",
        "member_join": "parse_guild_member_add",
        "member_remove": "parse_guild_member_remove",
        "member_update": "parse_guild_member_update",
        "voice_state": "parse_voice_state_update",
        "guild_channel_create": "parse_channel_create",
        "guild_channel_delete": "parse_channel_delete",
        "guild_role_create": "parse_guild_role_create",
        "guild_role_delete": "parse_guild_role_delete",
    }

    def _gateway_payload(self, kind: str, payload: dict) -> dict:
        """Build the exact gateway body the ConnectionState parser expects.

        Reactions and message deletes resolve message ids from the session and
        emit *wire* snowflakes; before/after diffing for member_update and
        voice_state is deliberately left to discord.py, which diffs the payload
        against its own cache exactly like the real client does.
        """
        session = self.session
        if kind in ("raw_reaction_add", "raw_reaction_remove"):
            stored = session.messages.get(str(payload.get("message_id") or ""))
            if stored is None or stored.get("deleted"):
                raise ValueError("message no longer exists")
            user_id = int(payload.get("user_id") or session.user_id)
            return self.transport.gateway_reaction_payload(stored, user_id,
                                                           str(payload.get("emoji") or ""))
        if kind == "message_delete":
            stored = session.messages.get(str(payload.get("message_id") or ""))
            if stored is None:
                raise ValueError("message no longer exists")
            return self.transport.gateway_message_delete_payload(stored)
        if kind in ("member_join", "member_update"):
            member = self._member_or_raise(payload)
            return self.transport.gateway_member_payload(member)
        if kind == "member_remove":
            member = payload.get("_member")
            if member is None:
                raise ValueError("member no longer exists")
            return self.transport.gateway_member_payload(member)
        if kind == "voice_state":
            member = self._member_or_raise(payload)
            return self.transport.gateway_voice_state_payload(member, session.voice_channel)
        if kind in ("guild_channel_create", "guild_channel_delete"):
            channel_id = str(payload.get("channel_id") or "")
            if kind == "guild_channel_create":
                channel = session.channels.get(channel_id)
                if channel is None:
                    raise ValueError("channel no longer exists")
                return self.transport._channel_payload(channel)
            wire = self._wire_channels.get(channel_id)
            if wire is None:
                raise ValueError(f"unknown channel {channel_id}")
            return wire
        if kind == "guild_role_create":
            # the gateway nests the role object; a flat payload raises KeyError in
            # ConnectionState.parse_guild_role_create
            role = session.guild.get_role(int(payload.get("role_id") or 0))
            if role is None:
                raise ValueError("role no longer exists")
            return {"guild_id": str(GUILD_ID), "role": self.transport._role_payload(role)}
        if kind == "guild_role_delete":
            # delete carries only the id, and the role is already gone from the
            # simulated world by the time the event is emitted
            role_id = int(payload.get("role_id") or 0)
            if role_id <= 0:
                raise ValueError("role no longer exists")
            return {"guild_id": str(GUILD_ID), "role_id": str(role_id)}
        raise ValueError(f"unsupported event {kind!r}")

    def _member_or_raise(self, payload: dict) -> pg.MockMember:
        member = self.session.guild.get_member(int(payload.get("user_id") or 0))
        if member is None:
            raise ValueError("member no longer exists")
        return member

    async def dispatch_event(self, kind: str, payload: dict | None = None) -> bool:
        """Simulated Discord event -> real gateway payload -> bot listener.

        This is the single pipeline every UI-side event family uses. The
        simulator never calls a bot callback: it feeds the same
        `ConnectionState.parse_*` entry point the real gateway shard uses, so
        discord.py builds the real objects (RawReactionActionEvent, Member,
        VoiceState, ...) and its own event plumbing runs unchanged.
        """
        bot = self.bot
        if bot is None:
            raise RuntimeError("no project bot is running")
        parser_name = self._GATEWAY_PARSERS.get(kind)
        if parser_name is None:
            self.session.log("⚠️", f"SIMULATOR ERROR: unsupported simulated event {kind!r}",
                             "warn", kind="event",
                             details={"layer": "simulator", "event": str(kind), "status": "dropped",
                                      "reason": "unsupported payload",
                                      "supported": sorted(self._GATEWAY_PARSERS)})
            return False
        try:
            data = self._gateway_payload(kind, dict(payload or {}))
        except (KeyError, TypeError, ValueError) as error:
            self.session.log("⚠️", f"SIMULATOR ERROR: cannot build {kind.upper()}", "warn",
                             kind="event",
                             details={"layer": "simulator", "event": kind.upper(),
                                      "operation": f"gateway.{kind}", "status": "dropped",
                                      "reason": f"{type(error).__name__}: {error}"})
            return False
        event = self.session.log("🔀", f"simulated {kind} → bot event", kind="event",
                                 details={"layer": "gateway", "event": kind.upper(),
                                          "operation": f"gateway.{kind}", "status": "dispatched"})
        from playground import _ACTIVE_EVENT
        token = _ACTIVE_EVENT.set(event["id"])
        try:
            getattr(bot._connection, parser_name)(data)
            await self._drain()
        except Exception as error:  # noqa: BLE001 - a bad payload must not kill the worker
            self.session.log("❌", f"WORKER ERROR dispatching {kind.upper()}: "
                                   f"{type(error).__name__}: {error}", "error", kind="event",
                             details={"layer": "worker", "event": kind.upper(),
                                      "operation": f"gateway.{kind}", "status": "failed",
                                      "exception": f"{type(error).__name__}: {error}",
                                      "traceback": traceback.format_exc()[-1200:]})
            return False
        finally:
            _ACTIVE_EVENT.reset(token)
        return True

    async def apply_ui_action(self, method: str, args: list | None = None,
                              kwargs: dict | None = None, event: str | None = None) -> Any:
        """Run a UI-originated Session mutation, then deliver its gateway event.

        Both halves happen inside the worker and in this order, so the bot sees
        the same ordering a real client produces: world state first, event
        second - and discord.py's cache still holds the *pre*-mutation member
        or voice state, which is what makes before/after diffs meaningful.
        """
        call_args = list(args or [])
        call_kwargs = {key: value for key, value in (kwargs or {}).items()
                       if key != "event_user_id"}  # names the event target, not a session arg
        event_target = int((kwargs or {}).get("event_user_id")
                           or (kwargs or {}).get("user_id") or self.session.user_id)
        if event is not None:
            self._capture_pre_state()
            # kick/ban return nothing, so remember who the target was
            self._pre_member = self.session.guild.get_member(event_target)
        result = getattr(self.session, method)(*call_args, **call_kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if event is not None:
            await self._dispatch_ui_event(method, event, result, call_args, event_target)
        return result

    def _capture_pre_state(self) -> None:
        """Snapshot wire payloads a delete event will need after the mutation."""
        for channel in self.session.channels.values():
            self._wire_channels[str(channel.id)] = self.transport._channel_payload(channel)

    async def _dispatch_ui_event(self, method: str, event: str, result: Any,
                                 args: list, user_id: int) -> None:
        """Translate a UI action into the gateway event the mutation implies."""
        session = self.session
        if event == "reaction":
            payload = {"message_id": args[0], "user_id": user_id, "emoji": args[1]}
            await self.dispatch_event(
                "raw_reaction_add" if result else "raw_reaction_remove", payload)
            return
        if event == "message_delete":
            await self.dispatch_event("message_delete", {"message_id": args[0]})
            return
        if event == "member_join":
            member = result if isinstance(result, pg.MockMember) else session.guild.get_member(user_id)
            if member is None:
                self.session.log("⚠️", "SIMULATOR ERROR: member_join has no member to deliver",
                                 "warn", kind="event",
                                 details={"layer": "simulator", "event": "GUILD_MEMBER_ADD",
                                          "operation": "gateway.member_join", "status": "dropped",
                                          "reason": "member no longer exists"})
                return
            await self.dispatch_event("member_join", {"user_id": member.id})
            return
        if event == "member_remove":
            member = result if isinstance(result, pg.MockMember) else self._pre_member
            if member is None:
                self.session.log("⚠️", "SIMULATOR ERROR: member_remove has no member to deliver",
                                 "warn", kind="event",
                                 details={"layer": "simulator", "event": "GUILD_MEMBER_REMOVE",
                                          "operation": "gateway.member_remove", "status": "dropped",
                                          "reason": "member no longer exists"})
                return
            await self.dispatch_event("member_remove", {"_member": member})
            return
        if event == "member_update":
            await self.dispatch_event("member_update", {"user_id": user_id})
            return
        if event == "voice_state":
            await self.dispatch_event("voice_state", {"user_id": user_id})
            return
        if event in ("guild_channel_create", "guild_channel_delete"):
            channel = result if isinstance(result, pg.MockChannel) else None
            channel_id = str(getattr(channel, "id", "") or "")
            await self.dispatch_event(event, {"channel_id": channel_id})
            return
        if event in ("guild_role_create", "guild_role_delete"):
            role = result if isinstance(result, MockRole) else None
            await self.dispatch_event(event, {"role_id": int(getattr(role, "id", 0) or 0)})
            return
        raise ValueError(f"unsupported UI event {event!r}")

    def _publish_message_update(self, before: dict, after: dict) -> None:
        """MESSAGE_UPDATE for a bot-side edit.

        The transport has already moved the simulated world; this hands the
        gateway payload to discord.py's own parser so `on_message_edit` /
        `on_raw_message_edit` fire exactly like they do for a live edit. If the
        message is not in the bot's cache the library drops it, which is real
        discord.py behaviour and is deliberately not bypassed.
        """
        bot = self.bot
        if bot is None or not before or after.get("deleted"):
            return
        if getattr(bot._connection, "_messages", None) is None:
            return
        bot._connection.parse_message_update(self.transport._message_payload(after, edited=True))

    def _publish_message_delete(self, message_id: str | None) -> None:
        """MESSAGE_DELETE after the world mutation, so the bot is told like live."""
        bot = self.bot
        if bot is None:
            return
        stored = self.session.messages.get(message_id or "")
        if stored is None:
            return
        bot._connection.parse_message_delete(self.transport.gateway_message_delete_payload(stored))

    def _cache_bot_message(self, stored: dict) -> None:
        """Cache an outgoing message so reaction/delete events can resolve it.

        Real Discord echoes your own message back over the gateway, but a
        discord.py bot must not receive it as `on_message` - so we build the
        Message object and put it in the cache without dispatching. That is what
        lets on_reaction_add / on_raw_message_delete resolve bot-sent messages.
        """
        bot = self.bot
        if bot is None:
            return
        cache = getattr(bot._connection, "_messages", None)
        if cache is None:
            return
        data = self.transport._message_payload(stored)
        channel, _guild = bot._connection._get_guild_channel(data)
        if channel is None or not data.get("author"):
            return
        cache.append(discord.Message(channel=channel, data=data, state=bot._connection))

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

    def _begin_cleanup(self, session_obj) -> asyncio.Task:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._finish_shutdown(session_obj))
        return self._cleanup_task

    async def _finish_shutdown(self, session_obj) -> None:
        for handler, log_filter in self._log_filters:
            handler.removeFilter(log_filter)
        self._log_filters.clear()
        with contextlib.suppress(ValueError):
            sys.path.remove(str(self.sandbox))
        if self in _CWD_STACK:
            _CWD_STACK.remove(self)
        for bot in list(self.clients):
            _HTTP_RUNTIMES.pop(id(bot.http), None)
        for bot in self.clients:
            http_session = _http_session(bot.http)
            if http_session is not None:
                _SESSION_RUNTIMES.pop(id(http_session), None)
        if session_obj is not None:
            with contextlib.suppress(Exception):
                await session_obj.close()
        with contextlib.suppress(OSError):
            _chdir_active_sandbox()  # leave the sandbox before it is deleted
        with contextlib.suppress(Exception):
            shutil.rmtree(self.sandbox, ignore_errors=True)
        _purge_sandbox_modules(self.sandbox)
        if getattr(self.session, "_project_runtime_shutdown_pending", None) is self:
            del self.session._project_runtime_shutdown_pending

    async def shutdown(self) -> None:
        if self._shutdown_requested:
            if self._cleanup_task is not None:
                await asyncio.shield(self._cleanup_task)
            return
        self._shutdown_requested = True
        self._connected.set()  # release any bot parked in connect()
        session_obj = None
        for bot in self.clients:
            http_session = _http_session(bot.http)
            if http_session is not None:
                session_obj = http_session
        if self.bot is not None and not self.bot.is_closed():
            with contextlib.suppress(Exception):
                await self.bot.close()
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
            try:
                async with asyncio.timeout(0.5):
                    await asyncio.shield(self._main_task)
            except TimeoutError:
                self._main_task.cancel()
            except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001
                if not isinstance(error, asyncio.CancelledError):
                    log.debug("Hosted main task errored during shutdown: %s", error)
            if not self._main_task.done():
                # asyncio can't kill a coroutine that suppresses cancellation.
                # Quarantine effects and retain resources until it really exits.
                self.session._project_runtime_shutdown_pending = self
                self._main_task.add_done_callback(lambda _: self._begin_cleanup(session_obj))
                return
        await asyncio.shield(self._begin_cleanup(session_obj))

def project_shutdown_pending(session: pg.Session) -> bool:
    """Whether a cancellation-resistant hosted project still owns this session."""
    return getattr(session, "_project_runtime_shutdown_pending", None) is not None


def inspect_awaitable(value) -> bool:  # tiny alias to keep imports honest
    import inspect

    return inspect.isawaitable(value)


async def _wrap_main(awaitable, runtime: ProjectRuntime) -> None:
    """Entry main() runs as a background task; failures surface as events."""
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except BaseException as error:  # noqa: BLE001
        runtime._record_exception(error, "project.main")


async def run_project(session: pg.Session, workspace: Path, tag: str | None = None,
                      on_exception: Callable[[], None] | None = None):
    """Create + boot a runtime (Python or Node); raises with a logged event on failure."""
    workspace = Path(workspace)
    runtime: ProjectRuntime | NodeProjectRuntime
    if _is_node_project(workspace):
        runtime = NodeProjectRuntime(session, workspace, tag)
    else:
        runtime = ProjectRuntime(session, workspace, tag, on_exception)
    try:
        await runtime.boot()
    except asyncio.CancelledError:
        await runtime.shutdown()
        raise
    except BaseException as error:
        if isinstance(runtime, ProjectRuntime):
            runtime._record_exception(error, "project.run")
        else:
            import traceback

            text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            session.log("💥", f"project boot failed: {error}", "error",
                        details={"operation": "project.run", "status": "script_error",
                                 "type": type(error).__name__, "message": str(error),
                                 "traceback": text[-4000:]})
        await runtime.shutdown()
        if not isinstance(error, Exception):
            raise RuntimeError(f"{type(error).__name__}: {error}") from error  # noqa: TRY004
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
        self._interaction_users: dict[str, int] = {}
        self._interaction_channels: dict[str, str] = {}
        self._interaction_replies: dict[str, str] = {}
        self._interaction_source_messages: dict[str, str] = {}
        self._interaction_events: dict[str, dict] = {}
        self._interaction_done: dict[str, asyncio.Event] = {}
        self._interaction_ack: dict[str, bool] = {}
        self._guild_cache_revision = 0
        self._pending_modal: tuple[str, str | None, str] | None = None
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
        except BaseException as error:
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
        interaction_id = str(payload.get("interaction_id") or "")
        event = self._interaction_events.get(interaction_id)
        if event is None:
            await self._handle_event_impl(payload)
            return
        from playground import _ACTIVE_EVENT

        token = _ACTIVE_EVENT.set(event["id"])
        try:
            await self._handle_event_impl(payload)
        finally:
            _ACTIVE_EVENT.reset(token)

    async def _handle_event_impl(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "hello":
            # Seed discord.js caches like the initial guild/member/channel gateway events.
            guild = self.session.guild
            self._send({"type": "ready", "user": self._bot_user(),
                        "guild": {"id": str(GUILD_ID), "name": guild.name,
                                  "members": [self._member_payload(member) for member in guild.members],
                                  "roles": [self._role_payload(role) for role in guild.roles],
                                  "channels": [self._channel_payload(channel)
                                               for channel in self.session.channels.values()]}})
            self._guild_cache_revision = self.session.revision
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
            channel = self.session.channels.get(str(payload.get("channel_id") or ""), self.session.channel)
            self.session.log("↗️", f"channel.send → #{channel.name}", kind="action",
                             details={"operation": "channel.send", "channel": channel.name,
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
            interaction_id = str(payload.get("interaction_id"))
            followup = bool(payload.get("followup"))
            if not followup:
                self._interaction_ack[interaction_id] = True
            channel = self.session.channels.get(
                self._interaction_channels.get(interaction_id, ""), self.session.channel
            )
            outgoing = {**payload, "channel_id": channel.id}
            ephemeral = bool(payload.get("ephemeral"))
            if ephemeral:
                outgoing["ephemeral_user_id"] = self._interaction_users.get(interaction_id)
            if followup or interaction_id not in self._interaction_replies:
                stored = self._store_outgoing(outgoing)
                if not followup:
                    self._interaction_replies[interaction_id] = stored["id"]
            else:
                message_id = self._interaction_replies[interaction_id]
                stored = self.session.messages.get(message_id) or {}
                self.session.update_message(message_id, content=payload.get("content"),
                                            embeds=self._embeds(payload),
                                            view=_revive_classic_view(payload.get("components")))
                if stored.get("ephemeral"):
                    stored["ephemeral_user_id"] = self._interaction_users.get(interaction_id)
            self.session.log("💬", f"interaction reply → message #{stored['index']}", kind="action",
                             details={"operation": "interaction.followup" if followup else "interaction.response",
                                      "message_id": stored["id"], "runtime": "node-shim",
                                      "status": "success"})
        elif kind == "interaction_defer":
            interaction_id = str(payload.get("interaction_id"))
            self._interaction_ack[interaction_id] = True
            channel = self.session.channels.get(
                self._interaction_channels.get(interaction_id, ""), self.session.channel
            )
            if payload.get("thinking"):
                deferred = self.session.add_message(
                    channel_id=channel.id, content="Thinking…", author=self.session.guild.me,
                    ephemeral=bool(payload.get("ephemeral")),
                    ephemeral_user_id=self._interaction_users.get(interaction_id)
                    if payload.get("ephemeral") else None,
                )
                self._interaction_replies[interaction_id] = deferred["id"]
            else:
                self._interaction_replies[interaction_id] = self._interaction_source_messages.get(interaction_id)
            self.session.log("💭", "interaction deferred (thinking)" if payload.get("thinking") else "interaction deferred",
                             kind="action", details={"operation": "interaction.defer", "runtime": "node-shim",
                                                       "status": "success"})
        elif kind == "interaction_delete":
            interaction_id = str(payload.get("interaction_id"))
            message_id = (self._interaction_replies.get(interaction_id)
                          or self._interaction_source_messages.get(interaction_id))
            stored = self.session.messages.get(message_id or "")
            if stored and stored.get("ephemeral") and stored.get("ephemeral_user_id") != str(
                self._interaction_users.get(interaction_id)
            ):
                self.session.log("🚫", "ephemeral response is only visible to its interaction user", "warn",
                                 kind="action", details={"operation": "interaction.delete_response",
                                                          "status": "denied", "permission": "ephemeral_owner_only"})
            elif message_id is not None:
                self.session.delete_message(message_id, actor_id=self._interaction_users.get(interaction_id))
        elif kind == "interaction_update":
            interaction_id = str(payload.get("interaction_id"))
            message_id = (self._interaction_replies.get(interaction_id)
                          or self._interaction_source_messages.get(interaction_id))
            stored = self.session.messages.get(message_id or "")
            if stored and stored.get("ephemeral") and stored.get("ephemeral_user_id") != str(
                self._interaction_users.get(interaction_id)
            ):
                self.session.log("🚫", "ephemeral response is only visible to its interaction user", "warn",
                                 kind="action", details={"operation": "interaction.update_message",
                                                          "status": "denied", "permission": "ephemeral_owner_only"})
            else:
                self._interaction_ack[interaction_id] = True
                if message_id is None:
                    self.session.log("⚠️", "component update has no response target", "warn", kind="event",
                                     details={"operation": "interaction.update_message", "status": "missing_message"})
                else:
                    changes = {}
                    if "content" in payload:
                        changes["content"] = payload["content"]
                    if "embeds" in payload:
                        changes["embeds"] = self._embeds(payload)
                    if "components" in payload:
                        changes["view"] = _revive_classic_view(payload["components"])
                    self.session.update_message(message_id, **changes)
        elif kind == "interaction_modal":
            interaction_id = str(payload.get("interaction_id"))
            self._interaction_ack[interaction_id] = True
            channel = self.session.channels.get(
                self._interaction_channels.get(interaction_id, ""), self.session.channel
            )
            raw = payload.get("modal") or {}
            if not raw.get("custom_id") or len(raw.get("title") or "") > 45:
                self.session.log("⚠️", "bot sent an invalid modal", "warn", kind="event",
                                 details={"operation": "interaction.send_modal", "status": "invalid_modal"})
                return
            fields = [component for row in raw.get("components", [])
                      for component in row.get("components", [])]
            if not 1 <= len(fields) <= 5 or any(
                not item.get("custom_id") or not item.get("label")
                or len(item.get("label", "")) > 45
                or (item.get("min_length") is not None and item["min_length"] < 0)
                or (item.get("max_length") is not None and item["max_length"] > 4000)
                or (item.get("min_length") is not None and item.get("max_length") is not None
                    and item["min_length"] > item["max_length"])
                for item in fields
            ):
                self.session.log("⚠️", "bot sent an invalid modal form", "warn", kind="event",
                                 details={"operation": "interaction.send_modal", "status": "invalid_modal"})
                return
            source = self._interaction_source_messages.get(interaction_id)
            opened = self.session.open_modal_payload(
                raw.get("title") or "Modal", fields, source,
                channel_id=channel.id, custom_id=raw.get("custom_id"),
                user_id=self._interaction_users.get(interaction_id),
            )
            self._pending_modal = (raw.get("custom_id"), opened.get("source"), opened["id"])
        elif kind == "interaction_edit":
            interaction_id = str(payload.get("interaction_id"))
            self._interaction_ack[interaction_id] = True
            message_id = (self._interaction_replies.get(interaction_id)
                          or self._interaction_source_messages.get(interaction_id))
            stored = self.session.messages.get(message_id or "")
            if stored and stored.get("ephemeral") and stored.get("ephemeral_user_id") != str(
                self._interaction_users.get(interaction_id)
            ):
                self.session.log("🚫", "ephemeral response is only visible to its interaction user", "warn",
                                 kind="action", details={"operation": "interaction.edit_response",
                                                          "status": "denied", "permission": "ephemeral_owner_only"})
            elif message_id is not None:
                changes = {}
                if "content" in payload:
                    changes["content"] = payload["content"]
                if "embeds" in payload:
                    changes["embeds"] = self._embeds(payload)
                if "components" in payload:
                    changes["view"] = _revive_classic_view(payload["components"])
                self.session.update_message(message_id, **changes)
        elif kind == "interaction_complete":
            interaction_id = str(payload.get("interaction_id"))
            self._interaction_ack[interaction_id] = bool(payload.get("acknowledged"))
            if done := self._interaction_done.get(interaction_id):
                done.set()
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
        return {"id": str(me.id), "username": me.name, "global_name": me.display_name,
                "bot": True, "avatar": me.avatar_url, "discriminator": "0"}

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
        ephemeral = bool(payload.get("ephemeral"))
        return self.session.add_message(
            channel_id=payload.get("channel_id") or self.session.channel.id, content=payload.get("content"),
            embeds=self._embeds(payload), view=view, ephemeral=ephemeral,
            ephemeral_user_id=payload.get("ephemeral_user_id") or self.session.active_user.id if ephemeral else None,
        )

    # ------------------------------------------------ dispatch

    _OPTION_NAMES: typing.ClassVar[dict[int, str]] = {
        3: "string", 4: "integer", 5: "boolean", 6: "user", 7: "channel",
        8: "role", 9: "mentionable", 10: "number", 11: "attachment",
    }

    def commands_payload(self) -> dict:
        out = {}
        for command in self._synced_commands:
            params = [{"name": item.get("name"), "description": item.get("description") or "",
                       "required": bool(item.get("required")),
                       "type": self._OPTION_NAMES.get(item.get("type"), "string"),
                       "choices": [[choice.get("name"), choice.get("value")]
                                   for choice in item.get("choices", [])]}
                      for item in command.get("options", []) if item.get("type") in self._OPTION_NAMES]
            out[command.get("name")] = {
                "name": command.get("name"),
                "description": command.get("description") or "",
                "params": params,
            }
        return out

    def _command_payload(self, command: dict, args: dict, channel: pg.MockChannel) -> dict:
        params = {item.get("name"): item for item in command.get("options", [])}
        resolved = {"users": {}, "members": {}, "roles": {}, "channels": {}}
        options = {}
        for name, raw in (args or {}).items():
            if raw in (None, ""):
                continue
            option = params.get(name) or {}
            kind = option.get("type", 3)
            if kind == 4:
                value = int(raw)
            elif kind == 5:
                value = str(raw).lower() in ("1", "true", "yes", "on")
            elif kind == 10:
                value = float(raw)
            else:
                value = str(raw)
            if kind in (6, 9):
                member = self.session.guild.get_member(int(value)) if str(value).isdigit() else None
                if member is not None:
                    resolved["users"][str(member.id)] = self._member_payload_for(member)
                    resolved["members"][str(member.id)] = self._member_payload(member)
            if kind in (7, 9):
                target_channel = self.session.channels.get(str(value))
                if target_channel is not None:
                    resolved["channels"][str(target_channel.id)] = self._channel_payload(target_channel)
            if kind in (8, 9):
                role = self.session.guild.get_role(int(value)) if str(value).isdigit() else None
                if role is not None:
                    resolved["roles"][str(role.id)] = self._role_payload(role)
            options[name] = value
        active = self.session.active_user
        return {"type": "interaction", "interaction_id": "", "command_name": command.get("name"),
                "options": options, "resolved": resolved, "user": self._member_payload_for(active),
                "member": self._member_payload(active),
                "permissions": str(channel.permissions_for(active).value),
                "app_permissions": str(channel.permissions_for(self.session.guild.me).value),
                "channel_id": str(channel.id),
                "channel": self._channel_payload(channel),
                "guild_id": str(GUILD_ID), "guild": {"id": str(GUILD_ID), "name": self.session.guild.name}}

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

    def refresh_guild_state(self) -> None:
        """Refresh discord.js caches after a scenario member or permission change."""
        self._refresh_guild_cache()

    def refresh_member_profile(self, _member: pg.MockMember) -> None:
        self.refresh_guild_state()

    def _refresh_guild_cache(self) -> None:
        if self.session.revision == self._guild_cache_revision:
            return
        self._send({"type": "guild_cache", "guild_id": str(GUILD_ID),
                    "members": [self._member_payload(member) for member in self.session.guild.members],
                    "roles": [self._role_payload(role) for role in self.session.guild.roles],
                    "channels": [self._channel_payload(channel)
                                 for channel in self.session.channels.values()]})
        self._guild_cache_revision = self.session.revision

    def _member_payload_for(self, member: pg.MockMember) -> dict:
        return {"id": str(member.id), "username": member.name,
                "global_name": member.display_name, "bot": member.bot,
                "avatar": member.avatar_url, "avatar_url": member.avatar_url,
                "banner": member.banner_url, "bio": member.bio,
                "accent_color": int(member.accent_color[1:], 16) if member.accent_color else None,
                "status": getattr(member.status, "name", "online"), "discriminator": "0"}

    def _member_payload(self, member: pg.MockMember) -> dict:
        user = self._member_payload_for(member)
        return {"user": user, "nick": member.display_name,
                "roles": [str(role.id) for role in member.roles if role.id != GUILD_ID],
                "joined_at": "2024-01-01T00:00:00+00:00", "deaf": False, "mute": False,
                "pending": False, "permissions": str(member.guild_permissions.value)}

    def _channel_payload(self, channel: pg.MockChannel) -> dict:
        role_ids = {role.id for role in self.session.guild.roles}
        return {"id": str(channel.id), "type": 0, "guild_id": str(GUILD_ID),
                "name": channel.name, "topic": channel.topic,
                "permission_overwrites": [
                {"id": str(target_id), "type": 0 if target_id in role_ids else 1,
                 "allow": str(overwrite.pair()[0].value), "deny": str(overwrite.pair()[1].value)}
                for target_id, overwrite in channel.overwrites.items()
                ]}

    def _role_payload(self, role: pg.MockRole) -> dict:
        return {"id": str(role.id), "name": role.name, "color": role.color.value,
                "permissions": str(role.permissions.value), "position": role.position}

    def _message_event_payload(self, message_id: str, message: dict) -> dict:
        wire_id = _wire_message_id(message_id)
        channel = self.session.channels.get(str(message.get("channel") or ""), self.session.channel)
        author_id = (message.get("author") or {}).get("id")
        author = self.session.guild.get_member(int(author_id)) if str(author_id or "").isdigit() else None
        author = author or self.session.guild.me
        return {"id": wire_id, "message_id": wire_id, "content": message.get("content", ""),
                "channel_id": str(channel.id), "guild_id": str(GUILD_ID),
                "components": message.get("components") or [], "v2": message.get("v2") or [],
                "embeds": message.get("embeds") or [], "author": self._member_payload_for(author),
                "member": self._member_payload(author)}

    async def _settle(self) -> None:
        await asyncio.sleep(0.35)  # give the child a beat to answer

    def _begin_interaction(self, operation: str, icon: str, text: str,
                           details: dict) -> tuple[str, dict]:
        event = self.session.log(icon, text, kind="event",
                                 details={"operation": operation, **details, "status": "dispatched"})
        self._interaction_counter += 1
        interaction_id = str(self._interaction_counter)
        self._interaction_events[interaction_id] = event
        self._interaction_done[interaction_id] = asyncio.Event()
        self._interaction_ack[interaction_id] = False
        return interaction_id, event

    async def _settle_interaction(self, interaction_id: str, event: dict, label: str) -> None:
        done = self._interaction_done[interaction_id]
        try:
            await asyncio.wait_for(done.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass
        if not self._interaction_ack.get(interaction_id):
            self.session.log("⚠️", f"{label} interaction was never acknowledged — real Discord shows 'This interaction failed'",
                             "warn", kind="event", details={"status": "unanswered",
                                                              "operation": "interaction.acknowledgement"})
        self._interaction_events.pop(interaction_id, None)
        self._interaction_done.pop(interaction_id, None)
        self._interaction_ack.pop(interaction_id, None)

    async def dispatch_command(self, name: str, args: dict, channel_id=None) -> None:
        command = next((item for item in self._synced_commands if item.get("name") == name), None)
        if command is None:
            self.session.log("⚠️", f"/{name} is not registered by this bot", "warn", kind="event",
                             details={"operation": "interaction.command", "command": name,
                                      "status": "missing_command"})
            return
        channel = self.session.channels.get(str(channel_id or ""), self.session.channel)
        allowed, reason = channel.permission_check(self.session.active_user, "view_channel")
        if not allowed:
            self.session.log("🚫", f"command blocked: missing view_channel permission ({reason})", "warn",
                             kind="event", details={"operation": "interaction.command", "command": name,
                                                      "status": "denied", "permission": "view_channel",
                                                      "reason": reason})
            return
        self._refresh_guild_cache()
        interaction_id, event = self._begin_interaction("interaction.command", "⌨️",
                                                        f"command invoked: /{name}",
                                                        {"command": name, "arguments": args,
                                                         "actor": self.session.active_user.name,
                                                         "channel": channel.name})
        self._interaction_users[interaction_id] = self.session.active_user.id
        self._interaction_channels[interaction_id] = str(channel.id)
        payload = self._command_payload(command, args, channel)
        payload["interaction_id"] = interaction_id
        self._send(payload)
        await self._settle_interaction(interaction_id, event, f"/{name}")

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
        self._refresh_guild_cache()
        self._begin_interaction("message.send", "💬", f"message received from {session.active_user.name}",
                                {"interaction": "message_create", "message_id": stored["id"],
                                 "channel": channel.name, "actor": session.active_user.name,
                                 "content": content})
        self._send({"type": "message", "message_id": _wire_message_id(stored["id"]),
                    "content": content, "channel_id": str(channel.id), "guild_id": str(GUILD_ID),
                    "author": self._member_payload_for(session.active_user),
                    "member": self._member_payload(session.active_user)})
        await self._settle()

    async def dispatch_click(self, message_id: str, custom_id: str, values: list) -> None:
        message = self.session.messages.get(message_id) or {}
        details = {"operation": "interaction.component", "custom_id": custom_id,
                   "message_id": message_id, "actor": self.session.active_user.name}
        if message.get("deleted"):
            self.session.log("⚠️", "component message is no longer available", "warn", kind="event",
                             details={**details, "status": "invalid_message"})
            return
        if message.get("ephemeral") and message.get("ephemeral_user_id") != str(self.session.active_user.id):
            self.session.log("🚫", "ephemeral message is only visible to its interaction user", "warn",
                             kind="event", details={**details, "status": "denied"})
            return
        channel = self.session.channels.get(str(message.get("channel") or ""), self.session.channel)
        allowed, reason = channel.permission_check(self.session.active_user, "view_channel")
        if not allowed:
            self.session.log("🚫", f"component use blocked: missing view_channel permission ({reason})", "warn",
                             kind="event", details={**details, "status": "denied", "permission": "view_channel",
                                                     "reason": reason})
            return
        component = pg._find_component(message.get("components") or [], message.get("v2") or [], custom_id)
        if component is None or component.get("disabled") or component.get("url"):
            self.session.log("⚠️", f"component {custom_id!r} is not available on this message", "warn",
                             kind="event", details={**details, "status": "invalid_component"})
            return
        self._refresh_guild_cache()
        interaction_id, event = self._begin_interaction("interaction.component", "🖱️",
                                                        f"component used: {custom_id!r}",
                                                        {**details, "channel": channel.name})
        self._interaction_users[interaction_id] = self.session.active_user.id
        self._interaction_channels[interaction_id] = str(channel.id)
        self._interaction_source_messages[interaction_id] = message_id
        item_type = self._component_type(message, custom_id)
        self._send({"type": "component", "interaction_id": interaction_id,
                    "custom_id": custom_id, "component_type": item_type,
                    "values": values or [], "channel_id": str(channel.id),
                    "channel": self._channel_payload(channel),
                    "guild_id": str(GUILD_ID), "guild": {"id": str(GUILD_ID), "name": self.session.guild.name},
                    "user": self._member_payload_for(self.session.active_user),
                    "member": self._member_payload(self.session.active_user),
                    "permissions": str(channel.permissions_for(self.session.active_user).value),
                    "app_permissions": str(channel.permissions_for(self.session.guild.me).value),
                    "message": self._message_event_payload(message_id, message)})
        await self._settle_interaction(interaction_id, event, f"component {custom_id!r}")

    @staticmethod
    def _component_type(message: dict, custom_id: str) -> int:
        item = NodeProjectRuntime._find_component(message, custom_id)
        if item is None:
            return 2
        return {"button": 2, "select": 3, "user_select": 5, "role_select": 6,
                "mentionable_select": 7, "channel_select": 8}.get((item or {}).get("kind"), 2)

    @staticmethod
    def _find_component(message: dict, custom_id: str) -> dict | None:
        for item in message.get("components") or []:
            if item.get("custom_id") == custom_id:
                return item
            for child in item.get("components") or []:
                if child.get("custom_id") == custom_id:
                    return child
        stack = list(message.get("v2") or [])
        while stack:
            item = stack.pop()
            if item.get("custom_id") == custom_id:
                return item
            stack.extend(item.get("children") or [])
            if item.get("accessory"):
                stack.append(item["accessory"])
        return None

    async def dispatch_submit(self, message_id: str | None, custom_id: str, values: dict) -> None:
        modal = next((item for item in self.session.modals
                      if item.get("id") == message_id or item.get("custom_id") == custom_id), None)
        if modal is None:
            self.session.log("⚠️", "modal is no longer open", "warn", kind="event",
                             details={"operation": "interaction.modal_submit", "custom_id": custom_id,
                                      "status": "missing_modal"})
            return
        channel = self.session.channels.get(modal.get("channel_id", ""), self.session.channel)
        self._interaction_counter += 1
        interaction_id = str(self._interaction_counter)
        self._interaction_users[interaction_id] = self.session.active_user.id
        self._interaction_channels[interaction_id] = str(channel.id)
        self.session.modals.remove(modal)
        self.session._touch()
        self.session.log("📝", f"modal submitted: {custom_id!r}", kind="event",
                         details={"operation": "interaction.modal_submit", "custom_id": custom_id,
                                  "modal_id": modal["id"], "status": "dispatched"})
        source = modal.get("source")
        source_message = self.session.messages.get(source or "")
        self._send({"type": "modal_submit", "interaction_id": interaction_id,
                    "custom_id": custom_id, "values": values or {},
                    "source": source,
                    "message": self._message_event_payload(source, source_message)
                    if source and source_message else None,
                    "channel_id": str(channel.id), "channel": self._channel_payload(channel),
                    "guild_id": str(GUILD_ID), "guild": {"id": str(GUILD_ID), "name": self.session.guild.name},
                    "user": self._member_payload_for(self.session.active_user),
                    "member": self._member_payload(self.session.active_user),
                    "permissions": str(channel.permissions_for(self.session.active_user).value),
                    "app_permissions": str(channel.permissions_for(self.session.guild.me).value)})
        await self._settle()

    async def dispatch_pending_modal(self, values: dict, modal_id: str | None = None) -> bool:
        if self._pending_modal is None:
            return False
        custom_id, source, expected_id = self._pending_modal
        if modal_id and modal_id != expected_id:
            return False
        modal = next((item for item in self.session.modals if item.get("id") == expected_id), None)
        if modal is None:
            self._pending_modal = None
            return False
        details = {"operation": "interaction.modal_submit", "modal_id": expected_id,
                   "custom_id": custom_id, "actor": self.session.active_user.name}
        if modal.get("user_id") != str(self.session.active_user.id):
            self.session.log("🚫", "modal is only visible to the user who opened it", "warn", kind="event",
                             details={**details, "status": "denied"})
            return True
        if not isinstance(values, dict) or any(
            (item.get("required") and not pg._modal_value(values, item).strip())
            or len(pg._modal_value(values, item)) < (item.get("min_length") or 0)
            or len(pg._modal_value(values, item)) > (item.get("max_length") or 4000)
            for item in modal.get("items", [])
        ):
            self.session.log("⚠️", "modal submission contains an invalid field", "warn", kind="event",
                             details={**details, "status": "invalid_form"})
            return True
        channel = self.session.channels.get(modal.get("channel_id") or "", self.session.channel)
        self._refresh_guild_cache()
        interaction_id, event = self._begin_interaction("interaction.modal_submit", "📝",
                                                        f"modal submitted: {custom_id!r}",
                                                        {**details, "channel": channel.name})
        self._interaction_users[interaction_id] = self.session.active_user.id
        self._interaction_channels[interaction_id] = str(channel.id)
        self._pending_modal = None
        self.session.modals.remove(modal)
        self.session._touch()
        source_message = self.session.messages.get(source or "")
        self._send({"type": "modal_submit", "interaction_id": interaction_id,
                    "custom_id": custom_id, "values": values or {}, "source": source,
                    "message": self._message_event_payload(source, source_message)
                    if source and source_message else None,
                    "channel_id": str(channel.id), "channel": self._channel_payload(channel),
                    "guild_id": str(GUILD_ID), "guild": {"id": str(GUILD_ID), "name": self.session.guild.name},
                    "user": self._member_payload_for(self.session.active_user),
                    "member": self._member_payload(self.session.active_user),
                    "permissions": str(channel.permissions_for(self.session.active_user).value),
                    "app_permissions": str(channel.permissions_for(self.session.guild.me).value)})
        await self._settle_interaction(interaction_id, event, f"modal {custom_id!r}")
        return True

    def dismiss_modal(self, modal_id: str) -> bool:
        result = self.session.dismiss_modal(modal_id, self.session.active_user.id)
        if self._pending_modal and self._pending_modal[2] == modal_id:
            self._pending_modal = None
        return result

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
