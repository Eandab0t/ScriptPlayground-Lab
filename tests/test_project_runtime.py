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


def test_asyncio_run_patch_scoped_to_boot_and_restored_everywhere():
    """The asyncio.run redirect is installed only during a boot and restored on
    every exit path: clean boot, boot failure, and shutdown — with an
    identity guard so a stale runtime's shutdown cannot clobber the real one."""
    real = bot_runtime._ORIGINAL_ASYNCIO_RUN
    assert bot_runtime.asyncio.run is real  # untouched at import

    # Install/restore pair is idempotent and identity-guarded.
    bot_runtime._install_asyncio_run_patch()
    try:
        assert bot_runtime.asyncio.run is bot_runtime._runtime_asyncio_run_shim
        bot_runtime._install_asyncio_run_patch()  # double install: no-op
        assert bot_runtime.asyncio.run is bot_runtime._runtime_asyncio_run_shim
    finally:
        bot_runtime._restore_asyncio_run_patch()
    assert bot_runtime.asyncio.run is real

    # A restore when the shim is NOT active must not clobber a foreign value
    # (simulates out-of-order shutdown from a stale runtime).
    def foreign():  # pragma: no cover - never called
        raise AssertionError

    bot_runtime.asyncio.run = foreign
    try:
        bot_runtime._restore_asyncio_run_patch()
        assert bot_runtime.asyncio.run is foreign, "stale restore clobbered a foreign patch"
    finally:
        bot_runtime.asyncio.run = real
    assert bot_runtime.asyncio.run is real


@pytest.mark.timeout(120)
def test_asyncio_run_restored_after_boot_failure():
    """A project that raises during boot leaves the real asyncio.run in place."""
    async def run():
        session = _session()
        project = Path(bot_runtime._SANDBOX_ROOT) / "pytest-boot-fail"
        project.mkdir(parents=True, exist_ok=True)
        (project / "main.py").write_text("raise RuntimeError('boom during import')\n", encoding="utf-8")
        try:
            with pytest.raises(RuntimeError, match="boom during import"):
                await asyncio.wait_for(bot_runtime.run_project(session, project), timeout=60)
            assert bot_runtime.asyncio.run is bot_runtime._ORIGINAL_ASYNCIO_RUN
        finally:
            session.close()

    asyncio.run(run())


@pytest.mark.timeout(120)
def test_python_asyncio_run_entry_boots():
    """Entries using asyncio.run(bot.start(...)) work: the call is redirected
    onto the simulator's already-running loop instead of raising
    'Cannot run the event loop while another loop is running'."""
    async def run():
        session = _session()
        project = Path(bot_runtime._SANDBOX_ROOT) / "pytest-asyncio-run"
        project.mkdir(parents=True, exist_ok=True)
        (project / "main.py").write_text(
            "import asyncio\n"
            "import discord\n"
            "from discord.ext import commands\n"
            "intents = discord.Intents.default()\n"
            "bot = commands.Bot(command_prefix='!', intents=intents)\n"
            "@bot.tree.command(name='where')\n"
            "async def where(interaction):\n"
            "    await interaction.response.send_message('token-var=set')\n"
            "def main():\n"
            "    asyncio.run(bot.start('offline-simulated-token'))\n",
            encoding="utf-8",
        )
        try:
            runtime = await asyncio.wait_for(bot_runtime.run_project(session, project), timeout=90)
            try:
                assert runtime.bot is not None and runtime.bot.is_ready()
                await runtime.dispatch_command("where", {})
                reply = session.messages.get(session.order[-1])
                assert reply["content"] == "token-var=set"
            finally:
                await runtime.shutdown()
        finally:
            session.close()

    asyncio.run(run())


def _write_node_project(target: "Path", body: str) -> None:
    import json

    target.mkdir(parents=True, exist_ok=True)
    (target / "package.json").write_text(
        json.dumps({"name": "events-style-bot", "dependencies": {"discord.js": "^14"}}),
        encoding="utf-8",
    )
    (target / "index.js").write_text(body, encoding="utf-8")


@pytest.mark.timeout(120)
def test_node_events_constants_and_options_reach_handlers():
    """Bots written with Events.* constants + option getters dispatch correctly."""
    async def run():
        session = _session()
        project = Path(bot_runtime._SANDBOX_ROOT) / "pytest-node-events"
        _write_node_project(project, """
const { Client, GatewayIntentBits, SlashCommandBuilder, REST, Routes,
        Events } = require('discord.js');
const client = new Client({ intents: [GatewayIntentBits.Guilds] });
client.on(Events.ClientReady, async () => {
  const commands = [new SlashCommandBuilder()
    .setName('echo')
    .setDescription('say it back')
    .addStringOption(o => o.setName('text').setDescription('words').setRequired(true))
    ].map(c => c.toJSON());
  const rest = new REST({ version: '10' }).setToken('x');
  await rest.put(Routes.applicationCommands(client.user.id), { body: commands });
});
client.on(Events.InteractionCreate, async interaction => {
  if (!interaction.isChatInputCommand()) return;
  await interaction.reply(`echo: ${interaction.options.getString('text')}`);
});
client.login('offline-simulated-token');
""")
        try:
            runtime = await asyncio.wait_for(
                bot_runtime.run_project(session, project), timeout=90
            )
            assert "echo" in runtime.commands_payload()
            await runtime.dispatch_command("echo", {"text": "package check"})
            reply = session.messages.get(session.order[-1])
            assert reply["content"] == "echo: package check"
        finally:
            await runtime.shutdown()
            session.close()

    asyncio.run(run())
