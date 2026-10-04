"""Acceptance tests: ordinary discord.py bots through the worker runtime, over HTTP.

Run: python -X utf8 -m pytest tests/test_acceptance.py -q
Boots the real aiohttp server on a private port and drives the same API the
browser uses: POST /run -> worker boot -> READY -> !ping -> Pong!.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"

BASIC_BOT = '''import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

bot.run("fake-token")
'''

CPU_BOT = '''import discord
from discord.ext import commands

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.event
async def on_ready():
    while True:
        pass

bot.run("fake-token")
'''

SLASH_BOT = '''import discord
from discord.ext import commands

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

bot = Bot()

@bot.tree.command(name="hello")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message("Hello!")

bot.run("fake-token")
'''

COG_BOT = '''import discord
from discord.ext import commands

class PingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        await ctx.send("Pong from cog")

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(PingCog(self))

bot = Bot()
bot.run("fake-token")
'''

BUTTON_BOT = '''import discord
from discord.ext import commands

class TestView(discord.ui.View):
    @discord.ui.button(label="Click me", custom_id="click_me")
    async def click_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Clicked!")

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

@bot.command()
async def menu(ctx):
    await ctx.send("Press this", view=TestView())

bot.run("fake-token")
'''

ASYNC_BOT = '''import asyncio

import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command(name="wait")
async def wait_cmd(ctx):
    await asyncio.sleep(0.2)
    await ctx.send("done")

bot.run("fake-token")
'''


def call(method, path, body=None, timeout=140):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001 - a non-JSON error body is still a valid failure
            return e.code, {}


def wait_for(sid, predicate, timeout=60, label="condition"):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        _, st = call("GET", f"/api/session/{sid}/state")
        last = st
        if predicate(st):
            return st
        time.sleep(0.4)
    raise AssertionError(f"timeout waiting for {label}; events tail: "
                         + json.dumps(last.get("events", [])[-4:], default=str)[:800])


def run_bot(sid, code):
    _, resp = call("POST", f"/api/session/{sid}/run", {"code": code})
    return resp


def ready_session(code, label="bot ready"):
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    resp = run_bot(sid, code)
    assert resp.get("ok"), f"run failed: {json.dumps(resp)[:500]}"
    assert resp.get("mode") == "project", resp
    wait_for(sid, lambda s: any("is ready" in (e.get("text") or "") for e in s.get("events", [])),
             timeout=30, label=label)
    return sid


def send_and_expect(sid, content, expect, timeout=25):
    st, resp = call("POST", f"/api/session/{sid}/message", {"content": content})
    assert st == 200, resp
    return wait_for(sid, lambda s: any(m.get("content") == expect for m in s.get("messages", [])),
                    timeout=timeout, label=f"reply {expect!r}")


@pytest.fixture(scope="session")
def server():
    env = dict(os.environ, SCRIPTPLAYGROUND_BOOT_TIMEOUT="15")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "main.py", "--port", str(PORT)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)  # drain access logs: a full pipe blocks the server
    deadline = time.time() + 45
    try:
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(BASE + "/", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001 - server not up yet
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not start")
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_basic_bot_ready_and_ping(server):
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    resp = run_bot(sid, BASIC_BOT)
    assert resp["ok"] and resp["mode"] == "project", resp
    status = resp["status"]
    assert status["ready"] is True and status["worker_pid"] > 0, status
    st = wait_for(sid, lambda s: any("is ready" in (e.get("text") or "") for e in s.get("events", [])),
                  label="ready event")
    # bot identity + world boot
    assert st["bot"]["name"], st["bot"]
    assert st["guild"] and st["channels"], st["guild"]
    # on_ready print landed in the timeline
    assert any("Logged in as" in (e.get("text") or "") for e in st["events"]), \
        [e["text"] for e in st["events"]][-6:]
    # simulated user sends !ping through real command processing
    st = send_and_expect(sid, "!ping", "Pong!")
    pong = next(m for m in st["messages"] if m["content"] == "Pong!")
    assert pong["author"]["bot"] is True
    assert any(m.get("content") == "!ping" for m in st["messages"])
    # no real Discord request was attempted (worker network blockade)
    assert not [e for e in st["events"] if "network request" in (e.get("text") or "")]


def test_restart_is_clean(server):
    sid = ready_session(BASIC_BOT, "first boot")
    st = send_and_expect(sid, "!ping", "Pong!")
    old_pid = [e for e in st["events"] if "is ready in worker process" in (e.get("text") or "")][-1]
    old_pid = int(old_pid["text"].rsplit(" ", 1)[-1])
    st, resp = call("POST", f"/api/session/{sid}/restart")
    assert st == 200 and resp.get("ok"), resp
    assert not [m for m in resp.get("messages", []) if m.get("content") == "Pong!"]
    # boot the same bot again: fresh worker, no stale commands/listeners/cogs
    resp = run_bot(sid, BASIC_BOT)
    assert resp["ok"], resp
    new_pid = resp["status"]["worker_pid"]
    assert new_pid != old_pid, (old_pid, new_pid)
    st = send_and_expect(sid, "!ping", "Pong!")
    pongs = [m for m in st["messages"] if m.get("content") == "Pong!"]
    assert len(pongs) == 1, f"expected exactly one fresh Pong!, got {len(pongs)}"


def _echo_bot(reply):
    return BASIC_BOT.replace('await ctx.send("Pong!")', f'await ctx.send("{reply}")')


@pytest.mark.timeout(180)
def test_two_sessions_are_isolated(server):
    sid_a = ready_session(_echo_bot("A-pong"), "bot A ready")
    sid_b = ready_session(_echo_bot("B-pong"), "bot B ready")
    assert sid_a != sid_b
    st_a = send_and_expect(sid_a, "!ping", "A-pong")
    st_b = send_and_expect(sid_b, "!ping", "B-pong")
    a_contents = [m.get("content") for m in st_a["messages"]]
    b_contents = [m.get("content") for m in st_b["messages"]]
    assert "A-pong" in a_contents and "B-pong" not in a_contents, a_contents
    assert "B-pong" in b_contents and "A-pong" not in b_contents, b_contents
    pid_a = int([e for e in st_a["events"]
                 if "is ready in worker process" in (e.get("text") or "")][-1]["text"].rsplit(" ", 1)[-1])
    pid_b = int([e for e in st_b["events"]
                 if "is ready in worker process" in (e.get("text") or "")][-1]["text"].rsplit(" ", 1)[-1])
    assert pid_a != pid_b, "each session must own its own worker process"


@pytest.mark.timeout(180)
def test_cpu_bound_worker_is_killed_and_session_recovers(server):
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    st, resp = call("POST", f"/api/session/{sid}/run", {"code": CPU_BOT}, timeout=120)
    assert not resp.get("ok"), f"CPU-bound bot must not boot: {json.dumps(resp)[:400]}"
    assert "terminated" in (resp.get("error") or "") or "ready" in (resp.get("error") or ""), resp
    _, st = call("GET", f"/api/session/{sid}/state")
    assert any((e.get("details") or {}).get("status") in
               ("worker_timeout", "worker_crash", "script_error")
               for e in st.get("events", [])), \
        [e.get("text") for e in st.get("events", [])][-5:]
    # the SERVER is still responsive and the same session can boot a healthy bot
    resp = run_bot(sid, BASIC_BOT)
    assert resp["ok"], resp
    wait_for(sid, lambda s: any("is ready" in (e.get("text") or "") for e in s.get("events", [])),
             label="healthy boot after kill")
    st = send_and_expect(sid, "!ping", "Pong!")
    assert [m for m in st["messages"] if m["content"] == "Pong!"]


@pytest.mark.timeout(180)
def test_async_command_callback(server):
    sid = ready_session(ASYNC_BOT, "async bot ready")
    st = send_and_expect(sid, "!wait", "done", timeout=30)
    done = next(m for m in st["messages"] if m["content"] == "done")
    assert done["author"]["bot"] is True


@pytest.mark.timeout(180)
def test_slash_command_registration_and_invocation(server):
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    resp = run_bot(sid, SLASH_BOT)
    assert resp["ok"], resp
    st = wait_for(sid, lambda s: "hello" in (s.get("commands") or {}),
                  timeout=30, label="slash command registered")
    assert st["commands"]["hello"]["name"] == "hello"
    st, resp = call("POST", f"/api/session/{sid}/command", {"name": "hello"})
    assert st == 200, resp
    st = wait_for(sid, lambda s: any(m.get("content") == "Hello!" for m in s.get("messages", [])),
                  timeout=25, label="interaction.response.send_message")
    hello = next(m for m in st["messages"] if m["content"] == "Hello!")
    assert hello["author"]["bot"] is True


@pytest.mark.timeout(180)
def test_cog_command_executes(server):
    resp_holder = {}
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    resp = run_bot(sid, COG_BOT)
    resp_holder["run"] = resp
    assert resp["ok"], resp
    assert "PingCog" in resp["status"].get("cogs", []), resp["status"]
    st = send_and_expect(sid, "!ping", "Pong from cog")
    assert [m for m in st["messages"] if m["content"] == "Pong from cog"]


@pytest.mark.timeout(180)
def test_view_button_callback_executes(server):
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    resp = run_bot(sid, BUTTON_BOT)
    assert resp["ok"], resp
    st = send_and_expect(sid, "!menu", "Press this")
    target = next(m for m in st["messages"] if m["content"] == "Press this")
    assert "click_me" in json.dumps(target), target
    st, resp = call("POST", f"/api/session/{sid}/click",
                    {"message_id": target["id"], "custom_id": "click_me", "values": []})
    assert st == 200, resp
    st = wait_for(sid, lambda s: any(m.get("content") == "Clicked!" for m in s.get("messages", [])),
                  timeout=25, label="button callback reply")
    clicked = next(m for m in st["messages"] if m["content"] == "Clicked!")
    assert clicked["author"]["bot"] is True


@pytest.mark.timeout(240)
def test_workspace_routing_keeps_script_mode(server):
    # a real discord.py bot.py workspace -> Discord Bot Mode (worker)
    ws = "acceptance-workspace"
    ws_dir = ROOT / "bots" / ws
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "bot.py").write_text(BASIC_BOT, encoding="utf-8")
    try:
        _, r = call("POST", "/api/session")
        sid = r["sid"]
        _, resp = call("POST", f"/api/session/{sid}/run", {"workspace": ws})
        assert resp.get("ok") and resp.get("mode") == "project", json.dumps(resp)[:400]
        wait_for(sid, lambda s: any("is ready" in (e.get("text") or "") for e in s.get("events", [])),
                 label="workspace bot ready")
        send_and_expect(sid, "!ping", "Pong!")
    finally:
        shutil.rmtree(ws_dir, ignore_errors=True)
    # the playground's mock-helper workspace keeps Script Mode
    _, r2 = call("POST", "/api/session")
    _, resp = call("POST", f"/api/session/{r2['sid']}/run", {"workspace": "demo"})
    assert resp.get("ok") and resp.get("mode") == "workspace", json.dumps(resp)[:300]


# ---------------------------------------------------------------- event bots

REACTION_BOT = '''import discord
from discord.ext import commands

# message_content is required for discord.py's message cache, which is what
# resolves the *constructed* on_reaction_add (raw events work without it).
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def _say(bot, text):
    for channel in bot.guilds[0].text_channels:
        await channel.send(text)
        return


@bot.event
async def on_raw_reaction_add(payload):
    print(f"raw add {payload.emoji.name} msg={payload.message_id}")
    await _say(bot, f"raw-add {payload.emoji.name}")


@bot.event
async def on_raw_reaction_remove(payload):
    print(f"raw remove {payload.emoji.name} msg={payload.message_id}")
    await _say(bot, f"raw-remove {payload.emoji.name}")


@bot.event
async def on_reaction_add(reaction, user):
    # Reaction.emoji is the plain name (str) in discord.py 2.x
    print(f"reaction add {reaction.emoji} count={reaction.count}")
    await _say(bot, f"reaction-add {reaction.emoji} by {user}")


@bot.command()
async def menu(ctx):
    await ctx.send("react here")


bot.run("fake-token")
'''

DELETE_BOT = '''import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_raw_message_delete(payload):
    cached = payload.cached_message
    print(f"raw delete cached={cached is not None}")
    for channel in bot.guilds[0].text_channels:
        await channel.send("deleted" if cached else "delete-unresolved")
        return


@bot.event
async def on_message_delete(message):
    print("cached delete", message.content)
    for channel in bot.guilds[0].text_channels:
        await channel.send("message_delete gone")
        return


@bot.command()
async def menu(ctx):
    await ctx.send("delete me")


bot.run("fake-token")
'''

MEMBER_BOT = '''import discord
from discord.ext import commands

# the privileged members intent is what keeps discord.py's member cache alive,
# without it GUILD_MEMBER_UPDATE/REMOVE are dropped exactly like the real library
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def _say(bot, text):
    for channel in bot.guilds[0].text_channels:
        await channel.send(text)
        return


@bot.event
async def on_member_join(member):
    print(f"join {member.id}")
    await _say(bot, f"joined {member.display_name}")


@bot.event
async def on_member_remove(member):
    print(f"leave {member.id}")
    await _say(bot, f"left {member.display_name}")


@bot.event
async def on_member_update(before, after):
    print(f"update {before.display_name} -> {after.display_name}")
    await _say(bot, f"renamed {before.display_name} to {after.display_name}")


@bot.event
async def on_voice_state_update(member, before, after):
    print(f"voice {member.id} {before.channel} -> {after.channel}")
    await _say(bot, f"voice {'in' if after.channel else 'out'}")


bot.run("fake-token")
'''


def _messages(st):
    return [m.get("content") for m in st.get("messages", [])]


def _text(st):
    """All message contents joined, for substring assertions."""
    return "\n".join(str(m) for m in _messages(st))


@pytest.mark.timeout(240)
def test_reaction_reaches_raw_and_constructed_bot_events(server):
    """UI reaction -> worker IPC -> fake gateway -> real discord.py handlers."""
    sid = ready_session(REACTION_BOT, "reaction bot ready")
    st = send_and_expect(sid, "!menu", "react here")
    target = next(m for m in st["messages"] if m["content"] == "react here")

    st, resp = call("POST", f"/api/session/{sid}/react",
                    {"message_id": target["id"], "emoji": "\N{FIRE}"})
    assert st == 200, resp
    assert resp.get("ok"), resp
    st = wait_for(sid, lambda s: "raw-add \N{FIRE}" in _text(s)
                  and "reaction-add \N{FIRE}" in _text(s),
                  label="on_raw_reaction_add + on_reaction_add replies")
    # the constructed (non-raw) event needs the bot's own message cache
    assert "reaction-add \N{FIRE}" in _text(st), _messages(st)
    # UI state and bot event state agree: exactly one reaction, one user
    st = call("GET", f"/api/session/{sid}/state")[1]
    message = next(m for m in st["messages"] if m["content"] == "react here")
    reactions = message.get("reactions") or []
    assert len(reactions) == 1, message
    assert len(reactions[0].get("users") or []) == 1, reactions

    # toggling again delivers the remove events
    st, resp = call("POST", f"/api/session/{sid}/react",
                    {"message_id": target["id"], "emoji": "\N{FIRE}"})
    assert st == 200 and resp.get("ok"), resp
    st = wait_for(sid, lambda s: "raw-remove \N{FIRE}" in _text(s),
                  label="on_raw_reaction_remove reply")
    st = call("GET", f"/api/session/{sid}/state")[1]
    message = next(m for m in st["messages"] if m["content"] == "react here")
    assert not (message.get("reactions") or []), message


@pytest.mark.timeout(240)
def test_raw_message_delete_resolves_cached_bot_message(server):
    sid = ready_session(DELETE_BOT, "delete bot ready")
    st = send_and_expect(sid, "!menu", "delete me")
    call("POST", f"/api/session/{sid}/events",
         {"kind": "message_delete",
          "payload": {"message_id": next(m["id"] for m in st["messages"]
                                          if m["content"] == "delete me")}})
    st = wait_for(sid, lambda s: "deleted" in _messages(s),
                  label="on_raw_message_delete reply")
    assert "delete-unresolved" not in _messages(st), _messages(st)


@pytest.mark.timeout(240)
def test_member_and_voice_events_reach_the_bot(server):
    sid = ready_session(MEMBER_BOT, "member bot ready")
    _, st = call("GET", f"/api/session/{sid}/state")
    channel_id = st["channels"][0]["id"]
    # a simulated member joins -> on_member_join
    st, resp = call("POST", f"/api/session/{sid}/members", {"username": "Newbie"})
    assert st == 200, resp
    st = wait_for(sid, lambda s: any(m.get("content") == "joined Newbie" for m in s.get("messages", [])),
                  label="on_member_join reply")
    joined = [e for e in st["events"] if "member_join" in (e.get("text") or "")]
    assert joined, [e.get("text") for e in st["events"]][-5:]

    # rename -> on_member_update with real before/after
    st = call("GET", f"/api/session/{sid}/state")[1]
    newcomer = next(m for m in st["member_details"] if m["name"] == "Newbie")
    member_id = str(newcomer["id"])
    st, resp = call("PUT", f"/api/session/{sid}/members/{member_id}/profile",
                    {"display_name": "Renamed"})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "renamed Newbie to Renamed" in _messages(s),
                  label="on_member_update reply")

    # voice join then leave -> on_voice_state_update
    st, resp = call("POST", f"/api/session/{sid}/voice",
                    {"action": "join", "channel_id": channel_id})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "voice in" in _messages(s), label="voice join event")
    st, resp = call("POST", f"/api/session/{sid}/voice", {"action": "leave"})
    assert st == 200, resp
    wait_for(sid, lambda s: "voice out" in _messages(s), label="voice leave event")

    # kick -> on_member_remove
    st, resp = call("POST", f"/api/session/{sid}/moderate/kick", {"user_id": member_id})
    assert st == 200, resp
    wait_for(sid, lambda s: "left Renamed" in _messages(s), label="on_member_remove reply")


@pytest.mark.timeout(300)
def test_three_bots_stay_isolated(server):
    sids, pids, replies, sandboxes = [], [], [], []
    for label in ("A", "B", "C"):
        sid = ready_session(_echo_bot(f"{label}-pong"), f"bot {label} ready")
        st = call("GET", f"/api/session/{sid}/state")[1]
        sids.append(sid)
        pids.append(int([e for e in st["events"]
                         if "is ready in worker process" in (e.get("text") or "")][-1]["text"]
                        .rsplit(" ", 1)[-1]))
        sandbox_text = next(e["text"] for e in st["events"]
                            if "sandbox " in (e.get("text") or ""))
        sandboxes.append(sandbox_text.split("sandbox ")[-1])
        replies.append(f"{label}-pong")
    assert len(set(pids)) == 3, pids
    assert len(set(sids)) == 3, sids
    for sid, reply, other in zip(sids, replies, ("B-pong", "C-pong", "A-pong"), strict=True):
        st = send_and_expect(sid, "!ping", reply)
        assert reply in _messages(st)
        assert other not in _messages(st), (reply, other, _messages(st))
    # each worker has its own sandbox directory
    assert len(set(sandboxes)) == 3, sandboxes


@pytest.mark.timeout(240)
def test_worker_crash_is_reported_and_the_session_recovers(server):
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    resp = run_bot(sid, BASIC_BOT)
    assert resp["ok"], resp
    pid = resp["status"]["worker_pid"]
    wait_for(sid, lambda s: any("is ready" in (e.get("text") or "") for e in s.get("events", [])),
             label="crash-test bot ready")
    assert pid > 0
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, timeout=30, check=False)
    st = wait_for(sid, lambda s: any("worker" in (e.get("text") or "").lower()
                                     and ("crash" in (e.get("text") or "").lower()
                                          or "exited" in (e.get("text") or "").lower())
                                     for e in s.get("events", [])),
                  timeout=30, label="worker crash event")
    crashed = [e for e in st["events"]
               if "crashed" in (e.get("text") or "").lower()
               or "exited with code" in (e.get("text") or "").lower()]
    assert crashed, [e.get("text") for e in st["events"]][-5:]
    details = (crashed[-1].get("details") or {})
    assert details.get("status") == "worker_crash", details
    assert details.get("exit_code") not in (None, 0), details
    # the server survived and the same session boots a fresh worker
    assert call("GET", f"/api/session/{sid}/state")[0] == 200
    resp = run_bot(sid, BASIC_BOT)
    assert resp["ok"], resp
    assert resp["status"]["worker_pid"] != pid
    send_and_expect(sid, "!ping", "Pong!")


@pytest.mark.timeout(300)
def test_multi_file_project_loads_extensions_and_restarts_clean(server):
    """bot.py + cogs/ + utils/ with normal imports and load_extension."""
    ws = "acceptance-multifile"
    ws_dir = ROOT / "bots" / ws
    (ws_dir / "cogs").mkdir(parents=True, exist_ok=True)
    (ws_dir / "utils").mkdir(parents=True, exist_ok=True)
    (ws_dir / "cogs" / "__init__.py").write_text("", encoding="utf-8")
    (ws_dir / "utils" / "__init__.py").write_text("", encoding="utf-8")
    (ws_dir / "utils" / "text.py").write_text('GREETING = "pong from utils"\n', encoding="utf-8")
    (ws_dir / "cogs" / "ping.py").write_text(
        "import discord\n"
        "from discord.ext import commands\n"
        "from utils.text import GREETING\n"
        "\n"
        "class Ping(commands.Cog):\n"
        "    @commands.command()\n"
        "    async def ping(self, ctx):\n"
        "        await ctx.send(GREETING)\n"
        "\n"
        "async def setup(bot):\n"
        "    await bot.add_cog(Ping())\n",
        encoding="utf-8")
    (ws_dir / "bot.py").write_text(
        "import asyncio\n"
        "import discord\n"
        "from discord.ext import commands\n"
        "\n"
        "async def main():\n"
        "    bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())\n"
        "    await bot.load_extension('cogs.ping')\n"
        "    await bot.start('fake-token')\n"
        "\n"
        "asyncio.run(main())\n",
        encoding="utf-8")
    try:
        _, r = call("POST", "/api/session")
        sid = r["sid"]
        _, resp = call("POST", f"/api/session/{sid}/run", {"workspace": ws}, timeout=140)
        assert resp.get("ok") and resp.get("mode") == "project", json.dumps(resp)[:400]
        assert "Ping" in resp.get("status", {}).get("cogs", []), resp.get("status")
        send_and_expect(sid, "!ping", "pong from utils")
        # restart the same project: no stale module state, exactly one cog copy
        _, resp = call("POST", f"/api/session/{sid}/run", {"workspace": ws}, timeout=140)
        assert resp.get("ok"), json.dumps(resp)[:300]
        assert "Ping" in resp.get("status", {}).get("cogs", []), resp.get("status")
        st = send_and_expect(sid, "!ping", "pong from utils")
        assert _messages(st).count("pong from utils") == 1, _messages(st)
    finally:
        shutil.rmtree(ws_dir, ignore_errors=True)


def test_orphan_sandbox_sweep_only_removes_dead_workers():
    """PID-safe cleanup: dead worker -> removed, live worker -> kept, no meta -> kept."""
    import bot_runtime

    root = Path(bot_runtime._SANDBOX_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    dead = root / "pytest-orphan-dead"
    live = root / "pytest-orphan-live"
    nometa = root / "pytest-orphan-nometa"
    for directory in (dead, live, nometa):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
    (dead / "bot.py").write_text("x", encoding="utf-8")
    (live / "bot.py").write_text("x", encoding="utf-8")
    (nometa / "bot.py").write_text("x", encoding="utf-8")
    bot_runtime.write_sandbox_meta(dead, session_id="s1", worker_pid=999_999, server_instance_id="x")
    bot_runtime.write_sandbox_meta(live, session_id="s2", worker_pid=os.getpid(), server_instance_id="x")
    try:
        report = bot_runtime.sweep_orphan_sandboxes()
        assert not dead.exists(), "a sandbox whose worker PID is dead must be removed"
        assert live.exists(), "a sandbox owned by a live worker must never be touched"
        assert nometa.exists(), "a sandbox without metadata must be left alone"
        assert live.name in report["kept"], report
        assert nometa.name in report["unknown"], report
    finally:
        for directory in (dead, live, nometa):
            shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------- third pass: lifecycle coverage

EDIT_BOT = '''import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def _say(text):
    for channel in bot.guilds[0].text_channels:
        await channel.send(text)
        return


@bot.event
async def on_raw_message_edit(payload):
    print("raw edit", payload.message_id)


@bot.event
async def on_message_edit(before, after):
    print("edited", before.content, "->", after.content)
    if after.content == "after":
        await _say(f"edited:{before.content}->{after.content}")


@bot.command()
async def say(ctx):
    message = await ctx.send("before")
    await message.edit(content="after")


bot.run("fake-token")
'''

SELECT_BOT = '''import discord
from discord.ext import commands

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


class PickView(discord.ui.View):
    @discord.ui.select(
        custom_id="pick_one",
        placeholder="Choose",
        options=[discord.SelectOption(label="One", value="one"),
                 discord.SelectOption(label="Two", value="two")],
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_message(f"selected:{select.values[0]}")


@bot.command()
async def menu(ctx):
    await ctx.send("pick one", view=PickView())


bot.run("fake-token")
'''

MODAL_BOT = '''import discord
from discord.ext import commands

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


class AskModal(discord.ui.Modal, title="Ask"):
    name = discord.ui.TextInput(label="Name", custom_id="name")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"modal:{self.name}")


class OpenView(discord.ui.View):
    @discord.ui.button(label="Open", custom_id="open_form")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AskModal())


@bot.command()
async def ask(ctx):
    await ctx.send("open the form", view=OpenView())


bot.run("fake-token")
'''

CHANNEL_ROLE_BOT = '''import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def _say(text):
    for channel in bot.guilds[0].text_channels:
        await channel.send(text)
        return


@bot.event
async def on_guild_channel_create(channel):
    print("channel create", channel.name)
    await _say(f"channel-created:{channel.name}")


@bot.event
async def on_guild_channel_delete(channel):
    print("channel delete", channel.name)
    await _say(f"channel-deleted:{channel.name}")


@bot.event
async def on_guild_role_create(role):
    print("role create", role.name)
    await _say(f"role-created:{role.name}")


@bot.event
async def on_guild_role_delete(role):
    print("role delete", role.name)
    await _say(f"role-deleted:{role.name}")


bot.run("fake-token")
'''

DOUBLE_BOT = '''import discord
from discord.ext import commands

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


class TwiceView(discord.ui.View):
    @discord.ui.button(label="twice", custom_id="twice")
    async def twice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("first")
        done = interaction.response.is_done()
        try:
            await interaction.response.send_message("second")
        except discord.InteractionResponded as error:
            await interaction.followup.send(f"already-done:{type(error).__name__}:{done}")


@bot.command()
async def menu(ctx):
    await ctx.send("press", view=TwiceView())


bot.run("fake-token")
'''

NET_BOT = '''import aiohttp
import discord
from discord.ext import commands

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())


@bot.command()
async def net(ctx):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://discord.com/api/v9/users/@me"):
                await ctx.send("NOT-blocked")
        except AssertionError:
            await ctx.send("network-blocked")


bot.run("fake-token")
'''


@pytest.mark.timeout(240)
def test_message_edit_emits_real_message_update(server):
    sid = ready_session(EDIT_BOT, "edit bot ready")
    st = send_and_expect(sid, "!say", "after")
    st = wait_for(sid, lambda s: "edited:before->after" in _text(s),
                  label="on_message_edit reply")
    message = next(m for m in st["messages"] if m["content"] == "after")
    assert not message.get("deleted"), message
    # the edit really went through the gateway parser, not a direct callback
    assert any("raw edit" in (e.get("text") or "") for e in st["events"]), \
        [e.get("text") for e in st["events"]][-6:]


@pytest.mark.timeout(240)
def test_user_delete_updates_world_and_fires_message_delete(server):
    sid = ready_session(DELETE_BOT, "delete bot ready")
    st = send_and_expect(sid, "!menu", "delete me")
    target = next(m for m in st["messages"] if m["content"] == "delete me")
    st, resp = call("POST", f"/api/session/{sid}/messages/delete",
                    {"message_id": target["id"]})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "deleted" in _text(s), label="on_raw_message_delete reply")
    assert "delete-unresolved" not in _text(st), _messages(st)
    # world + timeline agree: the message is marked deleted, the browser hides it
    assert not [m for m in st["messages"] if m["id"] == target["id"]], st["messages"]
    # the cached (non-raw) variant fires too when the cache allows it
    wait_for(sid, lambda s: "message_delete gone" in _text(s), label="on_message_delete reply")


@pytest.mark.timeout(240)
def test_select_interaction_executes_real_callback(server):
    sid = ready_session(SELECT_BOT, "select bot ready")
    st = send_and_expect(sid, "!menu", "pick one")
    target = next(m for m in st["messages"] if m["content"] == "pick one")
    assert "pick_one" in json.dumps(target), target
    st, resp = call("POST", f"/api/session/{sid}/click",
                    {"message_id": target["id"], "custom_id": "pick_one", "values": ["two"]})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "selected:two" in _text(s), label="select callback reply")
    assert "selected:one" not in _text(st), _messages(st)


@pytest.mark.timeout(240)
def test_modal_submit_executes_real_callback(server):
    sid = ready_session(MODAL_BOT, "modal bot ready")
    st = send_and_expect(sid, "!ask", "open the form")
    trigger = next(m for m in st["messages"] if m["content"] == "open the form")
    st, resp = call("POST", f"/api/session/{sid}/click",
                    {"message_id": trigger["id"], "custom_id": "open_form", "values": []})
    assert st == 200, resp
    st = wait_for(sid, lambda s: bool(s.get("modals")), label="modal opened")
    modal = st["modals"][-1]
    assert "name" in json.dumps(modal), modal
    st, resp = call("POST", f"/api/session/{sid}/submit",
                    {"modal_id": modal["id"], "values": {"name": "Zed"}})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "modal:Zed" in _text(s), label="modal callback reply")


@pytest.mark.timeout(300)
def test_channel_and_role_events_reach_the_bot(server):
    sid = ready_session(CHANNEL_ROLE_BOT, "channel/role bot ready")
    st, resp = call("POST", f"/api/session/{sid}/channels",
                    {"name": "lounge", "topic": "chill"})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "channel-created:lounge" in _text(s),
                  label="on_guild_channel_create reply")
    created = next(c for c in st["channels"] if c["name"] == "lounge")
    assert any("guild_channel_create" in (e.get("text") or "") for e in st["events"]), \
        [e.get("text") for e in st["events"]][-6:]
    st, resp = call("POST", f"/api/session/{sid}/channels/delete",
                    {"channel_id": created["id"]})
    assert st == 200, resp
    wait_for(sid, lambda s: "channel-deleted:lounge" in _text(s),
             label="on_guild_channel_delete reply")

    st, resp = call("POST", f"/api/session/{sid}/roles", {"op": "create", "name": "testers"})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "role-created:testers" in _text(s),
                  label="on_guild_role_create reply")
    role = next((r for r in st["roles"] if r["name"] == "testers"), None)
    assert role is not None, st["roles"]
    st, resp = call("POST", f"/api/session/{sid}/roles",
                    {"op": "delete", "role_id": role["id"]})
    assert st == 200, resp
    wait_for(sid, lambda s: "role-deleted:testers" in _text(s),
             label="on_guild_role_delete reply")


@pytest.mark.timeout(240)
def test_double_interaction_response_raises_like_discord(server):
    """discord.py's own error surfaces; the simulator does not silently accept it."""
    sid = ready_session(DOUBLE_BOT, "double bot ready")
    st = send_and_expect(sid, "!menu", "press")
    target = next(m for m in st["messages"] if m["content"] == "press")
    st, resp = call("POST", f"/api/session/{sid}/click",
                    {"message_id": target["id"], "custom_id": "twice", "values": []})
    assert st == 200, resp
    st = wait_for(sid, lambda s: "already-done:InteractionResponded" in _text(s),
                  label="InteractionResponded surfaced")
    assert "already-done:InteractionResponded:True" in _text(st), _messages(st)


@pytest.mark.timeout(300)
def test_repeated_runs_do_not_duplicate_commands_or_cogs(server):
    _, r = call("POST", "/api/session")
    sid = r["sid"]
    seen = []
    for _ in range(3):
        resp = run_bot(sid, COG_BOT)
        assert resp["ok"], resp
        assert resp["status"]["cogs"] == ["PingCog"], resp["status"]
        st = send_and_expect(sid, "!ping", "Pong from cog")
        pongs = [m for m in st["messages"] if m.get("content") == "Pong from cog"]
        assert len(pongs) == 1, f"duplicate reply: {len(pongs)}"
        seen.append(resp["status"]["worker_pid"])
    assert len(set(seen)) == 3, seen  # a fresh worker every time


@pytest.mark.timeout(300)
def test_one_sandbox_cannot_import_another(server):
    ws = "acceptance-isolation"
    ws_dir = ROOT / "bots" / ws
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "secret_module.py").write_text("VALUE = 'leaked'\n", encoding="utf-8")
    importer = ('import discord\nfrom discord.ext import commands\n\n'
                'import secret_module\n\n'
                'bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())\n'
                'bot.run("fake-token")\n')
    try:
        _, r = call("POST", "/api/session")
        owner = r["sid"]
        _, resp = call("POST", f"/api/session/{owner}/run", {"workspace": ws}, timeout=140)
        assert resp.get("ok"), json.dumps(resp)[:300]

        _, r = call("POST", "/api/session")
        other = r["sid"]
        resp = run_bot(other, importer)
        assert not resp.get("ok"), "a worker must not import another sandbox's modules"
        assert "ModuleNotFoundError" in json.dumps(resp), json.dumps(resp)[:400]
    finally:
        shutil.rmtree(ws_dir, ignore_errors=True)


@pytest.mark.timeout(240)
def test_real_discord_network_is_never_contacted(server):
    sid = ready_session(NET_BOT, "net bot ready")
    st = send_and_expect(sid, "!net", "network-blocked")
    assert "NOT-blocked" not in _text(st), _messages(st)
    violations = [e for e in st["events"]
                  if "network" in (e.get("text") or "").lower()]
    assert violations, "the blockade must record the attempt for the user"
