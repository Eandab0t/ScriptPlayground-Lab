"""Worker subprocess runtime: one real discord.py bot per OS process.

Discord Bot Mode execution path:

    server (aiohttp)
        ↓  NDJSON over stdio pipes
    bot_worker.py subprocess (cwd = the bot's private sandbox)
        ├─ imports playground (headless Session owns the simulator state)
        ├─ boots ProjectRuntime (real discord.py + fake REST/gateway transport)
        └─ patches + asyncio loop + CWD + modules are process-private

The server NEVER executes bot code in its own process, never chdirs for a
bot, and can always recover by killing the worker (CPU-bound loops included).
Every state change ships back as one full `Session.state()` snapshot, which
the server caches and the browser renders exactly like Script Mode state.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
_BOOT_TIMEOUT = float(os.environ.get("SCRIPTPLAYGROUND_BOOT_TIMEOUT", "60"))
_OP_TIMEOUT = 30.0
_SHUTDOWN_GRACE = 8.0
_KILL_GRACE = 5.0


# --------------------------------------------------------------------------
# worker side (runs inside the subprocess)
# --------------------------------------------------------------------------


class _StdoutFilter:
    """sys.stdout replacement: user prints become log events, not protocol noise."""

    def __init__(self, emit) -> None:
        self._emit = emit
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._emit(line)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - nothing buffered across flush
        if self._buffer.strip():
            self._emit(self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:  # pragma: no cover - compatibility shim
        return False

    def __getattr__(self, name):  # pragma: no cover - passthrough for io API
        return getattr(sys.__stdout__, name)


class WorkerSession:
    """One booted bot inside the worker: local Session + ProjectRuntime."""

    def __init__(self, sandbox: Path, tag: str, on_exception=None, log_sink=None,
                 display: str | None = None) -> None:
        import playground as pg

        self.pg = pg
        self.session = pg.Session(f"worker-{tag}")
        import bot_runtime

        self.bot_runtime = bot_runtime
        self.runtime = bot_runtime.ProjectRuntime(self.session, sandbox, tag, on_exception)
        if display:
            # errors/logs name the real workspace folder, not "<folder>-<tag>"
            self.runtime.workspace = Path(display)
        self.log_sink = log_sink or (lambda: None)

    def snapshot(self, req: int | None = None, ok: bool = True,
                 error: str | None = None, result=None) -> dict:
        from playground import state as pg_state

        self.log_sink()  # user print()/stderr becomes a timeline entry like Script Mode

        state = pg_state(self.session)
        commands = self.runtime.commands_payload()
        state["commands"] = commands  # synced app commands live on the runtime, not the session
        payload: dict = {
            "type": "state",
            "ok": ok,
            "state": state,
            "commands": commands,
            "last_run": state.get("last_run"),
            # the runtime copies the sandbox once more; the server deletes this
            # one too (the worker may be killed before it can clean up)
            "inner_sandbox": str(self.runtime.sandbox),
            "status": self.runtime.status(),
            "worker_pid": os.getpid(),
        }
        if error is not None:
            payload["error"] = error
        if result is not None:
            payload["result"] = result
        if req is not None:
            payload["req"] = req
        return payload


def _install_network_blockade(session) -> None:
    """Fail loudly if anything tries a real network request inside the worker.

    The fake transport intercepts every discord HTTP route before aiohttp and
    replaces the gateway in Client.connect, so any call that reaches aiohttp
    itself is an escape from the sandbox — record it instead of performing it.
    """
    import aiohttp


    async def blockade(self, method, str_or_url, *args, **kwargs):
        violation = f"network request attempted: {method} {str_or_url}"
        session.log("🛡", violation, "error",
                    details={"operation": "network.blocked", "status": "script_error"})
        raise AssertionError(violation)

    aiohttp.ClientSession._request = blockade  # type: ignore[method-assign]
    session.log("🛡", "network blockade installed — no real Discord request can leave",
                details={"operation": "network.blockade", "status": "installed"})


async def _worker_main(sandbox: Path, tag: str, display: str | None = None) -> int:
    lines: queue.SimpleQueue[str | None] = queue.SimpleQueue()

    def read_stdin() -> None:
        for raw in sys.stdin:
            lines.put(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
        lines.put(None)

    threading.Thread(target=read_stdin, daemon=True, name="worker-stdin").start()

    def send(payload: dict) -> None:
        os.write(1, (json.dumps(payload, default=str) + "\n").encode("utf-8"))

    log_buffer: list[tuple[str, str]] = []

    def record(icon: str, text: str) -> None:
        log_buffer.append((icon, text))

    def flush_logs() -> None:
        if worker is not None and log_buffer:
            for icon, text in log_buffer:
                worker.session.log(icon, text)
        log_buffer.clear()

    sys.stdout = _StdoutFilter(lambda text: record("🖨️", text))
    sys.stderr = _StdoutFilter(lambda text: record("⚠️", text))

    worker: WorkerSession | None = None
    booted = False

    def on_exception() -> None:
        if worker is not None and booted:
            send(worker.snapshot())  # unsolicited: callback crash already logged

    async def handle(payload: dict) -> None:
        nonlocal worker, booted
        kind = payload.get("type")
        req = payload.get("req")
        if kind == "boot":
            worker = WorkerSession(sandbox, tag, on_exception, flush_logs, display)
            _install_network_blockade(worker.session)
            try:
                await worker.runtime.boot()
                booted = True
                send(worker.snapshot(req=req, ok=True))
            except BaseException as error:  # noqa: BLE001 - record + report + reply
                booted = True  # errors flow through snapshots from now on
                worker.runtime._record_exception(error, "project.run")
                send(worker.snapshot(req=req, ok=False, error=f"{type(error).__name__}: {error}"))
            return
        if kind == "op":
            assert worker is not None
            method = getattr(worker.runtime, str(payload.get("op")))
            args = payload.get("args") or []
            kwargs = payload.get("kwargs") or {}
            ok, error, result = True, None, None
            try:
                result = method(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
            except BaseException as exc:  # noqa: BLE001 - callback errors are logged
                ok, error, result = False, f"{type(exc).__name__}: {exc}", None
            send(worker.snapshot(req=req, ok=ok, error=error, result=result))
            return
        if kind == "session":
            assert worker is not None
            args = payload.get("args") or []
            kwargs = payload.get("kwargs") or {}
            event = payload.get("event")
            ok, error, result = True, None, None
            try:
                if event:
                    # mutation + its gateway event, in that order, one round trip
                    result = await worker.runtime.apply_ui_action(str(payload.get("method")),
                                                                  args, kwargs, str(event))
                else:
                    result = getattr(worker.session, str(payload.get("method")))(*args, **kwargs)
                    if asyncio.iscoroutine(result):
                        result = await result
            except BaseException as exc:  # noqa: BLE001
                ok, error, result = False, f"{type(exc).__name__}: {exc}", None
            send(worker.snapshot(req=req, ok=ok, error=error, result=result))
            return
        if kind == "shutdown":
            if worker is not None:
                with contextlib.suppress(BaseException):
                    await worker.runtime.shutdown()
                    # shutdown cleans up via a fire-and-forget task; wait for it
                    # so the sandbox is gone before the process is allowed to exit
                    deadline = time.monotonic() + 5
                    while worker.runtime.sandbox.exists() and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    shutil.rmtree(worker.runtime.sandbox, ignore_errors=True)
            send({"type": "stopped"})

    pending: asyncio.Queue[dict | None] = asyncio.Queue()

    async def pump() -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, lines.get)
            if line is None:
                pending.put_nowait(None)
                return
            line = line.strip()
            if not line:
                continue
            try:
                pending.put_nowait(json.loads(line))
            except ValueError:
                continue

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            payload = await pending.get()
            if payload is None:
                return 0  # server closed the pipe
            await handle(payload)
            if payload.get("type") == "shutdown":
                return 0
    except BaseException as error:  # noqa: BLE001 - report it, never swallow it
        # Anything escaping handle() is the bot's own code (or a simulator bug)
        # taking the worker down. stderr is buffered into log_buffer, so flush
        # first or the traceback dies with the process.
        flush_logs()
        send({"type": "fatal",
              "error": f"{type(error).__name__}: {error}",
              "traceback": traceback.format_exc()})
        return 1
    finally:
        pump_task.cancel()
        if worker is not None:  # crash / EOF: still remove the runtime's own copy
            shutil.rmtree(worker.runtime.sandbox, ignore_errors=True)
        with contextlib.suppress(BaseException):
            await pump_task


def main() -> None:
    sandbox = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    tag = sys.argv[2] if len(sys.argv) > 2 else uuid.uuid4().hex[:8]
    display = sys.argv[3] if len(sys.argv) > 3 else None
    sys.exit(asyncio.run(_worker_main(sandbox, tag, display)))


# --------------------------------------------------------------------------
# server side (runs inside the aiohttp process)
# --------------------------------------------------------------------------


class WorkerTimeout(RuntimeError):
    """The worker did not answer within its deadline and was hard-killed."""


class WorkerBootFailed(RuntimeError):
    """The worker booted the module but the bot itself failed (details in last_run)."""


SERVER_INSTANCE_ID = uuid.uuid4().hex
"""Identifies this server process inside sandbox metadata."""

TERMINATION_REASONS = ("stopped", "boot_failed", "boot_timeout", "timeout",
                       "bot_exception", "crashed")
"""Every reason a worker can be gone. status()["termination"] is one of these."""

TERMINATION_REASONS = ("stopped", "boot_failed", "boot_timeout", "timeout",
                       "bot_exception", "crashed")
"""Every reason a worker can be gone. status()["termination"] is one of these."""


class WorkerProjectRuntime:
    """Server-side handle for one bot worker subprocess (Discord Bot Mode).

    Duck-types the ProjectRuntime surface main.py already calls
    (dispatch_message/command/click, dispatch_pending_modal, dismiss_modal,
    commands_payload, status, shutdown) so routes need no mode branching.
    """

    mode = "project"

    def __init__(self, session, workspace: Path, tag: str | None = None,
                 on_exception=None) -> None:
        import bot_runtime

        self.session = session
        self.workspace = Path(workspace)
        self.on_exception = on_exception
        self.tag = tag or uuid.uuid4().hex[:8]
        self.sandbox = bot_runtime._make_sandbox(self.workspace, self.tag)
        self.bot = None  # parity attribute: real bot object lives in the worker
        self.process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._state: dict | None = None
        self._status: dict = {}
        self._req = 0
        self._closed = False
        self._stopping = False
        self._booted = asyncio.Event()
        self._boot_error: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._mirrored = 0  # server timeline events already folded into the snapshot
        self._inner_sandbox: str | None = None  # the worker's own copy (cleaned by us if killed)
        self.entry = "bot.py"
        self._termination: str | None = None  # see _classify_exit for the taxonomy
        self._termination_reason: str | None = None
        self._exit_code: int | None = None
        self._fatal: dict | None = None  # the worker's own last-words report

    def _log_server(self, icon: str, text: str, details: dict | None = None) -> None:
        """Log a server-side lifecycle event and keep it in the rendered snapshot.

        The browser renders the worker's state JSON, so events the server adds
        itself (worker ready / terminated / crashed) must be mirrored in.
        """
        self.session.log(icon, text, "error" if icon in ("💥", "🛑") else None,
                         details=details)
        self._mirror_into_state()

    def _mirror_into_state(self) -> None:
        events = self.session.events
        self._mirrored = min(self._mirrored, len(events))  # restart() may clear the timeline
        snapshot = getattr(self.session, "_project_state", None)
        if snapshot is None:
            return
        new_events = events[self._mirrored:]
        if new_events:
            snapshot["events"] = list(snapshot.get("events") or []) + list(new_events)
        self._mirrored = len(events)

    # -- process lifecycle -------------------------------------------------

    async def boot(self) -> None:
        import bot_runtime

        env = os.environ.copy()
        for key in ("DISCORD_TOKEN", "BOT_TOKEN", "DISCORD_BOT_TOKEN", "TOKEN"):
            env[key] = "offline-simulated-token"
        env.pop("GROQ_KEY", None)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(APP_DIR / "bot_worker.py"),
            str(self.sandbox), self.tag, str(self.workspace),
            cwd=str(self.sandbox), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=creationflags,
        )
        bot_runtime.write_sandbox_meta(
            self.sandbox, session_id=str(getattr(self.session, "sid", "")),
            worker_pid=self.worker_pid, server_instance_id=SERVER_INSTANCE_ID,
        )
        self._reader_task = asyncio.create_task(self._pump())
        try:
            reply = await self._request({"type": "boot"}, timeout=_BOOT_TIMEOUT, boot=True)
        except WorkerTimeout as error:
            self._termination = "boot_timeout"
            self._termination_reason = f"worker did not answer boot in {_BOOT_TIMEOUT:.0f}s"
            self._kill(f"boot timeout after {_BOOT_TIMEOUT:.0f}s (worker killed)")
            raise RuntimeError(
                "bot worker did not become ready in time and was terminated"
            ) from error
        except (ConnectionError, RuntimeError):
            await self.shutdown(keep_state=True)
            detail = self._boot_error or "worker exited during boot"
            raise RuntimeError(f"bot worker failed: {detail}")
        if not reply.get("ok"):  # the worker booted the module and it failed
            self._termination = "boot_failed"
            self._termination_reason = str(reply.get("error") or "bot raised while booting")
            self._log_server("🚨", "bot failed to boot", {"operation": "worker.boot",
                                                           "status": "boot_failed",
                                                           "reason": reply.get("error")})
            await self.shutdown(keep_state=True)  # keep the failed boot's timeline + last_run
            raise WorkerBootFailed(reply.get("error") or "worker boot failed")
        self._log_server("✅", f"bot is ready in worker process {self.worker_pid}",
                         {"operation": "project.ready", "status": "ready",
                          "runtime": "worker", "pid": self.worker_pid})

    @property
    def worker_pid(self) -> int:
        return self.process.pid if self.process is not None else 0

    async def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        process = self.process
        try:
            async for raw in process.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue  # never expected: prints are forwarded as JSON logs
                await self._handle_event(payload)
        finally:
            code = await process.wait()
            self._exit_code = code
            self._classify_exit(code)
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError(f"worker exited (code {code})"))
            self._pending.clear()
            if not self._stopping and code != 0 and self.on_exception is not None:
                self.on_exception()
            if self._closed or not self._stopping:
                # A worker that dies unexpectedly owns its own sandbox too: do
                # not leave it for the next startup sweep to guess about.
                self._cleanup_sandbox()

    def _classify_exit(self, code: int | None) -> None:
        """Record exactly why the worker is gone, and say so once in the timeline.

        Taxonomy (also documented in docs/COMPATIBILITY.md):

            stopped         user asked for it / normal shutdown
            boot_failed     the bot's own code raised while booting
            boot_timeout    never answered the boot request in time
            timeout         an operation missed its deadline (worker killed)
            bot_exception   bot code raised outside a handled callback
            crashed         exited with no fatal report: hard crash or external kill

        Windows reports no signal number for a terminated process, so an external
        kill and an unhandled hard crash are the same observation; the reason
        string says so instead of guessing.
        """
        if self._exit_code is None:
            self._exit_code = code  # this method owns "why is it gone, and how"
        derived = self._termination is None  # nobody else has claimed this exit
        if derived:
            if self._fatal is not None:
                self._termination = "bot_exception"
                self._termination_reason = self._fatal["error"]
            elif self._stopping:
                self._termination = "stopped"
                self._termination_reason = "worker stopped on request"
            else:
                self._termination = "crashed"
                self._termination_reason = (
                    f"worker exited with code {code} without reporting a fatal error "
                    "(unhandled hard crash or external kill)"
                )
        if not derived or self._termination == "stopped":
            return  # already reported where it was decided (timeout, boot failure)
        details = {"operation": "worker.exit",
                   "status": "worker_crash" if self._termination == "crashed"
                             else "bot_exception",
                   "termination": self._termination,
                   "reason": self._termination_reason,
                   "exit_code": code, "pid": self.worker_pid}
        if self._fatal is not None:
            details["traceback"] = self._fatal["traceback"]
            text = f"bot code took the worker down — {self._fatal['error']}"
        else:
            text = f"bot worker crashed (exit code {code})"
        self._log_server("💥", text, details)

    async def _handle_event(self, payload: dict) -> None:
        kind = payload.get("type")
        if kind == "state":
            self._apply_state(payload)
        elif kind == "log":
            icon = payload.get("icon") or "🖨️"
            self.session.log(icon, str(payload.get("text") or ""))
        elif kind == "fatal":
            self._fatal = {"error": str(payload.get("error") or "worker died"),
                           "traceback": str(payload.get("traceback") or "")}
        elif kind == "stopped" and not self._stopping:
            self._stopping = True

    def _apply_state(self, payload: dict) -> None:
        state = payload.get("state") or {}
        self._state = state
        self._status = payload.get("status") or {}
        if payload.get("inner_sandbox"):
            self._inner_sandbox = str(payload["inner_sandbox"])
        self.session._project_state = state
        self.session.commands = payload.get("commands") or {}
        seq = getattr(self.session, "_project_seq", 0) + 1
        self.session._project_seq = seq
        state["revision"] = seq
        if payload.get("last_run") is not None:
            self.session.last_run = payload["last_run"]
        if payload.get("error") and payload.get("req") is not None:
            future = self._pending.get(int(payload["req"]))
            if future is not None and not future.done():
                future.set_exception(RuntimeError(payload["error"]))
        req = payload.get("req")
        if req is not None:
            future = self._pending.pop(int(req), None)
            if future is not None and not future.done():
                future.set_result(payload)
        if payload.get("ok") and not self._booted.is_set():
            self._booted.set()
        self._mirrored = 0  # a fresh snapshot: re-append the server-side tail
        self._mirror_into_state()
        if self.on_exception is not None:
            self.on_exception()

    # -- request/response --------------------------------------------------

    async def _request(self, payload: dict, *, timeout: float = _OP_TIMEOUT,
                       boot: bool = False) -> dict:
        if self._closed or self.process is None or self.process.stdin is None:
            raise RuntimeError("bot worker is not running")
        self._req += 1
        payload = {**payload, "req": self._req}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._req] = future
        try:
            self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
        except (ConnectionError, RuntimeError) as error:
            self._pending.pop(self._req, None)
            raise ConnectionError("worker pipe closed") from error
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as error:
            self._pending.pop(self._req, None)
            if boot:
                raise WorkerTimeout(f"worker did not answer {payload.get('type')!r} in {timeout:.0f}s")
            self._kill(f"no response to {payload.get('type')!r} within {timeout:.0f}s (worker killed)")
            raise WorkerTimeout(
                f"bot stopped responding and the worker was terminated ({payload.get('type')})"
            ) from error
    def _kill(self, reason: str) -> None:
        self._stopping = True
        self._termination = "timeout"
        self._termination_reason = reason
        self._log_server("🛑", f"bot worker terminated — {reason}",
                         {"operation": "worker.kill", "status": "worker_timeout",
                          "reason": reason, "pid": self.worker_pid})
        process, self.process = self.process, None
        _terminate_process(process)
        if self.on_exception is not None:
            self.on_exception()

    def _cleanup_sandbox(self) -> None:
        shutil.rmtree(self.sandbox, ignore_errors=True)
        if self._inner_sandbox:  # the worker's inner copy may outlive a hard kill
            shutil.rmtree(self._inner_sandbox, ignore_errors=True)

    async def shutdown(self, *, keep_state: bool = False) -> None:
        """Stop the worker; keep_state preserves a failed boot's timeline for the UI."""
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is not None and process.returncode is None and not self._stopping:
            self._stopping = True
            if self._termination is None:
                self._termination = "stopped"
                self._termination_reason = "worker stopped on request"
            try:
                await asyncio.wait_for(self._request({"type": "shutdown"}), timeout=_SHUTDOWN_GRACE)
            except (WorkerTimeout, ConnectionError, RuntimeError, asyncio.TimeoutError):
                pass
        self.process = None
        if process is not None and process.returncode is None:
            _terminate_process(process)  # terminate(); kill is the backstop below
            with contextlib.suppress(asyncio.TimeoutError, BaseException):
                await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reader_task
        if not keep_state:
            self.session._project_state = None
        self._cleanup_sandbox()

    # -- state accessors + duck-typed dispatch surface ----------------------

    @property
    def state(self) -> dict:
        return self._state or {}

    def commands_payload(self) -> dict:
        return dict(self._state.get("commands") or {}) if self._state else {}

    def status(self) -> dict:
        status = dict(self._status)
        status.setdefault("ready", self._booted.is_set())
        status["worker"] = True
        status["worker_pid"] = self.worker_pid
        # Why the worker is gone - the UI must not have to guess between
        # "user pressed stop", "the bot crashed", "it never booted" and
        # "it was killed for not answering".
        status["termination"] = self._termination
        status["termination_reason"] = self._termination_reason
        status["exit_code"] = self._exit_code
        if self._exit_code is not None and self._exit_code < 0:  # POSIX only
            import signal as _signal

            status["signal"] = _signal.Signals(-self._exit_code).name
        if self._fatal is not None:
            status["traceback"] = self._fatal["traceback"]
        return status

    async def dispatch_message(self, content: str, channel_id=None) -> None:
        await self._request({"type": "op", "op": "dispatch_message",
                             "args": [content], "kwargs": {"channel_id": channel_id}})

    async def dispatch_command(self, name: str, args: dict, channel_id=None) -> None:
        await self._request({"type": "op", "op": "dispatch_command",
                             "args": [name, args or {}], "kwargs": {"channel_id": channel_id}})

    async def dispatch_click(self, message_id: str, custom_id: str, values: list) -> None:
        await self._request({"type": "op", "op": "dispatch_click",
                             "args": [message_id, custom_id, values or []]})

    async def dispatch_submit(self, message_id: str, custom_id: str, values: dict) -> None:
        await self._request({"type": "op", "op": "dispatch_submit",
                             "args": [message_id, custom_id, values or {}]})

    async def dispatch_pending_modal(self, values: dict, modal_id: str | None = None) -> bool:
        reply = await self._request({"type": "op", "op": "dispatch_pending_modal",
                                     "args": [values or {}], "kwargs": {"modal_id": modal_id}})
        return bool(reply.get("result"))

    async def dismiss_modal(self, modal_id: str) -> bool:
        reply = await self._request({"type": "op", "op": "dismiss_modal",
                                     "args": [modal_id]})
        return bool(reply.get("result"))

    async def session_op(self, method: str, args: list | None = None,
                         kwargs: dict | None = None, event: str | None = None) -> dict:
        """Run a UI-originated Session mutation inside the worker; reply snapshot.

        `event` names the gateway event the mutation implies ("reaction",
        "member_join", ...). The worker applies both halves in order, so the
        bot sees world-state-then-event like a real client does.
        """
        return await self._request({"type": "session", "method": method,
                                    "args": args or [], "kwargs": kwargs or {},
                                    "event": event})

    async def dispatch_event(self, kind: str, payload: dict | None = None) -> bool:
        """Deliver a simulated Discord event to the running bot (API/tests)."""
        reply = await self._request({"type": "op", "op": "dispatch_event",
                                     "args": [kind], "kwargs": {"payload": payload or {}}})
        return bool(reply.get("result"))

    def refresh_guild_state(self) -> None:  # parity no-op: worker ships full state
        return None

    def refresh_member_profile(self, _member=None) -> None:  # parity no-op
        return None


def sweep_orphan_sandboxes() -> dict:
    """Remove sandboxes whose recorded worker process is provably gone."""
    import bot_runtime

    return bot_runtime.sweep_orphan_sandboxes()


def _terminate_process(process: asyncio.subprocess.Process | None) -> None:
    """terminate(); the caller awaits process.wait() and escalates to kill()."""
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.terminate()


async def run_worker_project(session, workspace: Path, tag: str | None = None,
                             on_exception=None) -> WorkerProjectRuntime:
    """Create + boot a worker runtime; mirrors bot_runtime.run_project contract."""
    runtime = WorkerProjectRuntime(session, workspace, tag, on_exception)
    try:
        await runtime.boot()
    except asyncio.CancelledError:
        await runtime.shutdown(keep_state=True)
        raise
    except BaseException:
        await runtime.shutdown(keep_state=True)  # keep the failure visible in the UI
        raise
    session.commands = runtime.commands_payload()
    return runtime


if __name__ == "__main__":
    main()
