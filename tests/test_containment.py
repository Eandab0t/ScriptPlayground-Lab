"""Containment: bot file writes (sqlite by relative path, lazily opened during
dispatch) must stay inside the runtime sandbox — never the app directory."""
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot_runtime  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent


def make_workspace(root: Path, name: str) -> Path:
    ws = root / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "main.py").write_text(
        "import discord\n"
        "from discord.ext import commands\n"
        "import sqlite3\n"
        "bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())\n"
        "tree = bot.tree\n"
        "@tree.command(name=\"write\")\n"
        "async def write(i: discord.Interaction):\n"
        "    con = sqlite3.connect('spill_test.db')\n"
        "    con.execute('CREATE TABLE IF NOT EXISTS t (x)')\n"
        "    con.execute('INSERT INTO t VALUES (1)')\n"
        "    con.commit(); con.close()\n"
        "    open('spill_test.log', 'w').write('hi')\n"
        "    await i.response.send_message('wrote files')\n"
        "@bot.event\n"
        "async def on_ready():\n"
        "    pass\n"
        "bot.run('x')\n"
    )
    return ws


async def main() -> None:
    import playground as pg

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("PASS" if cond else "FAIL"), label)
        if not cond:
            failures.append(label)

    session = pg.Session("containment")
    ws = make_workspace(APP_DIR / "tests" / ".tmp-containment", "ctw")
    try:
        runtime = await asyncio.wait_for(bot_runtime.run_project(session, ws), timeout=60)
        sandbox = runtime.sandbox
        app_cwd_before = Path.cwd()

        await runtime.dispatch_command("write", {})
        await asyncio.sleep(0.3)

        check((sandbox / "spill_test.db").is_file(), "sqlite db created inside sandbox")
        check((sandbox / "spill_test.log").is_file(), "log file created inside sandbox")
        check(not (APP_DIR / "spill_test.db").exists(), "no sqlite db in app dir")
        check(not (APP_DIR / "spill_test.log").exists(), "no log file in app dir")
        check(Path.cwd() == sandbox, "process CWD parked in sandbox during runtime life")

        con = sqlite3.connect(sandbox / "spill_test.db")
        rows = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        con.close()
        check(rows == 1, "db usable across dispatches (same file reused)")

        await runtime.shutdown()
        check(Path.cwd() == APP_DIR, "CWD restored to app dir after shutdown")

        # boot a second runtime after the first died: CWD must not sit in the
        # dead sandbox and the old spill files must not resurface in app dir.
        session2 = pg.Session("containment2")
        runtime2 = await asyncio.wait_for(bot_runtime.run_project(session2, ws), timeout=60)
        await runtime2.dispatch_command("write", {})
        await asyncio.sleep(0.3)
        check((runtime2.sandbox / "spill_test.db").is_file(), "second boot writes to its own sandbox")
        check(not (APP_DIR / "spill_test.db").exists(), "still no spill in app dir")
        await runtime2.shutdown()
        session2.close()
    finally:
        session.close()

    import shutil
    shutil.rmtree(ws.parent, ignore_errors=True)
    if failures:
        print(f"\n{len(failures)} FAILURES")
        sys.exit(1)
    print("\nall containment checks passed")


if __name__ == "__main__":
    asyncio.run(main())
