"""Offline integration tests: real multi-file bot projects through ProjectRuntime.

Run: python -X utf8 -m pytest tests/test_project_runtime.py -x -q
No network is touched; live tokens are scrubbed before any code runs.
"""
import asyncio
from pathlib import Path

import pytest

import bot_runtime
import playground as pg

ROOT = Path(__file__).resolve().parent.parent
BOTS = ROOT / "bots"


def _session() -> pg.Session:
    return pg.Session("proj-test")


async def _boot(name: str) -> tuple[pg.Session, "bot_runtime.ProjectRuntime"]:
    session = _session()
    runtime = await asyncio.wait_for(
        bot_runtime.run_project(session, BOTS / name), timeout=90
    )
    return session, runtime


async def _noop_runtime() -> "bot_runtime.ProjectRuntime":
    """A tiny in-memory bot project: boots fast, exposes one zero-arg command."""
    session = _session()
    project = Path(bot_runtime._SANDBOX_ROOT) / "pytest-mini-bot"
    project.mkdir(parents=True, exist_ok=True)
    (project / "main.py").write_text(
        "import discord\n"
        "from discord.ext import commands\n"
        "bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())\n"
        "@bot.tree.command(name='ping')\n"
        "async def ping(interaction: discord.Interaction):\n"
        "    await interaction.response.send_message('pong!')\n"
        "async def main():\n"
        "    async with bot as b:\n"
        "        await b.start('offline-simulated-token')\n",
        encoding="utf-8",
    )
    runtime = await asyncio.wait_for(bot_runtime.run_project(session, project), timeout=60)
    return runtime


@pytest.mark.timeout(120)
def test_mini_bot_boot_and_command():
    async def run():
        runtime = await _noop_runtime()
        try:
            assert runtime.bot is not None
            assert runtime.bot.is_ready()
            assert "ping" in runtime.commands_payload()
            await runtime.dispatch_command("ping", {})
            reply = runtime.session.messages.get(runtime.session.order[-1])
            assert reply["content"] == "pong!"
            assert reply["author"]["bot"] is True
        finally:
            await runtime.shutdown()

    asyncio.run(run())


@pytest.mark.timeout(150)
def test_e_xpv6_boots_with_cogs_and_syncs():
    async def run():
        session, runtime = await _boot("E_XPV6")
        try:
            status = runtime.status()
            assert status["ready"] is True
            assert set(status["cogs"]) >= {"XP", "Admin", "Economy"}
            assert "wizard" in status["commands"]
            # user message flows into on_message listeners
            await runtime.dispatch_message("hello bot")
            assert any(m["author"]["name"] == "You" for m in session.messages.values())
        finally:
            await runtime.shutdown()
            session.close()

    asyncio.run(run())


@pytest.mark.timeout(150)
def test_jerkess_module_run_boot():
    async def run():
        session, runtime = await _boot("JERKESS")
        try:
            status = runtime.status()
            assert status["ready"] is True
            assert status["cogs"], "JERKESS should load its cogs"
            assert "gacha" in status["commands"]
            await runtime.dispatch_command("gacha", {})
            assert len(session.order) >= 1, "gacha should reply"
        finally:
            await runtime.shutdown()
            session.close()

    asyncio.run(run())


def test_sandbox_scrub_neutralizes_tokens():
    workspace = BOTS / "JERKESS"
    sandbox = bot_runtime._make_sandbox(workspace, "scrub-test")
    try:
        import re

        pattern = re.compile(r"[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}")
        for file in sandbox.rglob("*"):
            if not file.is_file():
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            assert not pattern.search(text), f"live token leaked in sandbox file {file.name}"
    finally:
        import shutil

        shutil.rmtree(sandbox, ignore_errors=True)


def test_wire_id_roundtrip():
    assert bot_runtime._wire_message_id("m5") == str(bot_runtime._WIRE_BASE + 5)
    assert bot_runtime._session_message_id(bot_runtime._wire_message_id("m12")) == "m12"
    assert bot_runtime._session_message_id("123456789012345678") is None
