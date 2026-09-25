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

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from aiohttp.web_log import AccessLogger

import bot_runtime
import bridge
import env_discovery
from playground import (
    Session,
    dispatch_click,
    dispatch_command,
    dispatch_message,
    dispatch_submit,
    run_scenario,
    run_script,
    state,
    validate_scenario,
)
from project_state import validate_project

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
RUNTIMES: dict[str, bot_runtime.ProjectRuntime] = {}  # sid -> booted project runtime
WS_CLIENTS: dict[str, set[web.WebSocketResponse]] = {}
OAUTH_STATES: dict[str, float] = {}
AUTH_SESSIONS: dict[str, dict[str, str | None]] = {}


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


async def workspace_environment(request: web.Request) -> web.Response:
    """Configuration discovery for a workspace's dotenv files (values redacted)."""
    workspace = request.match_info["workspace"]
    if not _SAFE_NAME.match(workspace):
        raise web.HTTPBadRequest(text="invalid workspace")
    folder = WORKSPACES_DIR / workspace
    if not folder.is_dir():
        raise web.HTTPNotFound(text="workspace not found")
    return web.json_response(env_discovery.discover(folder))


async def list_workspaces(_request: web.Request) -> web.Response:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    workspaces = []
    for folder in sorted(WORKSPACES_DIR.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        files = [str(path.relative_to(folder)).replace("\\", "/")
                 for path in folder.rglob("*.py") if path.is_file()]
        workspaces.append({"name": folder.name, "files": sorted(files)})
    return web.json_response({"workspaces": workspaces})


def _workspace_file(workspace: str, filename: str = "bot.py") -> Path:
    if not _SAFE_NAME.match(workspace) or Path(filename).name != filename:
        raise web.HTTPBadRequest(text="invalid workspace file")
    path = WORKSPACES_DIR / workspace / filename
    if path.suffix != ".py":
        raise web.HTTPBadRequest(text="only Python files are supported")
    return path


async def get_workspace_file(request: web.Request) -> web.Response:
    path = _workspace_file(request.match_info["workspace"], request.match_info["filename"])
    if not path.is_file():
        raise web.HTTPNotFound(text="workspace file not found")
    return web.json_response({"name": path.name, "code": path.read_text(encoding="utf-8")})


async def save_workspace_file(request: web.Request) -> web.Response:
    path = _workspace_file(request.match_info["workspace"], request.match_info["filename"])
    body = await request.json()
    code = body.get("code") or ""
    if not code.strip():
        return web.json_response({"ok": False, "error": "Refusing to save empty code."}, status=400)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return web.json_response({"ok": True, "name": path.name})


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
            result = await run_script(session, path.read_text(encoding="utf-8"))
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
        result = await run_scenario(session, body.get("scenario", body))
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
    try:
        session.set_user(int(body.get("user_id")))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "unknown simulated user"}, status=400)
    return web.json_response(state(session))


async def run_code(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    workspace = (body.get("workspace") or "").strip()
    if workspace:
        # Project run: boot the whole bot (login → setup_hook → cogs → sync).
        folder = WORKSPACES_DIR / workspace
        if not folder.is_dir():
            return web.json_response({"ok": False, "error": f"unknown workspace {workspace!r}"}, status=404)
        old = RUNTIMES.pop(session.sid, None)
        if old is not None:
            await old.shutdown()
        session.restart()
        try:
            RUNTIMES[session.sid] = await asyncio.wait_for(
                bot_runtime.run_project(session, folder), timeout=90
            )
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "project boot exceeded 90s"}, status=504)
        except BaseException as error:  # noqa: BLE001 - details were logged to the timeline
            return web.json_response({"ok": False, "error": f"{type(error).__name__}: {error}"}, status=500)
        _bump(session.sid)
        return web.json_response({"ok": True, "mode": "project",
                                  "status": RUNTIMES[session.sid].status()})
    code = body.get("code") or ""
    if not code.strip():
        return web.json_response({"ok": False, "error": "Nothing to run — the editor is empty."}, status=400)
    result = await run_script(session, code)
    _bump(session.sid)
    return web.json_response(result)


async def run_project_file(request: web.Request) -> web.Response:
    """Run a workspace's entry file through the offline project runtime."""
    body = {} if request.can_read_body is False else await request.json()
    workspace = (body.get("workspace") or request.match_info.get("workspace") or "").strip()
    session = _get_session(request)
    folder = WORKSPACES_DIR / workspace
    if not workspace or not folder.is_dir():
        return web.json_response({"ok": False, "error": f"unknown workspace {workspace!r}"}, status=404)
    old = RUNTIMES.pop(session.sid, None)
    if old is not None:
        await old.shutdown()
    session.restart()
    try:
        RUNTIMES[session.sid] = await asyncio.wait_for(
            bot_runtime.run_project(session, folder), timeout=90
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "project boot exceeded 90s"}, status=504)
    except BaseException as error:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"{type(error).__name__}: {error}"}, status=500)
    _bump(session.sid)
    return web.json_response({"ok": True, "mode": "project",
                              "status": RUNTIMES[session.sid].status()})


def _runtime(session: Session) -> bot_runtime.ProjectRuntime | None:
    return RUNTIMES.get(session.sid)


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
            handled = await runtime.dispatch_pending_modal(values)
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


async def send_message(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        return web.json_response({"ok": False, "error": "empty message"}, status=400)
    runtime = _runtime(session)
    if runtime is not None:
        try:
            await runtime.dispatch_message(content, channel_id=body.get("channel_id"))
        except Exception as error:  # noqa: BLE001
            session.log("💥", f"{type(error).__name__}: {error}", "error",
                        details={"operation": "project.callback", "status": "script_error"})
    else:
        await dispatch_message(session, content, channel_id=body.get("channel_id"))
    _bump(session.sid)
    return web.json_response(state(session))


async def run_command(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    name = body.get("name") or ""
    channel_id = body.get("channel_id")
    if channel_id:
        session.channel = session.channels.get(str(channel_id), session.channel)
    runtime = _runtime(session)
    if runtime is not None:
        commands = runtime.commands_payload()
        if name not in commands:
            return web.json_response({"ok": False, "error": f"unknown command /{name}"}, status=400)
        try:
            await runtime.dispatch_command(name, body.get("args") or {})
        except Exception as error:  # noqa: BLE001
            session.log("💥", f"{type(error).__name__}: {error}", "error",
                        details={"operation": "project.callback", "status": "script_error"})
    else:
        if name not in session.cmd_objects:
            return web.json_response({"ok": False, "error": f"unknown command /{name}"}, status=400)
        await dispatch_command(session, name, body.get("args") or {})
    _bump(session.sid)
    return web.json_response(state(session))


async def restart(request: web.Request) -> web.Response:
    session = _get_session(request)
    old = RUNTIMES.pop(session.sid, None)
    if old is not None:
        await old.shutdown()
    session.restart()
    _bump(session.sid)
    return web.json_response(state(session))


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
    _bump._loop = asyncio.get_running_loop()
    if app.get(_AUTO_SHUTDOWN):
        enable_auto_shutdown(app)


async def shutdown_sessions(_app: web.Application) -> None:
    for runtime in list(RUNTIMES.values()):
        try:
            await runtime.shutdown()
        except Exception:
            log.exception("Error shutting down project runtime")
    RUNTIMES.clear()
    for session in tuple(SESSIONS.values()):
        try:
            session.close()
        except Exception:
            log.exception("Error closing ScriptPlayground session %s", session.sid)
    SESSIONS.clear()


async def on_cleanup(app: web.Application) -> None:
    watcher = app.get(_DESKTOP_SHUTDOWN_WATCHER)
    if watcher is not None:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
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
    app.router.add_get("/api/workspaces/{workspace}/files/{filename}", get_workspace_file)
    app.router.add_put("/api/workspaces/{workspace}/files/{filename}", save_workspace_file)
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
    app.router.add_post("/api/session/{sid}/run", run_code)
    app.router.add_post("/api/session/{sid}/click", click)
    app.router.add_post("/api/session/{sid}/submit", submit_modal)
    app.router.add_post("/api/session/{sid}/message", send_message)
    app.router.add_post("/api/session/{sid}/command", run_command)
    app.router.add_post("/api/session/{sid}/restart", restart)
    app.router.add_post("/api/session/{sid}/project", run_project_file)
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

    data_dir = configure_data_directory(args.data_dir) if args.data_dir else None
    if args.auto_shutdown or getattr(sys, "frozen", False):
        bootstrap_default_scripts()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    log.info("ScriptPlayground listening on %s", url)
    web.run_app(build_app(auto_shutdown=args.auto_shutdown, data_dir=data_dir), host=args.host, port=args.port, print=None,
                access_log_class=_AccessLogger)


if __name__ == "__main__":
    main()
