"""ScriptPlayground server: a local web playground for Discord bot UI code.

    python main.py [--port 8741]

Serves static/index.html, keeps sessions in memory (no persistence), and
streams timeline update nudges to the browser over a websocket. Run `python main.py --help`
for options.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import discord
from aiohttp import ClientError, ClientSession, ClientTimeout, web
from aiohttp.web_log import AccessLogger

import bot_runtime
import bot_worker
import bridge
import env_discovery
from playground import (
    Session,
    _member_before_snapshot,
    dispatch_click,
    dispatch_command,
    dispatch_event,
    dispatch_message,
    dispatch_submit,
    reload_script,
    run_scenario,
    run_script,
    validate_scenario,
)
from playground import (
    state as _pg_state,
)
from project_state import validate_project


def state(session: Session) -> dict:
    """Project (worker) sessions render the worker's latest state snapshot."""
    cached = getattr(session, "_project_state", None)
    if cached is not None:
        snapshot = dict(cached)
        snapshot["sid"] = session.sid
        snapshot["revision"] = getattr(session, "_project_seq", 0)
        return snapshot
    return _pg_state(session)

log = logging.getLogger("playground")
_MODULE_DIR = Path(__file__).resolve().parent
STATIC_DIR = _MODULE_DIR / "static"
EMBEDER_DIR = _MODULE_DIR / "embeder"
DATA_DIR = _MODULE_DIR
SCRIPTS_DIR = _MODULE_DIR / "scripts"
WORKSPACES_DIR = _MODULE_DIR / "bots"
_DESIGNS_SUBDIR = "designs"
_SCENARIOS_SUBDIR = "scenarios"
_MAIN_SCRIPT = "demo"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_ -]{1,50}$")

# How long a hosted project may take to boot before the worker is killed.
_PROJECT_BOOT_SECONDS = 90
_OAUTH_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
_OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
_OAUTH_USER_URL = "https://discord.com/api/users/@me"
_AUTH_COOKIE = "scriptplayground_auth"
_OAUTH_STATE_COOKIE = "scriptplayground_oauth_state"
_OAUTH_STATE_TTL = 600
_DESKTOP_CLOSE_GRACE = 30.0
_DESKTOP_POLL_INTERVAL = 0.25
_AUTO_SHUTDOWN = "scriptplayground_auto_shutdown"
_DATA_DIR_KEY = "scriptplayground_data_dir"
_DESKTOP_SHUTDOWN_REQUESTED = "scriptplayground_shutdown_requested"


def _server_already_running(host: str, port: int) -> bool:
    """True when another ScriptPlayground instance already listens on host:port.

    Without this guard, every `python main.py` (or exe launch) with a server
    still up opened yet another browser tab — the "running a new bot opens a
    window" complaint. With it, a relaunch focuses nothing and opens nothing.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


_DESKTOP_CLIENT_CONNECTED = "scriptplayground_client_connected"
_DESKTOP_LAST_CLIENT_DISCONNECT = "scriptplayground_last_client_disconnect"
_DESKTOP_SHUTDOWN_WATCHER = "scriptplayground_shutdown_watcher"


def data_directory() -> Path:
    """Return writable data in dev mode or the OS user-data path when frozen."""
    override = os.getenv("SCRIPTPLAYGROUND_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return _MODULE_DIR
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local/share")
    path = base / "ScriptPlayground"
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_data_directory(path: Path | None = None) -> Path:
    """Point writable libraries at a data root and return that root."""
    global DATA_DIR, SCRIPTS_DIR, WORKSPACES_DIR
    DATA_DIR = (path if path is not None else data_directory()).expanduser().resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DATA_DIR == _MODULE_DIR:
        SCRIPTS_DIR = _MODULE_DIR / "scripts"
        WORKSPACES_DIR = _MODULE_DIR / "bots"
    else:
        SCRIPTS_DIR = DATA_DIR / "scripts"
        WORKSPACES_DIR = DATA_DIR / "bots"
    return DATA_DIR


def _scenarios_dir() -> Path:
    return SCRIPTS_DIR / _SCENARIOS_SUBDIR if DATA_DIR == _MODULE_DIR else DATA_DIR / _SCENARIOS_SUBDIR


def _designs_dir() -> Path:
    return SCRIPTS_DIR / _DESIGNS_SUBDIR if DATA_DIR == _MODULE_DIR else DATA_DIR / _DESIGNS_SUBDIR


def _script_path(name: str) -> Path:
    if not _SAFE_NAME.match(name):
        raise web.HTTPBadRequest(text="invalid script name")
    return SCRIPTS_DIR / f"{name}.py"


def _scenario_path(name: str) -> Path:
    if not _SAFE_NAME.match(name):
        raise web.HTTPBadRequest(text="invalid scenario name")
    return _scenarios_dir() / f"{name}.scenario.json"


def bootstrap_default_scripts() -> None:
    """Seed bundled scripts, scenarios, and designs on a fresh data directory."""
    bundled = _MODULE_DIR / "scripts"
    if DATA_DIR == _MODULE_DIR:
        return
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    _scenarios_dir().mkdir(parents=True, exist_ok=True)
    _designs_dir().mkdir(parents=True, exist_ok=True)
    for source in bundled.glob("*.py"):
        target = SCRIPTS_DIR / source.name
        if not target.exists():
            shutil.copy2(source, target)
    for source_dir, target_dir in (
        (bundled / _SCENARIOS_SUBDIR, _scenarios_dir()),
        (bundled / _DESIGNS_SUBDIR, _designs_dir()),
    ):
        if source_dir.is_dir():
            for source in source_dir.iterdir():
                target = target_dir / source.name
                if source.is_file() and not target.exists():
                    shutil.copy2(source, target)


SESSIONS: dict[str, Session] = {}
RUNTIMES: dict[str, bot_runtime.ProjectRuntime | bot_runtime.NodeProjectRuntime] = {}  # sid -> booted runtime
WS_CLIENTS: dict[str, set[web.WebSocketResponse]] = {}
OAUTH_STATES: dict[str, float] = {}
AUTH_SESSIONS: dict[str, dict[str, str | None]] = {}
# sid -> {"path": Path, "mtime": int} for the workspace file a session last ran
# via /run-file; the mtime poller live-reloads it when the file changes on disk.
WORKSPACE_WATCHERS: dict[str, dict] = {}
_WORKSPACE_WATCH_INTERVAL = 0.5
_WORKSPACE_WATCH_TASK: asyncio.Task | None = None


def _session_project_lock(session: Session) -> asyncio.Lock:
    lock = getattr(session, "_project_operation_lock", None)
    if lock is None:
        lock = session._project_operation_lock = asyncio.Lock()
    return lock


def _oauth_config() -> dict[str, str] | None:
    values = {name: os.getenv(name, "").strip() for name in (
        "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"
    )}
    return values if all(values.values()) else None


def _auth_user(request: web.Request) -> dict[str, str | None] | None:
    return AUTH_SESSIONS.get(request.cookies.get(_AUTH_COOKIE))


async def _oauth_request(method: str, url: str, **kwargs) -> tuple[int, dict]:
    async with ClientSession(timeout=ClientTimeout(total=10)) as client, client.request(method, url, **kwargs) as response:
        try:
            payload = await response.json(content_type=None)
        except (TypeError, ValueError):
            payload = {}
        return response.status, payload if isinstance(payload, dict) else {}


def _oauth_state() -> str:
    now = time.monotonic()
    for value, created in list(OAUTH_STATES.items()):
        if now - created > _OAUTH_STATE_TTL:
            OAUTH_STATES.pop(value, None)
    value = secrets.token_urlsafe(32)
    OAUTH_STATES[value] = now
    return value


def _auth_error(message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


class _AccessLogger(AccessLogger):
    """Keep OAuth callback credentials out of request-target access logs."""

    def __init__(self, logger, log_format=AccessLogger.LOG_FORMAT):
        super().__init__(logger, log_format)
        self._methods = [
            (key, self._format_r if key == "first_request_line" else method)
            for key, method in self._methods
        ]

    @staticmethod
    def _format_r(request, response, request_time):
        if request is not None and request.path == "/auth/discord/callback":
            return f"{request.method} {request.path} HTTP/{request.version.major}.{request.version.minor}"
        return AccessLogger._format_r(request, response, request_time)


def _get_session(request: web.Request) -> Session:
    sid = request.match_info["sid"]
    if sid not in SESSIONS:
        raise web.HTTPNotFound(text="unknown session")
    return SESSIONS[sid]


async def _sse_broadcast(sid: str, payload: dict) -> None:
    dead = []
    for ws in tuple(WS_CLIENTS.get(sid, ())):
        try:
            await ws.send_str(json.dumps(payload))
        except (ConnectionResetError, RuntimeError, web.WebSocketError):
            dead.append(ws)
    clients = WS_CLIENTS.get(sid, set())
    for ws in dead:
        clients.discard(ws)


def _bump(sid: str) -> None:
    """Nudge every open browser tab for this session (schedule on our loop)."""
    loop = getattr(_bump, "_loop", None)
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(_sse_broadcast(sid, {"type": "update"}), loop)


_PENDING_EVENT_TASKS: set[asyncio.Task] = set()


def _notify_event(session: Session, kind: str, payload: dict) -> None:
    """Fire-and-notify: dispatch a gateway event without delaying the response.

    Management mutations (join/leave/roles/channels) return instantly; the
    handler's effects reach the browser through the normal _bump/WS channel.
    User code still runs only on the session runner via runner.gate. Tasks are
    pinned in a module set so they are not garbage-collected mid-await.
    """

    async def _run() -> None:
        await dispatch_event(session, kind, payload)
        _bump(session.sid)

    task = asyncio.create_task(_run())
    _PENDING_EVENT_TASKS.add(task)
    task.add_done_callback(_PENDING_EVENT_TASKS.discard)


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def create_session(_request: web.Request) -> web.Response:
    sid = secrets.token_hex(8)
    SESSIONS[sid] = Session(sid)
    WS_CLIENTS.setdefault(sid, set())
    main_script = SCRIPTS_DIR / f"{_MAIN_SCRIPT}.py"
    example = main_script.read_text(encoding="utf-8") if main_script.exists() else ""
    return web.json_response({"sid": sid, "example": example, "state": state(SESSIONS[sid])})


async def auth_status(request: web.Request) -> web.Response:
    user = _auth_user(request)
    return web.json_response({
        "configured": _oauth_config() is not None,
        "authenticated": user is not None,
        "user": user,
    })


async def discord_login(request: web.Request) -> web.Response:
    config = _oauth_config()
    if config is None:
        return _auth_error("Discord sign-in is not configured; continue in offline mode.", 503)
    state_value = _oauth_state()
    query = urlencode({
        "client_id": config["DISCORD_CLIENT_ID"],
        "redirect_uri": config["DISCORD_REDIRECT_URI"],
        "response_type": "code",
        "scope": "identify",
        "state": state_value,
    })
    response = web.HTTPFound(f"{_OAUTH_AUTHORIZE_URL}?{query}")
    response.set_cookie(
        _OAUTH_STATE_COOKIE, state_value, path="/", httponly=True,
        samesite="Lax", secure=request.secure, max_age=_OAUTH_STATE_TTL,
    )
    return response


async def discord_callback(request: web.Request) -> web.Response:
    config = _oauth_config()
    if config is None:
        return _auth_error("Discord sign-in is not configured; continue in offline mode.", 503)
    state_value = request.query.get("state", "")
    created = OAUTH_STATES.pop(state_value, None)
    cookie_state = request.cookies.get(_OAUTH_STATE_COOKIE, "")
    if (
        created is None
        or time.monotonic() - created > _OAUTH_STATE_TTL
        or not hmac.compare_digest(state_value.encode("utf-8"), cookie_state.encode("utf-8"))
    ):
        return _auth_error("Discord sign-in could not be verified. Please try again.", 400)
    if request.query.get("error"):
        return _auth_error("Discord authorization was not completed.", 400)
    code = request.query.get("code", "")
    if not code:
        return _auth_error("Discord did not return an authorization code.", 400)
    try:
        token_status, token = await _oauth_request(
            "POST",
            _OAUTH_TOKEN_URL,
            data={
                "client_id": config["DISCORD_CLIENT_ID"],
                "client_secret": config["DISCORD_CLIENT_SECRET"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config["DISCORD_REDIRECT_URI"],
            },
            headers={"Accept": "application/json"},
        )
        access_token = token.get("access_token")
        if token_status != 200 or not isinstance(access_token, str) or not access_token:
            return _auth_error("Discord sign-in could not exchange the authorization code.", 502)
        user_status, user = await _oauth_request(
            "GET", _OAUTH_USER_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    except (ClientError, OSError, asyncio.TimeoutError):
        return _auth_error("Discord sign-in is temporarily unavailable.", 502)
    if user_status != 200 or not user.get("id") or not user.get("username"):
        return _auth_error("Discord sign-in could not retrieve your identity.", 502)
    identity = {
        "id": str(user["id"]),
        "username": str(user["username"]),
        "global_name": user.get("global_name"),
        "avatar": user.get("avatar"),
    }
    auth_id = secrets.token_urlsafe(32)
    AUTH_SESSIONS[auth_id] = identity
    response = web.HTTPFound("/")
    response.set_cookie(
        _AUTH_COOKIE, auth_id, path="/", httponly=True, samesite="Lax",
        secure=request.secure,
    )
    response.del_cookie(_OAUTH_STATE_COOKIE, path="/")
    return response


async def logout(request: web.Request) -> web.Response:
    auth_id = request.cookies.get(_AUTH_COOKIE)
    if auth_id:
        AUTH_SESSIONS.pop(auth_id, None)
    response = web.HTTPFound("/")
    response.del_cookie(_AUTH_COOKIE, path="/")
    return response


_HIDDEN_SCRIPTS = {"vendor_embeder"}


def _workspace_dir(workspace: str) -> Path:
    if not _SAFE_NAME.fullmatch(workspace):
        raise web.HTTPBadRequest(text="invalid workspace")
    workspaces_root = WORKSPACES_DIR.resolve()
    root = (workspaces_root / workspace).resolve()
    if not root.is_relative_to(workspaces_root):
        raise web.HTTPBadRequest(text="invalid workspace")
    if not root.is_dir():
        raise web.HTTPNotFound(text="workspace not found")
    return root


def _workspace_file(workspace: str, filename: str = "bot.py") -> Path:
    relative = Path(filename)
    parts = filename.split("/")
    if (not filename or "\\" in filename or ":" in filename or relative.is_absolute()
            or relative.suffix != ".py" or any(part in {"", ".", ".."} for part in parts)):
        raise web.HTTPBadRequest(text="invalid workspace file")
    root = _workspace_dir(workspace)
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise web.HTTPBadRequest(text="invalid workspace file")
    return path


def _workspace_python_files(folder: Path) -> list[str]:
    root = folder.resolve()
    skipped = {"__pycache__", ".git", "node_modules", ".venv", "venv", "browser-profile", "backups"}
    files = []
    for path in folder.rglob("*.py"):
        relative = path.relative_to(folder)
        if (skipped.intersection(relative.parts) or any(part.startswith(".") for part in relative.parts)
                or not path.is_file() or not path.resolve().is_relative_to(root)):
            continue
        files.append(relative.as_posix())
    return sorted(files)


def _workspace_entry(folder: Path) -> Path | None:
    """The workspace's entry module: bot.py when it exists, else None.

    A folder without bot.py keeps its current behavior (hosted project boot
    from Run; run-file stays available for main.py-style entries).
    """
    entry = folder / "bot.py"
    return entry if entry.is_file() else None


class _ProjectBootDeadline(Exception):
    """The server's boot deadline expired (distinct from a script TimeoutError)."""


class _ProjectShutdownPending(Exception):
    """A prior in-process project ignored cancellation and still owns the session."""


_BOT_MODE_PATTERNS = (
    re.compile(r"\b(?:commands\.Bot|discord\.Client)\s*\("),
    re.compile(r"\bclass\s+\w+\s*\(\s*(?:commands\.Bot|discord\.Client)\s*\)"),
    re.compile(r"\b(?:bot|client)\.run\s*\("),
)


def _looks_like_discord_bot(source: str) -> bool:
    """Discord Bot Mode: real discord.py construction/run, not playground helpers."""
    if "discord" not in source:
        return False
    return any(pattern.search(source) for pattern in _BOT_MODE_PATTERNS)


async def _boot_editor_bot(session: Session, code: str) -> dict:
    """Run editor buffer bot code in an ephemeral worker sandbox (Bot Mode)."""
    await _shutdown_runtime(session)
    session.restart()
    staging = Path(bot_runtime._SANDBOX_ROOT) / f"editor-{session.sid}-{secrets.token_hex(4)}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "bot.py").write_text(code, encoding="utf-8")
    try:
        return await _boot_workspace_project(session, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def _shutdown_runtime(session: Session) -> None:
    old = RUNTIMES.pop(session.sid, None)
    if old is not None:
        await old.shutdown()
    session._project_state = None
    if bot_runtime.project_shutdown_pending(session):
        raise _ProjectShutdownPending("previous hosted project is still shutting down")


async def _boot_workspace_project(session: Session, folder: Path) -> dict:
    """Run the saved workspace entry point, never the editor buffer."""
    await _shutdown_runtime(session)
    session.restart()
    boot = asyncio.create_task(
        (bot_runtime.run_project if bot_runtime._is_node_project(folder)
         else bot_worker.run_worker_project)(session, folder,
                                             on_exception=lambda: _bump(session.sid))
    )

    async def cancel_boot() -> None:
        boot.cancel()
        done, _ = await asyncio.wait({boot}, timeout=0.75)
        if done:
            try:
                runtime = boot.result()
            except BaseException:  # noqa: BLE001 - cancellation/failure is already reported
                return
            await runtime.shutdown()

    deadline = asyncio.get_running_loop().time() + _PROJECT_BOOT_SECONDS
    try:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await cancel_boot()
                raise _ProjectBootDeadline
            done, _ = await asyncio.wait({boot}, timeout=remaining)
            if not done and not boot.done():
                await cancel_boot()
                raise _ProjectBootDeadline
            runtime = boot.result()
            break
    except asyncio.CancelledError:
        await cancel_boot()
        raise
    RUNTIMES[session.sid] = runtime
    _bump(session.sid)
    return {"ok": True, "mode": "project", "status": runtime.status(), "last_run": session.last_run}


async def workspace_environment(request: web.Request) -> web.Response:
    """Configuration discovery for a workspace's dotenv files (values redacted)."""
    folder = _workspace_dir(request.match_info["workspace"])
    return web.json_response(env_discovery.discover(folder))


async def list_workspaces(_request: web.Request) -> web.Response:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    root = WORKSPACES_DIR.resolve()
    workspaces = []
    for folder in sorted(WORKSPACES_DIR.iterdir()):
        if (not _SAFE_NAME.fullmatch(folder.name) or not folder.is_dir()
                or folder.name.startswith(".") or not folder.resolve().is_relative_to(root)):
            continue
        workspaces.append({"name": folder.name, "files": _workspace_python_files(folder)})
    return web.json_response({"workspaces": workspaces})


async def get_workspace_file(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    path = _workspace_file(request.match_info["workspace"], filename)
    if not path.is_file():
        raise web.HTTPNotFound(text="workspace file not found")
    return web.json_response({"name": filename, "code": path.read_text(encoding="utf-8")})


async def save_workspace_file(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    path = _workspace_file(request.match_info["workspace"], filename)
    body = await request.json()
    code = body.get("code") or ""
    if not code.strip():
        return web.json_response({"ok": False, "error": "Refusing to save empty code."}, status=400)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return web.json_response({"ok": True, "name": filename})


async def list_scripts(_request: web.Request) -> web.Response:
    files = sorted(f for f in SCRIPTS_DIR.glob("*.py") if f.stem not in _HIDDEN_SCRIPTS) \
        if SCRIPTS_DIR.exists() else []
    scripts = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        description = first.strip('"\'')[:100] if first.startswith('"') else ""
        scripts.append({"name": f.stem, "description": description, "main": f.stem == _MAIN_SCRIPT})
    return web.json_response({"scripts": scripts})


async def get_script(request: web.Request) -> web.Response:
    path = _script_path(request.match_info["name"])
    if not path.exists():
        raise web.HTTPNotFound(text="no such script")
    return web.json_response({"name": path.stem, "code": path.read_text(encoding="utf-8")})


async def test_scripts(_request: web.Request) -> web.Response:
    files = sorted(f for f in SCRIPTS_DIR.glob("*.py") if f.stem not in _HIDDEN_SCRIPTS) \
        if SCRIPTS_DIR.exists() else []
    async def run_file(path: Path) -> dict:
        session = Session(path.stem)
        try:
            result = await run_script(session, path.read_text(encoding="utf-8"), script_file=path)
            return {"name": path.stem, "ok": result["ok"], "ms": result["ms"],
                    "messages": len(session.order),
                    "error": next((event["text"] for event in reversed(session.events)
                                   if event.get("cls") == "error"), None)}
        finally:
            session.close()
    return web.json_response({"reports": await asyncio.gather(*(run_file(path) for path in files))})


async def save_script(request: web.Request) -> web.Response:
    body = await request.json()
    name = (body.get("name") or "").strip()
    code = body.get("code") or ""
    if not _SAFE_NAME.match(name):
        return web.json_response(
            {"ok": False, "error": "Name: letters, numbers, spaces, dashes, underscores (max 50)."},
            status=400,
        )
    if not code.strip():
        return web.json_response({"ok": False, "error": "Refusing to save an empty script."}, status=400)
    path = SCRIPTS_DIR / f"{name}.py"
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return web.json_response({"ok": True, "name": name, "existed": existed})


async def delete_script(request: web.Request) -> web.Response:
    path = _script_path(request.match_info["name"])
    if path.exists():
        path.unlink()
    return web.json_response({"ok": True})


async def list_scenarios(_request: web.Request) -> web.Response:
    directory = _scenarios_dir()
    directory.mkdir(parents=True, exist_ok=True)
    scenarios = []
    for path in sorted(directory.glob("*.scenario.json")):
        try:
            scenario = validate_scenario(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        scenarios.append({"name": scenario["name"], "steps": len(scenario["steps"])})
    return web.json_response({"scenarios": scenarios})


async def get_scenario(request: web.Request) -> web.Response:
    path = _scenario_path(request.match_info["name"])
    if not path.is_file():
        raise web.HTTPNotFound(text="no such scenario")
    try:
        scenario = validate_scenario(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise web.HTTPBadRequest(text=f"invalid scenario: {error}") from error
    return web.json_response(scenario)


async def save_scenario(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        scenario = validate_scenario(body.get("scenario", body))
    except (TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    path = _scenario_path(scenario["name"])
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
    return web.json_response({"ok": True, "name": scenario["name"], "existed": existed})


async def run_scenario_route(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    try:
        result = await run_scenario(session, body.get("scenario", body), runtime=_runtime(session))
    except (TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _bump(session.sid)
    return web.json_response({**result, "state": state(session)})


def _design_path(name: str) -> Path:
    if not _SAFE_NAME.match(name):
        raise web.HTTPBadRequest(text="invalid design name")
    return _designs_dir() / f"{name}.discordv2proj.json"


async def embeder_page(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(EMBEDER_DIR / "index.html")


async def embeder_info(_request: web.Request) -> web.Response:
    marker = EMBEDER_DIR / "VENDORED_FROM.txt"
    if not marker.exists():
        return web.json_response({"ok": False, "error": "vendor provenance is unavailable"}, status=404)
    return web.json_response({"ok": True, "provenance": marker.read_text(encoding="utf-8")})


async def bridge_design_to_code(request: web.Request) -> web.Response:
    body = await request.json()
    try:
        code = bridge.design_to_code(body.get("design"))
    except (TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    name = (body.get("save") or "").strip()
    if name:
        if not _SAFE_NAME.match(name):
            return web.json_response({"ok": False, "error": "invalid save name"}, status=400)
        _design_path(name).write_text(json.dumps(body["design"], indent=2), encoding="utf-8")
    return web.json_response({"ok": True, "code": code})


async def list_designs(_request: web.Request) -> web.Response:
    designs = []
    for path in sorted(_designs_dir().glob("*.discordv2proj.json")):
        try:
            data = validate_project(json.loads(path.read_text(encoding="utf-8")))
            meta = data.get("metadata") or {}
            name = path.name.removesuffix(".discordv2proj.json")
            designs.append({"name": name, "project": meta.get("name") or name,
                            "updatedAt": meta.get("updatedAt") or ""})
        except (OSError, TypeError, ValueError):
            continue
    return web.json_response({"designs": designs})


async def get_design(request: web.Request) -> web.Response:
    path = _design_path(request.match_info["name"])
    if not path.is_file():
        raise web.HTTPNotFound(text="no such design")
    try:
        design = validate_project(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError) as error:
        raise web.HTTPBadRequest(text=f"invalid project: {error}") from error
    return web.json_response(design)


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(state(_get_session(request)))


async def set_user(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(request, "set_user", args=[int(body.get("user_id"))])
    if early is not None:
        return early
    try:
        session.set_user(int(body.get("user_id")))
    except (AttributeError, TypeError, ValueError):
        return web.json_response({"ok": False, "error": "unknown simulated user"}, status=400)
    _bump(session.sid)
    return web.json_response(state(session))


async def add_member(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(
        request, "add_member", args=[body.get("username")],
        kwargs={key: body[key] for key in
                ("display_name", "bio", "avatar_url", "banner_url",
                 "accent_color", "status") if key in body},
        event="member_join")
    if early is not None:
        return early
    try:
        profile = {key: body[key] for key in ("display_name", "bio", "avatar_url", "banner_url",
                                               "accent_color", "status") if key in body}
        member = session.add_member(body.get("username"), profile)
        session.set_user(member.id)
    except (AttributeError, TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _notify_event(session, "member_join", {"user_id": member.id})
    _bump(session.sid)
    return web.json_response(state(session))


async def update_member_profile(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    target_id = int(request.match_info["user_id"])
    early = await _project_session_op(
        request, "update_member_profile", args=[target_id, body],
        kwargs={"event_user_id": target_id}, event="member_update")
    if early is not None:
        return early
    try:
        user_id = int(request.match_info["user_id"])
        before = _member_before_snapshot(session.guild.get_member(user_id)
                                         or session.guild.me)
        session.update_member_profile(user_id, body)
    except (AttributeError, TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    if before.display_name != session.guild.get_member(user_id).display_name:
        _notify_event(session, "member_update", {"user_id": user_id, "_before": before})
    _bump(session.sid)
    return web.json_response(state(session))


async def run_code(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    async with _session_project_lock(session):
        return await _run_code(session, body)


async def _run_code(session: Session, body: dict) -> web.Response:
    if bot_runtime.project_shutdown_pending(session):
        return web.json_response({"ok": False, "error": "previous hosted project is still shutting down"},
                                 status=503)
    workspace = (body.get("workspace") or "").strip()
    if workspace:
        # Run always boots the saved workspace entry point; editor text is intentionally ignored.
        try:
            folder = _workspace_dir(workspace)
        except (web.HTTPBadRequest, web.HTTPNotFound):
            return web.json_response({"ok": False, "error": f"unknown workspace {workspace!r}"}, status=404)
        entry = _workspace_entry(folder)
        if entry is not None and not _looks_like_discord_bot(
                entry.read_text(encoding="utf-8", errors="replace")):
            # Script Mode: playground-helper bot.py workspaces keep the mock
            # runtime; real discord.py bots fall through to the hosted boot.
            # A bot.py workspace runs through the playground runtime (imports,
            # cogs, cog slash commands); folders with a different entry
            # (main.py) keep the hosted-project boot below.
            _disarm_workspace_watch(session)  # editor-driven Run owns the runtime again
            code = body.get("code") or ""
            result = await run_script(session, code, workspace=workspace, workspace_root=folder,
                                      workspace_file=None)
            _bump(session.sid)
            payload = {**result, "mode": "workspace", "commands": session.commands}
            if not result.get("ok"):
                # Same failure contract as the two boot paths below: the caller
                # gets the exception summary and the diagnostics, not a bare
                # 200 with ok=false. Success keeps its 200 and full result.
                exception = (session.last_run or {}).get("exception") or {}
                summary = (f"{exception.get('type')}: {exception.get('message')}"
                           if exception.get("type") else str(result.get("error") or "run failed"))
                payload["error"] = summary
                payload["last_run"] = session.last_run or None
                return web.json_response(payload, status=500)
            return web.json_response(payload)
        try:
            return web.json_response(await _boot_workspace_project(session, folder))
        except _ProjectBootDeadline:
            _bump(session.sid)
            return web.json_response(
                {"ok": False, "error": f"project boot exceeded {_PROJECT_BOOT_SECONDS:g}s"},
                status=504)
        except _ProjectShutdownPending as error:
            return web.json_response({"ok": False, "error": str(error)}, status=503)
        except Exception as error:  # noqa: BLE001 - details were logged to the timeline
            last_run = session.last_run or {}
            exception = last_run.get("exception") or {}
            # worker boots report the ORIGINAL exception type (TimeoutError, SyntaxError, ...)
            summary = (f"{exception.get("type")}: {exception.get("message")}"
                       if exception.get("type") else f"{type(error).__name__}: {error}")
            return web.json_response({
                "ok": False, "error": summary, "last_run": last_run or None,
            }, status=500)
        finally:
            _disarm_workspace_watch(session)  # hosted projects dispatch through the runtime
    code = body.get("code") or ""
    if not code.strip():
        return web.json_response({"ok": False, "error": "Nothing to run — the editor is empty."}, status=400)
    if _looks_like_discord_bot(code):
        # Discord Bot Mode: run the editor buffer as a real bot in a worker.
        _disarm_workspace_watch(session)
        try:
            return web.json_response(await _boot_editor_bot(session, code))
        except _ProjectBootDeadline:
            _bump(session.sid)
            return web.json_response(
                {"ok": False, "error": f"project boot exceeded {_PROJECT_BOOT_SECONDS:g}s"},
                status=504)
        except _ProjectShutdownPending as error:
            return web.json_response({"ok": False, "error": str(error)}, status=503)
        except Exception as error:  # noqa: BLE001 - details were logged to the timeline
            last_run = session.last_run or {}
            exception = last_run.get("exception") or {}
            # worker boots report the ORIGINAL exception type (TimeoutError, SyntaxError, ...)
            summary = (f"{exception.get("type")}: {exception.get("message")}"
                       if exception.get("type") else f"{type(error).__name__}: {error}")
            return web.json_response({
                "ok": False, "error": summary, "last_run": last_run or None,
            }, status=500)
    _disarm_workspace_watch(session)  # editor-driven Run owns the runtime again
    name = (body.get("name") or "").strip()
    script_file = _script_path(name) if name and _script_path(name).is_file() else None
    result = await run_script(session, code, script_file=script_file)
    _bump(session.sid)
    return web.json_response(result)


async def run_workspace_file(request: web.Request) -> web.Response:
    """Run the active editor buffer without saving or booting the whole project."""
    session = _get_session(request)
    body = await request.json()
    async with _session_project_lock(session):
        return await _run_workspace_file(session, body)


async def _run_workspace_file(session: Session, body: dict) -> web.Response:
    if bot_runtime.project_shutdown_pending(session):
        return web.json_response({"ok": False, "error": "previous hosted project is still shutting down"},
                                 status=503)
    workspace = str(body.get("workspace") or "").strip()
    filename = str(body.get("filename") or "")
    code = body.get("code") or ""
    try:
        _workspace_file(workspace, filename)
        if not code.strip():
            return web.json_response({"ok": False, "error": "Nothing to run — the active file is empty."}, status=400)
        if _looks_like_discord_bot(code):
            return web.json_response(
                {"ok": False, "error": "This file is a real discord.py bot — press Run to boot it in Discord Bot Mode."},
                status=409)
        await _shutdown_runtime(session)
        session.restart()
        folder = _workspace_dir(workspace)
        result = await run_script(session, code, workspace=workspace, workspace_root=folder,
                                  workspace_file=filename)
        _arm_workspace_watch(session, _workspace_file(workspace, filename))
        _bump(session.sid)
        result = {**result, "mode": "file", "file": filename}
        return web.json_response(result)
    except web.HTTPBadRequest as error:
        return web.json_response({"ok": False, "error": error.text}, status=400)
    except web.HTTPNotFound as error:
        return web.json_response({"ok": False, "error": error.text}, status=404)
    except _ProjectShutdownPending as error:
        return web.json_response({"ok": False, "error": str(error)}, status=503)
    except Exception as error:  # noqa: BLE001 - script failures remain visible in session state
        return web.json_response({"ok": False, "error": f"{type(error).__name__}: {error}"}, status=500)


async def _watch_workspace_files(_app: web.Application) -> None:
    """Poll armed workspace files; reload the session when one changes on disk."""
    while True:
        await asyncio.sleep(_WORKSPACE_WATCH_INTERVAL)
        for sid, watch in list(WORKSPACE_WATCHERS.items()):
            session = SESSIONS.get(sid)
            if session is None:
                WORKSPACE_WATCHERS.pop(sid, None)
                continue
            path: Path = watch["path"]
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue  # file briefly missing (editor mid-save); keep watching
            if mtime == watch["mtime"]:
                continue
            watch["mtime"] = mtime
            try:
                code = path.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                await reload_script(session, code)
                _bump(sid)
            except Exception:  # the poller must survive bad edits
                log.exception("workspace auto-reload failed for %s", sid)


def _arm_workspace_watch(session: Session, path: Path) -> None:
    """Watch exactly this one file (no recursive folder watching)."""
    try:
        WORKSPACE_WATCHERS[session.sid] = {"path": path, "mtime": path.stat().st_mtime_ns}
    except OSError:
        WORKSPACE_WATCHERS.pop(session.sid, None)


def _disarm_workspace_watch(session: Session) -> None:
    WORKSPACE_WATCHERS.pop(session.sid, None)


async def reload_code(request: web.Request) -> web.Response:
    """Live-reload the editor buffer into the session without clearing the timeline."""
    session = _get_session(request)
    if _runtime(session) is not None:
        return web.json_response(
            {"ok": False, "error": "This session is running a hosted project — press Run to boot the saved files."},
            status=409)
    body = await request.json()
    code = body.get("code") or ""
    if not code.strip():
        return web.json_response({"ok": False, "error": "Nothing to reload — the editor is empty."}, status=400)
    if _looks_like_discord_bot(code):
        return web.json_response(
            {"ok": False, "error": "This script is a real discord.py bot — press Run to boot it in Discord Bot Mode."},
            status=409)
    result = await reload_script(session, code, run_main=bool(body.get("run_main")))
    _bump(session.sid)
    return web.json_response({**result, "state": state(session)})


def _runtime(session: Session) -> bot_runtime.ProjectRuntime | bot_runtime.NodeProjectRuntime | None:
    return RUNTIMES.get(session.sid)


async def _project_session_op(request: web.Request, method: str,
                              args: list | None = None,
                              kwargs: dict | None = None,
                              event: str | None = None) -> web.Response | None:
    """Forward a UI-side Session mutation into the bot worker; None = no worker.

    `event` names the gateway event the mutation implies ("reaction",
    "member_join", ...). The worker applies the mutation and then feeds the
    matching fake gateway payload to the real bot, in that order.
    """
    session = _get_session(request)
    runtime = _runtime(session)
    if runtime is None:
        return None
    try:
        await runtime.session_op(method, args, kwargs, event)
    except bot_worker.WorkerTimeout:
        return web.json_response(
            {"ok": False, "error": "bot stopped responding — worker terminated"}, status=504)
    except Exception as error:  # noqa: BLE001 - mirrors handler-specific errors
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def react(request: web.Request) -> web.Response:
    """Toggle the active simulated user's reaction on a timeline message."""
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(
        request, "toggle_reaction",
        args=[str(body.get("message_id") or ""), str(body.get("emoji") or "")],
        kwargs={"user_id": session.user_id, "actor": "user"}, event="reaction")
    if early is not None:
        return early
    try:
        added = session.toggle_reaction(str(body.get("message_id") or ""),
                                        str(body.get("emoji") or ""),
                                        user_id=session.user_id, actor="user")
    except KeyError:
        return web.json_response({"ok": False, "error": "message not found"}, status=404)
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except discord.Forbidden as error:
        return web.json_response({"ok": False, "error": str(error)}, status=403)
    # Reactions are interaction-class: await the handler so the response is
    # deterministic, exactly like the click/command routes. Both the raw and
    # the constructed (Reaction, member) events fire, in Discord's order:
    # raw first, then on_reaction_add/remove (component-style handlers, in
    # registration order).
    reaction_payload = {"message_id": str(body.get("message_id") or ""),
                        "user_id": session.user_id, "emoji": str(body.get("emoji") or "")}
    await dispatch_event(session, "raw_reaction_add" if added else "raw_reaction_remove",
                         reaction_payload)
    await dispatch_event(session, "reaction_add" if added else "reaction_remove",
                         reaction_payload)
    _bump(session.sid)
    return web.json_response({"ok": True, "added": added, "state": state(session)})


async def voice(request: web.Request) -> web.Response:
    """Simulated voice state change (join/leave/mute/deafen) for the active user."""
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(
        request, "voice_action",
        kwargs={"action": str(body.get("action") or ""), "channel_id": body.get("channel_id")},
        event="voice_state")
    if early is not None:
        return early
    try:
        result = session.voice_action(str(body.get("action") or ""),
                                      channel_id=body.get("channel_id"))
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _bump(session.sid)
    return web.json_response({"ok": True, **result, "state": state(session)})


async def upload(request: web.Request) -> web.Response:
    """Register a browser-side file upload (data URI; nothing leaves the machine)."""
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(
        request, "add_upload",
        args=[str(body.get("name") or "file"), int(body.get("size") or 0),
              str(body.get("content_type") or ""), body.get("data_uri")])
    if early is not None:
        return early
    entry = session.add_upload(str(body.get("name") or "file"),
                               int(body.get("size") or 0),
                               str(body.get("content_type") or ""),
                               body.get("data_uri"))
    _bump(session.sid)
    return web.json_response({"ok": True, "upload": {k: v for k, v in entry.items() if k != "data_uri"},
                              "state": state(session)})


async def delete_upload(request: web.Request) -> web.Response:
    session = _get_session(request)
    early = await _project_session_op(request, "delete_upload",
                                      args=[int(request.match_info["index"])])
    if early is not None:
        return early
    try:
        session.delete_upload(int(request.match_info["index"]))
    except (KeyError, ValueError):
        return web.json_response({"ok": False, "error": "upload not found"}, status=404)
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def create_channel(request: web.Request) -> web.Response:
    """User-driven channel creation (same model path bots use)."""
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(
        request, "create_text_channel_ui",
        args=[str(body.get("name") or ""), str(body.get("topic") or "")],
        event="guild_channel_create")
    if early is not None:
        return early
    try:
        channel = session.create_text_channel_ui(str(body.get("name") or ""),
                                                  str(body.get("topic") or ""))
    except discord.Forbidden as error:
        return web.json_response({"ok": False, "error": str(error)}, status=403)
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _notify_event(session, "guild_channel_create", {"channel_id": channel.id})
    _bump(session.sid)
    return web.json_response({"ok": True, "channel": {"id": str(channel.id), "name": channel.name},
                              "state": state(session)})


async def _thread_body(request: web.Request) -> dict:
    """JSON body, tolerating an empty POST - `request.json()` raises on one."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty body is a valid "no options" POST
        return {}
    return body if isinstance(body, dict) else {}


async def create_thread(request: web.Request) -> web.Response:
    """A simulated user opens a thread (the action that emits THREAD_CREATE)."""
    session = _get_session(request)
    body = await _thread_body(request)
    channel_id = str(body.get("channel_id") or session.channel.id)
    early = await _project_session_op(
        request, "make_thread",
        args=[channel_id, str(body.get("name") or "thread")],
        event="thread_create")
    if early is not None:
        return early
    try:
        thread = session.make_thread(int(channel_id), str(body.get("name") or "thread"))
    except KeyError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=404)
    except discord.Forbidden as error:
        return web.json_response({"ok": False, "error": str(error)}, status=403)
    _notify_event(session, "thread_create", {"thread_id": thread.id})
    _bump(session.sid)
    return web.json_response({"ok": True,
                              "thread": {"id": str(thread.id), "name": thread.name,
                                         "parent_id": str(thread.parent_id)},
                              "state": state(session)})


async def archive_thread(request: web.Request) -> web.Response:
    """Archiving a thread emits THREAD_UPDATE, which discord.py turns into
    on_thread_update with a real before/after pair."""
    session = _get_session(request)
    thread_id = request.match_info["thread_id"]
    body = await _thread_body(request)
    archived = bool(body.get("archived", True))
    early = await _project_session_op(
        request, "set_thread_archived",
        args=[thread_id, archived], event="thread_update")
    if early is not None:
        return early
    try:
        session.set_thread_archived(thread_id, archived)
    except KeyError:
        return web.json_response({"ok": False, "error": "thread not found"}, status=404)
    _notify_event(session, "thread_update", {"thread_id": thread_id})
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def delete_thread(request: web.Request) -> web.Response:
    """Deleting a thread emits THREAD_DELETE."""
    session = _get_session(request)
    thread_id = request.match_info["thread_id"]
    early = await _project_session_op(
        request, "delete_thread", args=[thread_id], event="thread_delete")
    if early is not None:
        return early
    try:
        session.delete_thread(thread_id)
    except KeyError:
        return web.json_response({"ok": False, "error": "thread not found"}, status=404)
    _notify_event(session, "thread_delete", {"thread_id": thread_id})
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def thread_members(request: web.Request) -> web.Response:
    """Adding a member to a thread emits THREAD_MEMBERS_UPDATE."""
    session = _get_session(request)
    thread_id = request.match_info["thread_id"]
    body = await _thread_body(request)
    user_id = str(body.get("user_id") or session.user_id)
    added = [] if user_id == str(session.guild.me.id) else [user_id]
    early = await _project_session_op(
        request, "thread_members",
        args=[thread_id, added], event="thread_members_update")
    if early is not None:
        return early
    thread = session.channels.get(str(thread_id))
    if thread is None or not getattr(thread, "is_thread", False):
        return web.json_response({"ok": False, "error": "thread not found"}, status=404)
    _notify_event(session, "thread_members_update",
                  {"thread_id": thread_id, "added": added})
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def moderate(request: web.Request) -> web.Response:
    """Kick / ban / unban / timeout a simulated member with permission checks."""
    session = _get_session(request)
    action = request.match_info["action"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - unban may post an empty body
        body = {}
    user_id = str(body.get("user_id") or "")
    if not user_id.isdigit():
        return web.json_response({"ok": False, "error": "user_id required"}, status=400)
    op_args = [int(user_id)]
    if action == "timeout":
        op_args.append(int(body.get("minutes") or 10))
    method = {"kick": "kick_member", "leave": "remove_member", "ban": "ban_member",
              "unban": "unban_member", "timeout": "timeout_member"}.get(action, "")
    # the simulator recreates an unbanned user, so the world change is a join
    event = {"kick_member": "member_remove", "remove_member": "member_remove",
             "ban_member": "member_remove", "unban_member": "member_join",
             "timeout_member": "member_update"}.get(method)
    early = await _project_session_op(request, method, args=op_args,
                                      kwargs={"event_user_id": int(user_id)}, event=event)
    if early is not None:
        return early
    member_before = session.guild.get_member(int(user_id))
    try:
        if action == "kick":
            session.kick_member(int(user_id))
            _notify_event(session, "member_remove", {"_member": member_before, "via": "kick"})
        elif action == "leave":
            session.remove_member(int(user_id))
            _notify_event(session, "member_remove", {"_member": member_before})
        elif action == "ban":
            session.ban_member(int(user_id))
        elif action == "unban":
            session.unban_member(int(user_id))
        elif action == "timeout":
            session.timeout_member(int(user_id), int(body.get("minutes") or 10))
        else:
            return web.json_response({"ok": False, "error": "unknown moderation action"}, status=404)
    except discord.Forbidden as error:
        return web.json_response({"ok": False, "error": str(error)}, status=403)
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def leave(request: web.Request) -> web.Response:
    """A simulated member leaves voluntarily (fires on_member_remove)."""
    session = _get_session(request)
    body = await request.json()
    user_id = str(body.get("user_id") or "")
    if not user_id.isdigit():
        return web.json_response({"ok": False, "error": "user_id required"}, status=400)
    early = await _project_session_op(request, "remove_member", args=[int(user_id)],
                                      event="member_remove")
    if early is not None:
        return early
    try:
        member = session.remove_member(int(user_id))
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _notify_event(session, "member_remove", {"_member": member})
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def member_roles(request: web.Request) -> web.Response:
    """Grant/revoke a role on a simulated member (fires on_member_update)."""
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(
        request, "revoke_role" if body.get("op") == "revoke" else "grant_role",
        args=[int(body.get("user_id") or 0), int(body.get("role_id") or 0)],
        kwargs={"event_user_id": int(body.get("user_id") or 0)}, event="member_update")
    if early is not None:
        return early
    try:
        user_id = int(body.get("user_id"))
        role_id = int(body.get("role_id"))
        before = _member_before_snapshot(session.guild.get_member(user_id)
                                         or session.guild.me)
        if body.get("op") == "revoke":
            session.revoke_role(user_id, role_id)
        else:
            session.grant_role(user_id, role_id)
    except (AttributeError, TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _notify_event(session, "member_update", {"user_id": user_id, "_before": before})
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def guild_roles(request: web.Request) -> web.Response:
    """Create/delete a guild role (fires on_guild_role_create/delete)."""
    session = _get_session(request)
    body = await request.json()
    action = str(body.get("op") or "create")
    early = await _project_session_op(
        request, "delete_role" if action == "delete" else "create_role",
        args=[int(body.get("role_id") or 0)] if action == "delete" else [str(body.get("name") or "")],
        event="guild_role_delete" if action == "delete" else "guild_role_create")
    if early is not None:
        return early
    try:
        if action == "delete":
            role = session.delete_role(int(body.get("role_id")))
            _notify_event(session, "guild_role_delete", {"_role": role})
        else:
            role = session.create_role(str(body.get("name") or ""))
            _notify_event(session, "guild_role_create", {"_role": role})
    except (AttributeError, TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _bump(session.sid)
    return web.json_response({"ok": True, "role": {"id": str(role.id), "name": role.name},
                              "state": state(session)})


async def delete_message(request: web.Request) -> web.Response:
    """User-initiated message delete: world mutation *and* a real MESSAGE_DELETE."""
    session = _get_session(request)
    body = await request.json()
    message_id = str(body.get("message_id") or "")
    early = await _project_session_op(request, "delete_message", args=[message_id],
                                      event="message_delete")
    if early is not None:
        return early
    try:
        session.delete_message(message_id, actor_id=session.user_id)
    except (KeyError, ValueError, discord.Forbidden) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _notify_event(session, "message_delete", {"message_id": message_id})
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def delete_channel(request: web.Request) -> web.Response:
    """User-driven channel deletion (fires on_guild_channel_delete)."""
    session = _get_session(request)
    body = await request.json()
    early = await _project_session_op(request, "delete_channel_ui",
                                      args=[str(body.get("channel_id") or "")],
                                      event="guild_channel_delete")
    if early is not None:
        return early
    try:
        channel = session.delete_channel_ui(str(body.get("channel_id") or ""))
    except discord.Forbidden as error:
        return web.json_response({"ok": False, "error": str(error)}, status=403)
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    _notify_event(session, "guild_channel_delete", {"_channel": channel})
    _bump(session.sid)
    return web.json_response({"ok": True, "state": state(session)})


async def simulate_event(request: web.Request) -> web.Response:
    """Generic gateway-event entry (tests/scenarios): awaited, deterministic."""
    session = _get_session(request)
    body = await request.json()
    kind = str(body.get("kind") or "")
    if not kind:
        return web.json_response({"ok": False, "error": "kind required"}, status=400)
    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "payload must be an object"}, status=400)
    runtime = _runtime(session)
    if runtime is not None:
        # Discord Bot Mode: hand the payload to the fake gateway, which feeds
        # the real discord.py ConnectionState parser.
        try:
            delivered = await runtime.dispatch_event(kind, payload)
        except bot_worker.WorkerTimeout:
            return web.json_response(
                {"ok": False, "error": "bot stopped responding — worker terminated"}, status=504)
        except Exception as error:  # noqa: BLE001 - mirrors handler-specific errors
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        _bump(session.sid)
        return web.json_response({"ok": True, "delivered": delivered, "kind": kind,
                                  "state": state(session)})
    result = await dispatch_event(session, kind, payload)
    _bump(session.sid)
    return web.json_response(result)


async def click(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    runtime = _runtime(session)
    if runtime is not None:
        try:
            await runtime.dispatch_click(body["message_id"], body["custom_id"], body.get("values") or [])
        except Exception as error:  # noqa: BLE001
            session.log("💥", f"{type(error).__name__}: {error}", "error",
                        details={"operation": "project.callback", "status": "script_error"})
    else:
        await dispatch_click(session, body["message_id"], body["custom_id"], body.get("values") or [])
    _bump(session.sid)
    return web.json_response(state(session))


async def submit_modal(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    runtime = _runtime(session)
    values = body.get("values") or {}
    if runtime is not None:
        handled = False
        try:
            handled = await runtime.dispatch_pending_modal(values, body.get("modal_id"))
        except Exception as error:  # noqa: BLE001
            session.log("💥", f"{type(error).__name__}: {error}", "error",
                        details={"operation": "project.callback", "status": "script_error"})
        if not handled:
            session.log("⚠️", "no modal is open in the running bot", "warn", kind="event",
                        details={"operation": "interaction.modal_submit", "status": "missing_modal"})
    else:
        await dispatch_submit(session, body["modal_id"], values)
    _bump(session.sid)
    return web.json_response(state(session))


async def dismiss_modal(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    modal_id = body.get("modal_id")
    if not isinstance(modal_id, str) or not modal_id:
        return web.json_response({"ok": False, "error": "modal_id is required"}, status=400)
    runtime = _runtime(session)
    try:
        if runtime is not None:
            runtime.dismiss_modal(modal_id)
        else:
            session.dismiss_modal(modal_id, session.active_user.id)
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=403)
    _bump(session.sid)
    return web.json_response(state(session))


async def send_message(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    content = (body.get("content") or "").strip()
    files = body.get("files") if isinstance(body.get("files"), list) else None
    if not content and not files:
        return web.json_response({"ok": False, "error": "empty message"}, status=400)
    runtime = _runtime(session)
    if runtime is not None:
        try:
            await runtime.dispatch_message(content, channel_id=body.get("channel_id"))
        except Exception as error:  # noqa: BLE001
            session.log("💥", f"{type(error).__name__}: {error}", "error",
                        details={"operation": "project.callback", "status": "script_error"})
    else:
        await dispatch_message(session, content, channel_id=body.get("channel_id"), files=files)
    _bump(session.sid)
    return web.json_response(state(session))


async def run_command(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    name = body.get("name") or ""
    channel_id = body.get("channel_id")
    runtime = _runtime(session)
    if runtime is not None:
        commands = runtime.commands_payload()
        if name not in commands:
            return web.json_response({"ok": False, "error": f"unknown command /{name}"}, status=400)
        try:
            await runtime.dispatch_command(name, body.get("args") or {}, channel_id=channel_id)
        except Exception as error:  # noqa: BLE001
            session.log("💥", f"{type(error).__name__}: {error}", "error",
                        details={"operation": "project.callback", "status": "script_error"})
    else:
        if name not in session.cmd_objects:
            return web.json_response({"ok": False, "error": f"unknown command /{name}"}, status=400)
        await dispatch_command(session, name, body.get("args") or {}, channel_id=channel_id)
    _bump(session.sid)
    return web.json_response(state(session))


async def restart(request: web.Request) -> web.Response:
    session = _get_session(request)
    async with _session_project_lock(session):
        try:
            await _shutdown_runtime(session)
        except _ProjectShutdownPending as error:
            return web.json_response({"ok": False, "error": str(error)}, status=503)
        connected = session.workspace  # Restart keeps the folder connected for the next Run
        session.restart()
        if connected:
            _hold_workspace(session, connected)
        _disarm_workspace_watch(session)
        _bump(session.sid)
        return web.json_response(state(session))


def _hold_workspace(session: Session, workspace: str) -> None:
    """Re-arm the active workspace on a session after Restart.

    Restart deliberately clears workspace import state; this re-attaches the
    folder so the next Run boots that same bot ("restart, rerun" keeps the
    workspace selected). Import isolation (sys.modules/cogs) is reset — Run
    re-imports everything fresh.
    """
    try:
        folder = _workspace_dir(workspace)
    except (web.HTTPBadRequest, web.HTTPNotFound):
        return
    session.workspace = workspace
    session.workspace_root = folder


async def websocket(request: web.Request) -> web.WebSocketResponse:
    sid = request.match_info["sid"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    WS_CLIENTS.setdefault(sid, set()).add(ws)
    app = request.app
    if app.get(_AUTO_SHUTDOWN):
        app[_DESKTOP_CLIENT_CONNECTED] = True
        app[_DESKTOP_LAST_CLIENT_DISCONNECT] = None
    try:
        async for _ in ws:
            pass
    finally:
        WS_CLIENTS.get(sid, set()).discard(ws)
        if not WS_CLIENTS.get(sid) and app.get(_AUTO_SHUTDOWN) and app.get(_DESKTOP_CLIENT_CONNECTED):
            app[_DESKTOP_LAST_CLIENT_DISCONNECT] = asyncio.get_running_loop().time()
    return ws


async def _watch_desktop_clients(app: web.Application) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(_DESKTOP_POLL_INTERVAL)
        if not app[_DESKTOP_CLIENT_CONNECTED]:
            continue
        if not any(WS_CLIENTS.values()):
            disconnected_at = app[_DESKTOP_LAST_CLIENT_DISCONNECT]
            if disconnected_at is None:
                app[_DESKTOP_LAST_CLIENT_DISCONNECT] = loop.time()
            elif loop.time() - disconnected_at >= _DESKTOP_CLOSE_GRACE:
                app[_DESKTOP_SHUTDOWN_REQUESTED].set()
                return


def enable_auto_shutdown(app: web.Application) -> None:
    app[_AUTO_SHUTDOWN] = True
    app[_DESKTOP_CLIENT_CONNECTED] = False
    app[_DESKTOP_LAST_CLIENT_DISCONNECT] = None
    app[_DESKTOP_SHUTDOWN_REQUESTED] = asyncio.Event()
    app[_DESKTOP_SHUTDOWN_WATCHER] = asyncio.create_task(_watch_desktop_clients(app))


async def on_startup(app: web.Application) -> None:
    global _WORKSPACE_WATCH_TASK

    # Sandboxes whose worker process is provably gone (server was killed,
    # machine rebooted). Metadata-free directories are never touched, so a
    # second server cannot delete a live session's files.
    swept = bot_worker.sweep_orphan_sandboxes()
    if swept["removed"] or swept["kept"]:
        log.info("sandbox sweep: %d orphaned removed, %d active kept, %d without metadata",
                 len(swept["removed"]), len(swept["kept"]), len(swept["unknown"]))
    _bump._loop = asyncio.get_running_loop()
    if _WORKSPACE_WATCH_TASK is None or _WORKSPACE_WATCH_TASK.done():
        _WORKSPACE_WATCH_TASK = asyncio.create_task(_watch_workspace_files(app))
    if app.get(_AUTO_SHUTDOWN):
        enable_auto_shutdown(app)


async def shutdown_sessions(_app: web.Application) -> None:
    for task in list(_PENDING_EVENT_TASKS):
        task.cancel()
    _PENDING_EVENT_TASKS.clear()
    for runtime in list(RUNTIMES.values()):
        try:
            await runtime.shutdown()
        except Exception:
            log.exception("Error shutting down project runtime")
    RUNTIMES.clear()
    WORKSPACE_WATCHERS.clear()
    for session in tuple(SESSIONS.values()):
        try:
            session.close()
        except Exception:
            log.exception("Error closing ScriptPlayground session %s", session.sid)
    SESSIONS.clear()


async def on_cleanup(app: web.Application) -> None:
    global _WORKSPACE_WATCH_TASK
    watcher = app.get(_DESKTOP_SHUTDOWN_WATCHER)
    if watcher is not None:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    if _WORKSPACE_WATCH_TASK is not None:
        _WORKSPACE_WATCH_TASK.cancel()
        await asyncio.gather(_WORKSPACE_WATCH_TASK, return_exceptions=True)
        _WORKSPACE_WATCH_TASK = None
    WS_CLIENTS.clear()
    log.info("ScriptPlayground stopped")


def build_app(*, auto_shutdown: bool = False, data_dir: Path | None = None) -> web.Application:
    app = web.Application()
    app[_AUTO_SHUTDOWN] = auto_shutdown
    if data_dir is not None:
        configure_data_directory(data_dir)
        bootstrap_default_scripts()
    app[_DATA_DIR_KEY] = DATA_DIR
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.on_shutdown.append(shutdown_sessions)
    app.router.add_get("/", index)
    app.router.add_get("/api/auth/status", auth_status)
    app.router.add_get("/auth/discord/login", discord_login)
    app.router.add_get("/auth/discord/callback", discord_callback)
    app.router.add_get("/auth/logout", logout)
    app.router.add_get("/embeder", embeder_page)
    app.router.add_get("/api/embeder/info", embeder_info)
    app.router.add_get("/api/workspaces", list_workspaces)
    app.router.add_get("/api/workspaces/{workspace}/environment", workspace_environment)
    app.router.add_get("/api/workspaces/{workspace}/files/{filename:.*}", get_workspace_file)
    app.router.add_put("/api/workspaces/{workspace}/files/{filename:.*}", save_workspace_file)
    app.router.add_get("/api/scripts", list_scripts)
    app.router.add_get("/api/scripts/{name}", get_script)
    app.router.add_post("/api/scripts/test", test_scripts)
    app.router.add_post("/api/scripts", save_script)
    app.router.add_delete("/api/scripts/{name}", delete_script)
    app.router.add_get("/api/scenarios", list_scenarios)
    app.router.add_get("/api/scenarios/{name}", get_scenario)
    app.router.add_post("/api/scenarios", save_scenario)
    app.router.add_post("/api/session/{sid}/scenario", run_scenario_route)
    app.router.add_get("/api/designs", list_designs)
    app.router.add_get("/api/designs/{name}", get_design)
    app.router.add_post("/api/bridge/design-to-code", bridge_design_to_code)
    app.router.add_post("/api/session", create_session)
    app.router.add_get("/api/session/{sid}/state", get_state)
    app.router.add_post("/api/session/{sid}/user", set_user)
    app.router.add_post("/api/session/{sid}/members", add_member)
    app.router.add_put("/api/session/{sid}/members/{user_id}/profile", update_member_profile)
    app.router.add_post("/api/session/{sid}/run", run_code)
    app.router.add_post("/api/session/{sid}/click", click)
    app.router.add_post("/api/session/{sid}/submit", submit_modal)
    app.router.add_post("/api/session/{sid}/dismiss", dismiss_modal)
    app.router.add_post("/api/session/{sid}/message", send_message)
    app.router.add_post("/api/session/{sid}/react", react)
    app.router.add_post("/api/session/{sid}/voice", voice)
    app.router.add_post("/api/session/{sid}/upload", upload)
    app.router.add_delete("/api/session/{sid}/upload/{index}", delete_upload)
    app.router.add_post("/api/session/{sid}/channels", create_channel)
    app.router.add_post("/api/session/{sid}/moderate/{action}", moderate)
    app.router.add_post("/api/session/{sid}/command", run_command)
    app.router.add_post("/api/session/{sid}/restart", restart)
    app.router.add_post("/api/session/{sid}/reload", reload_code)
    app.router.add_post("/api/session/{sid}/project", run_code)
    app.router.add_post("/api/session/{sid}/run-file", run_workspace_file)
    app.router.add_post("/api/session/{sid}/events", simulate_event)
    app.router.add_post("/api/session/{sid}/members/leave", leave)
    app.router.add_post("/api/session/{sid}/members/roles", member_roles)
    app.router.add_post("/api/session/{sid}/roles", guild_roles)
    app.router.add_post("/api/session/{sid}/channels/delete", delete_channel)
    app.router.add_post("/api/session/{sid}/threads", create_thread)
    app.router.add_post("/api/session/{sid}/threads/{thread_id}/archive", archive_thread)
    app.router.add_post("/api/session/{sid}/threads/{thread_id}/delete", delete_thread)
    app.router.add_post("/api/session/{sid}/threads/{thread_id}/members", thread_members)
    app.router.add_post("/api/session/{sid}/messages/delete", delete_message)
    app.router.add_get("/api/session/{sid}/ws", websocket)
    if STATIC_DIR.exists():
        app.router.add_static("/static/", STATIC_DIR)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="ScriptPlayground local web UI")
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open a browser tab")
    parser.add_argument("--auto-shutdown", action="store_true", help="stop after all UI sockets close")
    parser.add_argument("--data-dir", type=Path, help="override the persistent user data directory")
    args = parser.parse_args()

    if args.data_dir:
        configure_data_directory(args.data_dir)
    elif getattr(sys, "frozen", False):
        # The packaged server bundle is read-only; writable scripts/workspaces
        # live in the OS user-data root instead (mirrors launcher.py's flow).
        configure_data_directory()
    if args.auto_shutdown or getattr(sys, "frozen", False):
        bootstrap_default_scripts()
    data_dir = None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}/"
    if _server_already_running(args.host, args.port):
        log.info("ScriptPlayground is already running at %s — not starting a second instance", url)
        return
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    log.info("ScriptPlayground listening on %s", url)
    web.run_app(build_app(auto_shutdown=args.auto_shutdown, data_dir=data_dir), host=args.host, port=args.port, print=None,
                access_log_class=_AccessLogger)


if __name__ == "__main__":
    main()
