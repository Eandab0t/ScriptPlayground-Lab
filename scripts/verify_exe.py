"""Verify the frozen app against real discord.py scripts and development mode."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1"
URL = ""


def request(path: str, body: dict | None = None) -> dict | bytes:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(f"{URL}{path}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as response:
        payload = response.read()
        if "application/json" in response.headers.get("Content-Type", ""):
            return json.loads(payload)
        return payload


def stop_process(process: subprocess.Popen[str], label: str) -> str:
    if process.poll() is None:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=5)
            raise TimeoutError(f"{label} did not shut down within 10 seconds") from error
    output, _ = process.communicate(timeout=2)
    if process.returncode != 0:
        raise RuntimeError(f"{label} exited with {process.returncode}:\n{output}")
    if "ScriptPlayground stopped" not in output:
        raise RuntimeError(f"{label} did not log clean shutdown:\n{output}")
    return output


def _exercise(label: str, command: list[str], data_dir: Path, *, dev: bool = False) -> dict:
    global URL
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    URL = f"{BASE_URL}:{port}"
    args = [*command, "--port", str(port)]
    if dev:
        args += ["--host", "127.0.0.1", "--no-browser"]
    else:
        args += ["--data-dir", str(data_dir)]
    env = os.environ.copy()
    env.pop("SCRIPTPLAYGROUND_DATA_DIR", None)
    env["SCRIPTPLAYGROUND_NO_BROWSER"] = "1"
    started = time.perf_counter()
    process = subprocess.Popen(
        args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

    try:
        for _ in range(200):
            if process.poll() is not None:
                output, _ = process.communicate(timeout=2)
                raise RuntimeError(f"{label} exited during startup ({process.returncode}):\n{output}")
            try:
                page = request("/")
                if isinstance(page, bytes) and b"ScriptPlayground" in page:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            raise TimeoutError(f"{label} HTTP page did not become ready within 30 seconds")
        startup_ms = round((time.perf_counter() - started) * 1000, 1)

        session = request("/api/session", {})
        data_script = data_dir / "scripts" / "demo.py"
        if not isinstance(session, dict) or not session.get("sid") or not data_script.is_file():
            raise RuntimeError(f"{label} did not seed a persistent demo script")
        demo = data_script.read_text(encoding="utf-8")
        if session.get("example") != demo:
            raise RuntimeError(f"{label} did not load the saved demo as the startup example")

        embeder = request("/embeder")
        scenarios = request("/api/scenarios")
        designs = request("/api/designs")
        if not isinstance(embeder, bytes) or len(embeder) < 1_000_000:
            raise RuntimeError(f"{label} did not serve bundled Embeder (received {len(embeder)} bytes)")
        if not any(item["name"] == "Greeting" for item in scenarios["scenarios"]):
            raise RuntimeError(f"{label} did not seed the Greeting scenario")
        if not any(item["name"] == "Club Welcome" for item in designs["designs"]):
            raise RuntimeError(f"{label} did not seed the Club Welcome design")

        sid = session["sid"]
        run = request(f"/api/session/{sid}/run", {"code": demo})
        if not run.get("ok"):
            raise RuntimeError(f"{label} failed to run real scripts/demo.py: {run}")
        state = request(f"/api/session/{sid}/state")
        commands = {"echo", "greet", "roll"}
        if not commands <= set(state["commands"]):
            raise RuntimeError(f"{label} did not collect slash commands: {state['commands']}")
        first = state["messages"][0]
        ids = {item["custom_id"] for item in first["components"]}
        if not {"hi", "form", "flavor"} <= ids:
            raise RuntimeError(f"{label} missed button/select components: {ids}")
        if not any(f.get("data_uri") for f in state["messages"][1]["files"]):
            raise RuntimeError(f"{label} could not serialize locally generated demo images")

        clicked = request(f"/api/session/{sid}/click", {
            "message_id": first["id"], "custom_id": "hi", "values": [],
        })
        if not any(message["ephemeral"] for message in clicked["messages"]):
            raise RuntimeError(f"{label} button handler failed to respond")
        opened = request(f"/api/session/{sid}/click", {
            "message_id": first["id"], "custom_id": "form", "values": [],
        })
        modal = opened["modals"][0]
        if modal["title"] != "Tell us about you":
            raise RuntimeError(f"{label} got unexpected modal {modal['title']!r}")
        fields = {field["label"]: field["custom_id"] for field in modal["items"]}
        submitted = request(f"/api/session/{sid}/submit", {
            "modal_id": modal["id"],
            "values": {fields["Nickname"]: "Ada", fields["Favorite language"]: "Python"},
        })
        if not any(embed.get("title") == "Form received"
                   for message in submitted["messages"] for embed in message["embeds"]):
            raise RuntimeError(f"{label} modal submit handler failed to respond")

        selected = request(f"/api/session/{sid}/click", {
            "message_id": first["id"], "custom_id": "flavor", "values": ["chocolate"],
        })
        first_after_select = next(message for message in selected["messages"] if message["id"] == first["id"])
        if "🍫 6" not in first_after_select["content"]:
            raise RuntimeError(f"{label} select handler did not edit the message: {first_after_select['content']!r}")

        echoed = request(f"/api/session/{sid}/command", {
            "name": "echo", "args": {"text": "package check", "shout": True},
        })["messages"][-1]["content"]
        greeted = request(f"/api/session/{sid}/command", {
            "name": "greet", "args": {"who": "111111111111111111", "tier": "salute"},
        })["messages"][-1]["content"]
        if echoed != "🗣️ PACKAGE CHECK" or greeted != "🫡 Saluting <@111111111111111111>!":
            raise RuntimeError(f"{label} slash commands returned unexpected results: {echoed!r}, {greeted!r}")

        shutdown = ""
        if not dev:
            shutdown = stop_process(process, label)
        return {
            "startup_ms": startup_ms,
            "commands": sorted(commands),
            "components": sorted({"hi", "form", "flavor"}),
            "modal": modal["title"],
            "modal_fields": sorted(fields),
            "button_reply": "ephemeral",
            "modal_reply": "Form received",
            "select": first_after_select["content"],
            "echo": echoed,
            "greet": greeted,
            "embeder_bytes": len(embeder),
            "scenario": "Greeting",
            "design": "Club Welcome",
            "graceful_shutdown": "ScriptPlayground stopped" in shutdown if not dev else None,
        }
    finally:
        if dev and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        elif process.poll() is None:
            try:
                stop_process(process, label)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    executable = ROOT / "dist" / "ScriptPlayground" / "ScriptPlayground.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"Build the executable first: {executable} not found")
    with tempfile.TemporaryDirectory(prefix="ScriptPlayground-verify-") as temporary:
        frozen_data = Path(temporary) / "frozen-data"
        frozen_data.mkdir()
        frozen = _exercise("Frozen executable", [str(executable)], frozen_data)
        print("PASS frozen real-script behavior: slash commands, button, select, modal, generated files")
        print(f"PASS one-folder bundled resources and persistent seeding: {frozen['embeder_bytes']:,}-byte Embeder, starter scenario/design, graceful shutdown")
        print(f"Executable size: {executable.stat().st_size / 1024 / 1024:.2f} MiB")

        development = _exercise("Python main.py", [sys.executable, str(ROOT / "main.py")], ROOT, dev=True)
        exe_time, python_time = frozen.pop("startup_ms"), development.pop("startup_ms")
        frozen.pop("graceful_shutdown")
        development.pop("graceful_shutdown")
        if frozen != development:
            raise RuntimeError(f"Frozen/dev behavior differs:\nfrozen={frozen}\ndev={development}")
        print("PASS development parity: same commands, components, modal replies, resources, and data")
        print(f"Cold process → first HTTP page: exe {exe_time:.1f} ms; Python main.py {python_time:.1f} ms")
        print("This times process launch through the first server-rendered page response, not a physical display paint.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"FAIL packaged integration verification: {error}", file=sys.stderr)
        raise SystemExit(1) from error
