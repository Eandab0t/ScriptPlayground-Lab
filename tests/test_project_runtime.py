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
    """The asyncio.run redirect is scoped to boot and has an identity-guarded restore."""
    real = bot_runtime._ORIGINAL_ASYNCIO_RUN
    assert bot_runtime.asyncio.run is real
    bot_runtime._install_asyncio_run_patch()
    try:
        assert bot_runtime.asyncio.run is bot_runtime._runtime_asyncio_run_shim
        bot_runtime._install_asyncio_run_patch()
        assert bot_runtime.asyncio.run is bot_runtime._runtime_asyncio_run_shim
    finally:
        bot_runtime._restore_asyncio_run_patch()
    assert bot_runtime.asyncio.run is real

    def foreign():  # pragma: no cover - never called
        raise AssertionError

    bot_runtime.asyncio.run = foreign
    try:
        bot_runtime._restore_asyncio_run_patch()
        assert bot_runtime.asyncio.run is foreign
    finally:
        bot_runtime.asyncio.run = real


@pytest.mark.timeout(120)
def test_asyncio_run_restored_after_boot_failure():
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
def test_hosted_python_boot_cancellation_is_not_recorded_as_error():
    async def run():
        session = _session()
        project = Path(bot_runtime._SANDBOX_ROOT) / "pytest-boot-cancel"
        project.mkdir(parents=True, exist_ok=True)
        (project / "main.py").write_text(
            "import asyncio\n"
            "from pathlib import Path\n"
            "async def main():\n"
            "    Path('main-started').touch()\n"
            "    await asyncio.Event().wait()\n",
            encoding="utf-8",
        )
        tag = f"cancel-{id(session):x}"
        sandbox = Path(bot_runtime._SANDBOX_ROOT) / f"{project.name}-{tag}"
        errors = []
        try:
            boot = asyncio.create_task(bot_runtime.run_project(
                session, project, tag=tag, on_exception=lambda: errors.append(True)
            ))
            async with asyncio.timeout(5):
                while not (sandbox / "main-started").exists():
                    if boot.done():
                        await boot
                    await asyncio.sleep(0.005)
            boot.cancel()
            with pytest.raises(asyncio.CancelledError):
                await boot
            assert session.last_run is None and errors == []
            assert not any(event.get("cls") == "error" for event in session.events)
            assert not sandbox.exists() and str(sandbox) not in bot_runtime.sys.path
            assert bot_runtime.asyncio.run is bot_runtime._ORIGINAL_ASYNCIO_RUN
        finally:
            if not boot.done():
                boot.cancel()
            try:
                await boot
            except asyncio.CancelledError:
                pass
            session.close()

    asyncio.run(run())


@pytest.mark.timeout(120)
def test_hosted_python_system_exit_is_reported_and_sandbox_cleaned():
    async def run():
        session = _session()
        project = Path(bot_runtime._SANDBOX_ROOT) / "pytest-system-exit"
        project.mkdir(parents=True, exist_ok=True)
        (project / "main.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
        tag = f"exit-{id(session):x}"
        sandbox = Path(bot_runtime._SANDBOX_ROOT) / f"{project.name}-{tag}"
        try:
            with pytest.raises(RuntimeError, match="SystemExit: 7"):
                await bot_runtime.run_project(session, project, tag=tag)
            assert session.last_run["exception"]["type"] == "SystemExit"
            assert session.last_run["exception"]["file"] == "main.py"
            assert session.last_run["exception"]["line"] == 1
            assert not sandbox.exists()
            assert str(sandbox) not in bot_runtime.sys.path
            assert bot_runtime.asyncio.run is bot_runtime._ORIGINAL_ASYNCIO_RUN
        finally:
            session.close()

    asyncio.run(run())


@pytest.mark.timeout(120)
def test_hosted_callback_error_log_uses_sanitized_workspace_traceback(caplog):
    async def run():
        session = _session()
        project = Path(bot_runtime._SANDBOX_ROOT) / f"pytest-callback-log-{id(session):x}"
        project.mkdir(parents=True, exist_ok=True)
        (project / "main.py").write_text(
            "import discord\n"
            "from discord.ext import commands\n"
            "bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())\n"
            "@bot.tree.command(name='fail')\n"
            "async def fail(interaction):\n"
            "    raise RuntimeError('callback log failure')\n"
            "async def main():\n"
            "    async with bot:\n"
            "        await bot.start('offline-simulated-token')\n",
            encoding="utf-8",
        )
        sandbox = None
        runtime = None
        try:
            runtime = await asyncio.wait_for(bot_runtime.run_project(session, project), timeout=60)
            sandbox = runtime.sandbox
            before = len(caplog.records)
            await runtime.dispatch_command("fail", {})
            error = session.last_run["error"]
            rendered = "\n".join(record.getMessage() for record in caplog.records[before:]) + "\n".join(
                record.exc_text or "" for record in caplog.records[before:]
            )
            assert "callback log failure" in error
            assert "callback log failure" in rendered
            assert f"{project.name}/main.py" in error
            assert str(sandbox) not in error
            assert str(sandbox) not in rendered
            assert not any(str(sandbox) in str(event) for event in session.events)
        finally:
            if runtime is not None:
                await runtime.shutdown()
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
