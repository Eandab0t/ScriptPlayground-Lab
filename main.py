"""ScriptPlayground server: a local web playground for Discord bot UI code.

    python main.py [--port 8741]

Serves static/index.html, keeps sessions in memory (no persistence), and
streams timeline update nudges to the browser over a websocket. Run `python main.py --help`
for options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
import threading
import webbrowser
from pathlib import Path

from aiohttp import web

import bridge
from playground import (
    Session,
    dispatch_click,
    dispatch_command,
    dispatch_message,
    dispatch_submit,
    run_script,
    state,
)
from project_state import validate_project

log = logging.getLogger("playground")
STATIC_DIR = Path(__file__).parent / "static"
SCRIPTS_DIR = Path(__file__).parent / "scripts"
WORKSPACES_DIR = Path(__file__).parent / "bots"
EMBEDER_DIR = Path(__file__).parent / "embeder"
_DESIGNS_SUBDIR = "designs"
_MAIN_SCRIPT = "demo"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_ -]{1,50}$")


def _script_path(name: str) -> Path:
    """Resolve a library name to a .py file, rejecting path escapes."""
    if not _SAFE_NAME.match(name):
        raise web.HTTPBadRequest(text="invalid script name")
    return SCRIPTS_DIR / f"{name}.py"

SESSIONS: dict[str, Session] = {}
WS_CLIENTS: dict[str, set[web.WebSocketResponse]] = {}


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


# --------------------------------------------------------------- routes


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def create_session(_request: web.Request) -> web.Response:
    sid = secrets.token_hex(8)
    SESSIONS[sid] = Session(sid)
    WS_CLIENTS.setdefault(sid, set())
    main_script = SCRIPTS_DIR / f"{_MAIN_SCRIPT}.py"
    example = main_script.read_text(encoding="utf-8") if main_script.exists() else ""
    return web.json_response({"sid": sid, "example": example, "state": state(SESSIONS[sid])})


_HIDDEN_SCRIPTS = {"vendor_embeder"}  # internal tooling, not runnable demos


async def list_workspaces(_request: web.Request) -> web.Response:
    WORKSPACES_DIR.mkdir(exist_ok=True)
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


# --------------------------------------------------------------- embeder bridge


def _designs_dir() -> Path:
    d = SCRIPTS_DIR / _DESIGNS_SUBDIR
    d.mkdir(exist_ok=True)
    return d


def _design_path(name: str) -> Path:
    """Resolve a design name, rejecting path escapes (same rules as scripts)."""
    if not _SAFE_NAME.match(name):
        raise web.HTTPBadRequest(text="invalid design name")
    return _designs_dir() / f"{name}.discordv2proj.json"


async def embeder_page(request: web.Request) -> web.FileResponse:
    """Serve the DiscordEmbeder single-file build (mount-path agnostic)."""
    return web.FileResponse(EMBEDER_DIR / "index.html")


async def embeder_info(_request: web.Request) -> web.Response:
    marker = EMBEDER_DIR / "VENDORED_FROM.txt"
    if not marker.exists():
        return web.json_response({"ok": False, "error": "vendor provenance is unavailable"}, status=404)
    return web.json_response({"ok": True, "provenance": marker.read_text(encoding="utf-8")})


async def bridge_design_to_code(request: web.Request) -> web.Response:
    body = await request.json()
    design = body.get("design")
    try:
        code = bridge.design_to_code(design)
    except (TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    save_name = (body.get("save") or "").strip()
    if save_name:
        if not _SAFE_NAME.match(save_name):
            return web.json_response({"ok": False, "error": "invalid save name"}, status=400)
        _design_path(save_name).write_text(json.dumps(design, indent=2), encoding="utf-8")
    return web.json_response({"ok": True, "code": code})


async def list_designs(_request: web.Request) -> web.Response:
    d = _designs_dir()
    designs = []
    for f in sorted(d.glob("*.discordv2proj.json")):
        try:
            data = validate_project(json.loads(f.read_text(encoding="utf-8")))
            meta = data.get("metadata") or {}
            name = f.name.removesuffix(".discordv2proj.json")
            designs.append({"name": name, "project": (meta.get("name") or name),
                            "updatedAt": meta.get("updatedAt") or ""})
        except (OSError, TypeError, ValueError):
            continue
    return web.json_response({"designs": designs})


async def get_design(request: web.Request) -> web.Response:
    path = _design_path(request.match_info["name"])
    if not path.exists():
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
    code = body.get("code") or ""
    if not code.strip():
        return web.json_response({"ok": False, "error": "Nothing to run — the editor is empty."},
                                 status=400)
    result = await run_script(session, code)
    _bump(session.sid)
    return web.json_response(result)


async def click(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    await dispatch_click(session, body["message_id"], body["custom_id"], body.get("values") or [])
    _bump(session.sid)
    return web.json_response(state(session))


async def submit_modal(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    await dispatch_submit(session, body["modal_id"], body.get("values") or {})
    _bump(session.sid)
    return web.json_response(state(session))


async def send_message(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        return web.json_response({"ok": False, "error": "empty message"}, status=400)
    await dispatch_message(session, content, channel_id=body.get("channel_id"))
    _bump(session.sid)
    return web.json_response(state(session))


async def run_command(request: web.Request) -> web.Response:
    session = _get_session(request)
    body = await request.json()
    name = body.get("name") or ""
    if name not in session.cmd_objects:
        return web.json_response({"ok": False, "error": f"unknown command /{name}"}, status=400)
    channel_id = body.get("channel_id")
    if channel_id:
        session.channel = session.channels.get(str(channel_id), session.channel)
    await dispatch_command(session, name, body.get("args") or {})
    _bump(session.sid)
    return web.json_response(state(session))


async def restart(request: web.Request) -> web.Response:
    session = _get_session(request)
    session.restart()
    _bump(session.sid)
    return web.json_response(state(session))


async def websocket(request: web.Request) -> web.WebSocketResponse:
    """One websocket per browser tab; we push lightweight update nudges."""
    sid = request.match_info["sid"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    WS_CLIENTS.setdefault(sid, set()).add(ws)
    try:
        async for _ in ws:
            pass  # client -> server messages are unused
    finally:
        WS_CLIENTS.get(sid, set()).discard(ws)
    return ws


# --------------------------------------------------------------- app wiring


async def on_startup(app: web.Application) -> None:
    _bump._loop = asyncio.get_running_loop()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/", index)
    app.router.add_get("/embeder", embeder_page)
    app.router.add_get("/api/embeder/info", embeder_info)
    app.router.add_get("/api/workspaces", list_workspaces)
    app.router.add_get("/api/workspaces/{workspace}/files/{filename}", get_workspace_file)
    app.router.add_put("/api/workspaces/{workspace}/files/{filename}", save_workspace_file)
    app.router.add_get("/api/scripts", list_scripts)
    app.router.add_get("/api/scripts/{name}", get_script)
    app.router.add_post("/api/scripts/test", test_scripts)
    app.router.add_post("/api/scripts", save_script)
    app.router.add_delete("/api/scripts/{name}", delete_script)
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
    app.router.add_get("/api/session/{sid}/ws", websocket)
    if STATIC_DIR.exists():
        app.router.add_static("/static/", STATIC_DIR)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="ScriptPlayground local web UI")
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open a browser tab")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    log.info("ScriptPlayground listening on %s", url)
    web.run_app(build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
