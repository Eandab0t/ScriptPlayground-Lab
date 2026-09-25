"""Launch ScriptPlayground with desktop-style browser and shutdown behavior."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import webbrowser
from pathlib import Path

from aiohttp import web

import main as server

log = logging.getLogger("playground.launcher")
_BROWSER_EXIT_GRACE = 5.0
_BROWSER_POLL_INTERVAL = 1.0


def _browser_command() -> list[str] | None:
    candidates = [
        os.getenv("SCRIPTPLAYGROUND_BROWSER"),
        shutil.which("msedge.exe") or shutil.which("msedge") or shutil.which("microsoft-edge"),
        shutil.which("chrome.exe") or shutil.which("chrome") or shutil.which("google-chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        str(Path.home() / "AppData/Local/Microsoft/Edge/Application/msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"),
    ]
    return next(([candidate] for candidate in candidates
                 if candidate and (Path(candidate).is_file() or shutil.which(candidate))), None)


def _launch_browser(url: str) -> subprocess.Popen | None:
    if os.getenv("SCRIPTPLAYGROUND_NO_BROWSER") == "1":
        log.info("Browser launch disabled by SCRIPTPLAYGROUND_NO_BROWSER")
        return None
    command = _browser_command()
    if command:
        try:
            profile = server.DATA_DIR / "browser-profile"
            profile.mkdir(parents=True, exist_ok=True)
            args = [
                *command, f"--app={url}", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check",
            ]
            process = subprocess.Popen(args, close_fds=True)
            log.info("Opened app mode with %s", Path(command[0]).name)
            return process
        except OSError:
            log.exception("Could not launch browser app mode; falling back to the default browser")
    log.info("Opening default browser (WebSocket close detection fallback)")
    webbrowser.open(url)
    return None


async def _wait_for_browser_exit(process: subprocess.Popen, stop: asyncio.Event) -> None:
    while process.poll() is None and not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_BROWSER_POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass
    if not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_BROWSER_EXIT_GRACE)
        except asyncio.TimeoutError:
            stop.set()


async def run(port: int, data_dir: Path | None = None) -> int:
    server.configure_data_directory(data_dir)
    server.bootstrap_default_scripts()
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    previous_handlers = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
            previous_handlers[sig] = None
        except (NotImplementedError, RuntimeError):
            try:
                previous = signal.getsignal(sig)
                signal.signal(sig, lambda _signum, _frame: loop.call_soon_threadsafe(stop.set))
                previous_handlers[sig] = previous
            except (OSError, ValueError):
                pass

    app = server.build_app(auto_shutdown=False)
    runner = web.AppRunner(app, access_log_class=server._AccessLogger)
    tasks = []
    browser_process = None
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        sockets = site._server.sockets if site._server else ()
        actual_port = sockets[0].getsockname()[1] if sockets else port
        url = f"http://127.0.0.1:{actual_port}/"
        log.info("ScriptPlayground data directory: %s", server.DATA_DIR)
        log.info("ScriptPlayground listening on %s", url)

        browser_process = await asyncio.to_thread(_launch_browser, url)
        tasks.append(asyncio.create_task(stop.wait()))
        if browser_process is not None:
            tasks.append(asyncio.create_task(_wait_for_browser_exit(browser_process, stop)))
        else:
            server.enable_auto_shutdown(app)
            tasks.append(asyncio.create_task(app[server._DESKTOP_SHUTDOWN_REQUESTED].wait()))
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return 0
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if browser_process is not None and browser_process.poll() is None:
            browser_process.terminate()
            try:
                await asyncio.to_thread(browser_process.wait, 5)
            except subprocess.TimeoutExpired:
                browser_process.kill()
                await asyncio.to_thread(browser_process.wait)
        await runner.cleanup()
        for sig, previous in previous_handlers.items():
            if previous is None:
                loop.remove_signal_handler(sig)
            else:
                signal.signal(sig, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch ScriptPlayground as a desktop app")
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--data-dir", type=Path, help="override the persistent user data directory")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(run(args.port, args.data_dir))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
