"""Offline smoke tests for the playground mock layer.

Run from the project root:

    python -m tests.test_smoke

Most checks use throwaway Session objects without network access; the OAuth checks use mocked provider calls, and the access-log regression starts a throwaway local aiohttp server. No external Discord credentials or network calls are used.
"""

import asyncio
import os
import pathlib
import subprocess
import sys
import tempfile
from pathlib import Path

import discord

import playground
from playground import (
    Session,
    dispatch_click,
    dispatch_command,
    dispatch_event,
    dispatch_message,
    dispatch_submit,
    reload_script,
    run_script,
    state,
)

DEMO = r"""
import discord

async def main():
    e = discord.Embed(title="T", description="D", color=0xff0000)
    e.add_field(name="N", value="V", inline=True)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Go", custom_id="go", style=discord.ButtonStyle.success))
    sel = discord.ui.Select(placeholder="pick", custom_id="pick",
                            options=[discord.SelectOption(label="A", value="a")])
    view.add_item(sel)
    await send(content="hello", embed=e, view=view)

async def on_click(interaction, custom_id, values):
    if custom_id == "go":
        await interaction.response.send_message("clicked!", ephemeral=True)
    elif custom_id == "pick":
        await interaction.response.edit_message(content=f"picked {values}")

async def on_submit(interaction, values, modal_id):
    await interaction.response.send_message(f"got {values}")
"""


async def new_session_with(code: str) -> Session:
    s = Session("test")
    await run_script(s, code)
    return s


def last_msg(s: Session):
    return s.messages[s.order[-1]] if s.order else None


def _write_bot_workspace(root, name, bot_py):
    """Create a minimal bots/<name>/ with a bot.py entry under a temp root."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bot.py").write_text(bot_py, encoding="utf-8")
    return folder


async def test_run_captures_send():
    s = await new_session_with(DEMO)
    m = last_msg(s)
    assert m is not None, "no message captured"
    assert m["content"] == "hello"
    assert m["embeds"][0]["title"] == "T"
    assert m["embeds"][0]["color"] == 0xFF0000
    kinds = [c["kind"] for c in m["components"]]
    assert kinds == ["button", "select"], kinds
    btn = m["components"][0]
    assert btn["label"] == "Go" and btn["style"] == 3 and btn["row"] == 0


async def test_click_button_ephemeral_reply():
    s = await new_session_with(DEMO)
    mid = s.order[-1]
    await dispatch_click(s, mid, "go", [])
    m = last_msg(s)
    assert m["content"] == "clicked!"
    assert m["ephemeral"] is True
    await dispatch_click(s, mid, "pick", ["a"])
    edited = s.messages[mid]
    assert edited["revision"] >= 1 and "picked" in edited["content"]


async def test_modal_roundtrip():
    s2 = Session("test2")
    await run_script(s2, """
import discord
stashed = {}
async def main():
    m = discord.ui.Modal(title="Form")
    m.add_item(discord.ui.TextInput(label="Name", required=True, default="Anon"))
    m.add_item(discord.ui.TextInput(label="Bio", style=discord.TextStyle.paragraph, max_length=50))
    stashed['m'] = m
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="Open", custom_id="open"))
    await send(view=v)
async def on_click(interaction, custom_id, values):
    await interaction.response.send_modal(stashed['m'])
async def on_submit(interaction, values, modal_id):
    await interaction.response.send_message("submitted " + repr(sorted(values)))
""")
    mid2 = s2.order[-1]
    await dispatch_click(s2, mid2, "open", [])
    assert s2.modals, "modal was not opened"
    modal = s2.modals[0]
    assert modal["title"] == "Form"
    fields = modal["items"]
    assert fields[0]["label"] == "Name" and fields[0]["value"] == "Anon"
    assert fields[1]["max_length"] == 50
    await dispatch_submit(s2, modal["id"], {fields[0]["custom_id"]: "Bob", fields[1]["custom_id"]: "hey"})
    m = last_msg(s2)
    assert m["content"].startswith("submitted"), m["content"]
    assert not s2.modals, "modal should be consumed after submit"


async def test_active_user_and_interaction_metadata():
    s = Session("t-users")
    await run_script(s, """
import discord
from discord import app_commands
modal = discord.ui.Modal(title="Form")
modal.add_item(discord.ui.TextInput(label="Name"))

@app_commands.command(name="who")
async def who(interaction):
    await interaction.response.send_message(f"command:{interaction.user.name}:{interaction.type.name}")

async def main():
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open", custom_id="open"))
    await send(view=view)

async def on_click(interaction, custom_id, values):
    modal.title = f"component:{interaction.type.name}"
    await interaction.response.send_modal(modal)

async def on_message(message):
    await message.channel.send(f"author:{message.author.name}")

async def on_submit(interaction, values, modal_id):
    await interaction.response.send_message(f"modal:{interaction.user.name}:{interaction.type.name}")
""")
    assert s.active_user.name == "You"
    assert s.build_interaction().user.name == "You"
    s.set_user(111111111111111111)
    assert s.active_user.name == "Alice"
    mid = s.order[-1]
    await dispatch_click(s, mid, "open", [])
    assert s.modals[0]["title"] == "component:component"
    await dispatch_submit(s, s.modals[0]["id"], {"name": "ok"})
    await dispatch_command(s, "who", {})
    await dispatch_message(s, "hello")
    contents = [s.messages[mid]["content"] for mid in s.order]
    assert "modal:Alice:modal_submit" in contents
    assert "command:Alice:application_command" in contents
    assert s.messages[s.order[-2]]["author"]["name"] == "Alice"


async def test_permissions_and_interaction_permissions():
    s = Session("t-permissions")
    alice = s.guild.get_member(111111111111111111)
    role = s.guild.roles[-1]
    role.permissions.update(manage_messages=True)
    alice.roles = [s.guild.roles[0], role]
    assert alice.guild_permissions.manage_messages

    channel = s.channel
    channel.overwrites[s.guild.id] = discord.PermissionOverwrite(send_messages=False)
    channel.overwrites[role.id] = discord.PermissionOverwrite(send_messages=True)
    assert channel.permissions_for(alice).send_messages
    channel.overwrites[alice.id] = discord.PermissionOverwrite(send_messages=False)
    assert not channel.permissions_for(alice).send_messages
    allowed, reason = channel.permission_check(alice, "send_messages")
    assert not allowed and reason == "member overwrite"
    try:
        await channel.send("blocked", author=alice)
    except discord.Forbidden:
        pass
    else:
        raise AssertionError("member send should require send_messages")

    s.set_user(alice.id)
    interaction = s.build_interaction()
    assert interaction.permissions == channel.permissions_for(alice)
    assert interaction.app_permissions == channel.permissions_for(s.guild.me)

    owner = s.guild.get_member(s.guild.owner_id)
    assert channel.permissions_for(owner).administrator
    s.guild.me.roles = [s.guild.roles[0]]
    s.guild.me.permission_override = discord.Permissions(view_channel=True)
    assert not channel.permissions_for(s.guild.me).send_messages
    try:
        await channel.send("blocked")
    except discord.Forbidden as error:
        assert "@everyone overwrite" in str(error)
    else:
        raise AssertionError("bot send should require send_messages")
    blocked = s.build_interaction()
    for sender in (
        lambda: blocked.response.send_message("blocked response"),
        lambda: blocked.followup.send("blocked followup"),
    ):
        try:
            await sender()
        except discord.Forbidden:
            pass
        else:
            raise AssertionError("interaction send should require send_messages")

    s.guild.me.permission_override = None
    allowed = s.build_interaction()
    await allowed.response.send_message("allowed response")
    await allowed.followup.send("allowed followup")
    assert [s.messages[mid]["content"] for mid in s.order[-2:]] == [
        "allowed response", "allowed followup"
    ]


async def test_active_user_http_selection():
    import json

    import main as server

    s = Session("t-user-http")
    server.SESSIONS[s.sid] = s
    try:
        response = await server.set_user(_FakeReq(match={"sid": s.sid}, body={"user_id": 222222222222222222}))
        assert json.loads(response.body)["user"] == {"id": "222222222222222222", "name": "Bob"}
        response = await server.set_user(_FakeReq(match={"sid": s.sid}, body={"user_id": 987}))
        assert response.status == 400
        assert server.state(s)["user"] == {"id": "222222222222222222", "name": "Bob"}
    finally:
        server.SESSIONS.pop(s.sid, None)
        s.close()


async def test_print_and_send_helpers():
    s = Session("t3")
    await run_script(s, "print('to console')\nasync def main():\n    await send('via helper')")
    assert any("to console" in e["text"] for e in s.events)
    assert last_msg(s)["content"] == "via helper"


async def test_discord_desktop_shell_contract():
    html = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    assert 'addEventListener("beforeunload", flushLocalState)' in html
    assert 'addEventListener("visibilitychange"' in html and 'document.visibilityState === "hidden"' in html
    assert 'function flushLocalState()' in html and 'localStorage.setItem("pg-code", $("#code").value)' in html
    assert 'const design = localStorage.getItem("discord-embeder:autosave:v1")' in html
    assert 'localStorage.setItem("discord-embeder:autosave:v1", design)' in html
    for token in ("--background-primary", "--background-secondary", "--background-tertiary", "--brand-primary", "--text-normal"):
        assert token in html
    for element in ("server-rail", "Local simulation", "sim-context", "Act as user", "density-select", "message-display", "account-link", "/auth/discord/login", "member-list", "head-topic", "jump-present"):
        assert element in html
    for token in ("--motion-fast: 100ms", "--motion-normal: 150ms", "--motion-slow: 250ms", "--ease-standard", "--ease-decelerate"):
        assert token in html
    assert "localStorage.getItem(\"pg-density\")" in html
    assert "messageRows = new Map()" in html and "messageFingerprint" in html
    assert "prefers-reduced-motion: reduce" in html
    assert "@media (max-width: 980px)" in html


async def test_events_and_actions_are_structured():
    s = Session("t-inspect")
    await run_script(s, "async def main():\n    await send('inspect me')")
    actions = [event for event in s.events if event["kind"] == "action"]
    assert actions and actions[-1]["details"]["operation"] == "channel.send"
    assert actions[-1]["details"]["message_id"] == s.order[-1]
    s.log("🧪", "test event", details={"source": "smoke"})
    assert s.events[-1]["kind"] == "event"
    assert s.events[-1]["details"] == {"source": "smoke"}
    s.close()


async def test_state_exposes_permission_inspector_data():
    s = Session("t-permission-inspector")
    s.set_user(111111111111111111)
    s.channel.overwrites[s.guild.id] = discord.PermissionOverwrite(send_messages=False)
    snapshot = state(s)
    assert snapshot["permissions"]["user"]["send_messages"] == {
        "allowed": False, "reason": "@everyone overwrite"
    }
    assert snapshot["permissions"]["bot"]["send_messages"]["allowed"] is True
    s.close()


async def test_inspector_records_failed_attempts_and_channel_actions():
    s = Session("t-inspect-failures")
    await run_script(s, "async def main():\n    pass")
    await dispatch_command(s, "missing", {})
    missing = s.events[-1]
    assert missing["kind"] == "action"
    assert missing["details"] == {"operation": "interaction.command", "command": "missing",
                                    "arguments": {}, "status": "missing_command"}

    s.set_user(111111111111111111)
    s.active_user.permission_override = discord.Permissions(view_channel=True)
    await dispatch_message(s, "blocked")
    denied = s.events[-1]
    assert denied["kind"] == "event" and denied["details"]["status"] == "denied"
    assert denied["details"]["operation"] == "message.send"

    channel = s.make_channel("temporary")
    await channel.delete()
    deleted = s.events[-1]
    assert deleted["kind"] == "action"
    assert deleted["details"]["operation"] == "channel.delete"
    assert deleted["details"]["status"] == "ok"
    s.close()


async def test_error_in_main_is_captured():
    s = Session("t4")
    res = await run_script(s, "async def main():\n    raise ValueError('boom')")
    assert res["ok"] is False
    assert any("ValueError" in e["text"] and e.get("cls") == "error" for e in s.events)

async def test_missing_handler_warns():
    s = Session("t-missing")
    await run_script(s, """
async def main():
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="Lonely", custom_id="lonely"))
    await send(view=v)
""")  # no on_click defined at all
    before = len(s.events)
    await dispatch_click(s, s.order[-1], "lonely", [])
    assert len(s.events) > before
    failure = next(e for e in reversed(s.events) if "no `on_click`" in e["text"])
    assert failure["kind"] == "event"
    assert failure["details"]["status"] == "missing_handler"


async def test_unanswered_interaction_warns():
    s = Session("t-unanswered")
    await run_script(s, """
async def main():
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="Silent", custom_id="silent"))
    await send(view=v)
async def on_click(interaction, custom_id, values):
    pass  # oops: never answers the interaction
""")
    await dispatch_click(s, s.order[-1], "silent", [])
    failure = next(e for e in reversed(s.events) if "never answered" in e["text"])
    assert failure["kind"] == "event"
    assert failure["details"]["status"] == "unanswered"


# --- slash commands -----------------------------------------------------------


CMD_SCRIPT = """
import discord
from discord import app_commands

@app_commands.command(name="echo", description="Echo text")
async def echo(interaction: discord.Interaction, text: str, shout: bool = False):
    await interaction.response.send_message((text.upper() if shout else text))

@app_commands.command(name="pick", description="Pick one")
@app_commands.choices(size=[app_commands.Choice(name="Small", value=1),
                            app_commands.Choice(name="Big", value=2)])
async def pick(interaction: discord.Interaction, size: app_commands.Choice[int]):
    await interaction.response.send_message(f"{size.name}={size.value}")

@app_commands.command(name="who", description="Greet someone")
async def who(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.send_message(f"hi {member.name if member else 'all'}")

@app_commands.command(name="need", description="Has a required arg")
async def need(interaction: discord.Interaction, must: str):
    await interaction.response.send_message(must)
"""


async def test_commands_collected_and_serialized():
    s = Session("t-cmds")
    await run_script(s, CMD_SCRIPT)
    assert set(s.commands) == {"echo", "pick", "who", "need"}
    echo = s.commands["echo"]
    assert echo["params"][0]["name"] == "text" and echo["params"][0]["type"] == "string"
    assert echo["params"][1]["type"] == "boolean"
    pick = s.commands["pick"]
    assert pick["params"][0]["choices"] == [("Small", 1), ("Big", 2)]
    assert pick["params"][0]["type"] == "integer"


async def test_command_dispatch_and_coercion():
    s = Session("t-cmds2")
    await run_script(s, CMD_SCRIPT)
    await dispatch_command(s, "echo", {"text": "hello", "shout": "true"})
    assert last_msg(s)["content"] == "HELLO"
    assert last_msg(s)["command"] == "echo"  # the slash chip
    await dispatch_command(s, "pick", {"size": "2"})
    assert last_msg(s)["content"] == "Big=2"  # Choice delivered, not the raw "2"
    await dispatch_command(s, "who", {"member": "111111111111111111"})
    assert last_msg(s)["content"] == "hi Alice"


async def test_command_defaults_and_missing_required():
    s = Session("t-cmds3")
    await run_script(s, CMD_SCRIPT)
    await dispatch_command(s, "echo", {"text": "quiet one"})  # shout falls back to default
    assert last_msg(s)["content"] == "quiet one"
    before = len(s.events)
    await dispatch_command(s, "need", {})  # required arg missing
    assert any("must" in e["text"] for e in s.events[before:])
    assert not s.order or last_msg(s)["command"] != "need"


async def test_unknown_command_and_reset_on_rerun():
    s = Session("t-cmds4")
    await run_script(s, CMD_SCRIPT)
    before = len(s.events)
    await dispatch_command(s, "nope", {})
    failure = next(e for e in s.events[before:] if "not defined" in e["text"])
    assert failure["kind"] == "action"
    assert failure["details"]["status"] == "missing_command"
    await run_script(s, "async def main():\n    pass")  # no commands in this script
    assert s.commands == {} and s.cmd_objects == {}


# --- live reload (timeline-preserving re-exec) ----------------------------------


RELOAD_BASE = """
import discord
async def main():
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="Go", custom_id="go"))
    await send(view=v)
"""


async def test_reload_replaces_env_without_clearing_timeline():
    s = Session("t-reload-timeline")
    try:
        await run_script(s, RELOAD_BASE)
        assert len(s.order) == 1 and s.env is not None
        result = await reload_script(s, RELOAD_BASE)
        assert result["ok"] is True
        assert len(s.order) == 1  # the welcome message is untouched
        assert len(s.channels) >= 1  # channels survive too
        assert s.env is not None
        assert next(e for e in reversed(s.events) if "reloaded" in e["text"])
    finally:
        s.close()


async def test_reload_preserves_custom_id_routing_to_new_handler():
    s = Session("t-reload-routing")
    try:
        await run_script(s, RELOAD_BASE + '''
async def on_click(interaction, custom_id, values):
    await interaction.response.send_message("old handler", ephemeral=True)
''')
        mid = s.order[-1]
        await dispatch_click(s, mid, "go", [])
        assert last_msg(s)["content"] == "old handler"
        await reload_script(s, RELOAD_BASE + '''
async def on_click(interaction, custom_id, values):
    await interaction.response.send_message("new handler", ephemeral=True)
''')
        assert mid in s.messages  # the ORIGINAL message still exists
        await dispatch_click(s, mid, "go", [])
        assert last_msg(s)["content"] == "new handler"
        assert len(s.order) == 3  # welcome + old reply + new reply
    finally:
        s.close()


async def test_reload_recalls_commands():
    s = Session("t-reload-commands")
    try:
        await run_script(s, "async def main():\n    pass")
        await reload_script(s, CMD_SCRIPT)
        assert set(s.commands) == {"echo", "pick", "who", "need"}
        assert set(s.cmd_objects) == {"echo", "pick", "who", "need"}
        await dispatch_command(s, "echo", {"text": "fresh"})
        assert last_msg(s)["content"] == "fresh"
    finally:
        s.close()


async def test_reload_syntax_error_preserves_previous_env():
    s = Session("t-reload-broken")
    try:
        await run_script(s, RELOAD_BASE)
        previous_env = s.env
        assert previous_env is not None
        before = len(s.order)
        result = await reload_script(s, "def broken(:\n")
        assert result["ok"] is False
        assert s.env is previous_env  # the old module keeps running
        assert len(s.order) == before  # timeline untouched
        assert any(e.get("cls") == "error" for e in s.events)
        assert s.last_run["exception"]["type"] == "SyntaxError"
    finally:
        s.close()


async def test_reload_does_not_run_main_unless_asked():
    s = Session("t-reload-runmain")
    try:
        await run_script(s, RELOAD_BASE)
        before = len(s.order)
        await reload_script(s, RELOAD_BASE)
        assert len(s.order) == before  # main() did not re-run
        await reload_script(s, RELOAD_BASE, run_main=True)
        assert len(s.order) == before + 1  # main() booted again on the new module
    finally:
        s.close()


async def test_run_after_reload_still_clears_timeline():
    s = Session("t-reload-then-run")
    try:
        await run_script(s, RELOAD_BASE)
        await reload_script(s, RELOAD_BASE)
        assert len(s.order) == 1
        await run_script(s, "async def main():\n    await send('fresh run')\n")
        assert [s.messages[mid]["content"] for mid in s.order] == ["fresh run"]
        assert s.commands == {}
    finally:
        s.close()


async def test_reload_route_returns_state_and_refuses_hosted_projects():
    import json

    import main as server

    s = Session("t-reload-route")
    server.SESSIONS[s.sid] = s
    try:
        await run_script(s, RELOAD_BASE)
        response = await server.reload_code(_FakeReq(
            match={"sid": s.sid}, body={"code": RELOAD_BASE, "run_main": False}
        ))
        data = json.loads(response.body)
        assert data["ok"] is True
        assert data["state"]["revision"] == s.revision
        assert len(data["state"]["messages"]) == 1

        empty = await server.reload_code(_FakeReq(match={"sid": s.sid}, body={"code": "   "}))
        assert empty.status == 400

        server.RUNTIMES[s.sid] = object()  # a hosted project owns this session
        try:
            hosted = await server.reload_code(_FakeReq(
                match={"sid": s.sid}, body={"code": RELOAD_BASE}
            ))
            assert hosted.status == 409
        finally:
            server.RUNTIMES.pop(s.sid, None)
    finally:
        server.SESSIONS.pop(s.sid, None)
        s.close()


# --- Slice 1: multi-file bot workspaces ----------------------------------------


async def test_workspace_two_files_import_and_dispatch():
    """bot.py imports helpers.py; the command sends the helper's result."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        folder = _write_bot_workspace(pathlib.Path(td), "two_file_bot", '''
import discord
from discord import app_commands
from helpers import greet

@app_commands.command(name="greet_user")
async def greet_user(interaction):
    await interaction.response.send_message(greet("Alice"))

async def main():
    pass
''')
        (folder / "helpers.py").write_text("def greet(name):\n    return f'Hello, {name} from helpers!'\n", encoding="utf-8")
        s = Session("t-ws-two-files")
        try:
            result = await run_script(s, "", workspace="two_file_bot", workspace_root=folder)
            assert result["ok"] is True, s.last_run
            assert s.workspace == "two_file_bot"
            assert "greet_user" in s.commands
            await dispatch_command(s, "greet_user", {})
            assert last_msg(s)["content"] == "Hello, Alice from helpers!"
        finally:
            s.close()


async def test_workspace_relative_import_resolves():
    """`from .helpers import greet` works via the workspace finder."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        folder = _write_bot_workspace(pathlib.Path(td), "relative_import_bot", '''
from .helpers import greet

async def main():
    await send(greet("Bob"))
''')
        (folder / "helpers.py").write_text("def greet(name):\n    return f'hi {name}'\n", encoding="utf-8")
        s = Session("t-ws-relative")
        try:
            result = await run_script(s, "", workspace="relative_import_bot", workspace_root=folder)
            assert result["ok"] is True, s.last_run
            assert last_msg(s)["content"] == "hi Bob"
        finally:
            s.close()


async def test_workspace_dunder_file_points_inside_workspace():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        folder = _write_bot_workspace(pathlib.Path(td), "dunder_file_bot", '''
async def main():
    await send(__file__)
''')
        s = Session("t-ws-dunder")
        try:
            result = await run_script(s, "", workspace="dunder_file_bot", workspace_root=folder)
            assert result["ok"] is True, s.last_run
            sent = last_msg(s)["content"]
            assert pathlib.Path(sent).resolve() == (folder / "bot.py").resolve()
        finally:
            s.close()


async def test_workspace_manifest_and_traversal_are_rejected():
    """workspace.json allowlist + `..` import attempts fail with clear errors."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        folder = _write_bot_workspace(root, "manifest_bot", '''
import secret_helper

async def main():
    pass
''')
        (folder / "secret_helper.py").write_text("MARKER = 'outside allowlist'\n", encoding="utf-8")
        (folder / "workspace.json").write_text('{"files": ["bot.py"]}\n', encoding="utf-8")
        outside = root / "outside.py"
        outside.write_text("OUTSIDE_MARKER = 'this must never be read'\n", encoding="utf-8")
        s = Session("t-ws-manifest")
        try:
            await run_script(s, "", workspace="manifest_bot", workspace_root=folder)
            assert s.last_run["ok"] is False
            assert "allowlist" in s.last_run["exception"]["message"]
            assert "outside.py" not in s.last_run["error"]
            assert outside.read_text(encoding="utf-8") == "OUTSIDE_MARKER = 'this must never be read'\n"
            assert "OUTSIDE_MARKER" not in sys.modules

            # a symlink that points outside the workspace is rejected on import
            # (the `..`-equivalent escape vector that actually reaches the finder:
            # a plain `from ..outside import` fails inside CPython before any
            # filesystem probe, so the symlink is the real traversable path)
            escape = _write_bot_workspace(root, "escape_bot", '''
import sneaky

async def main():
    pass
''')
            try:
                (escape / "sneaky.py").symlink_to(outside)
            except OSError:
                return  # symlinks unavailable on this filesystem; covered above
            s2 = Session("t-ws-escape")
            try:
                result = await run_script(s2, "", workspace="escape_bot", workspace_root=escape)
                assert result["ok"] is False
                message = s2.last_run["exception"]["message"]
                assert "outside the workspace" in message
                assert "OUTSIDE_MARKER" not in sys.modules
            finally:
                s2.close()
        finally:
            s.close()


async def test_workspace_cog_command_discovered_and_dispatched():
    """cogs/echo.py: setup(bot) + commands.Cog + app_commands.command."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        folder = _write_bot_workspace(pathlib.Path(td), "cog_bot", '''
import discord
from discord.ext import commands

async def setup(bot):
    await bot.add_cog(Echo(bot))

async def main():
    pass
''')
        cogs = folder / "cogs"
        cogs.mkdir(exist_ok=True)
        (cogs / "echo.py").write_text('''
import discord
from discord import app_commands
from discord.ext import commands

class Echo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="echo", description="say it back")
    async def echo(self, interaction: discord.Interaction, text: str):
        await interaction.response.send_message(f"echo:{text}")

    @commands.Cog.listener()
    async def on_message(self, message):
        pass
''', encoding="utf-8")
        (folder / "bot.py").write_text('''
from cogs.echo import Echo

async def setup(bot):
    await bot.add_cog(Echo(bot))

async def main():
    pass
''', encoding="utf-8")
        s = Session("t-ws-cog")
        try:
            result = await run_script(s, "", workspace="cog_bot", workspace_root=folder)
            assert result["ok"] is True, s.last_run
            assert "echo" in s.commands, s.commands
            assert s.client.get_cog("Echo") is not None
            assert s.client._listeners.get("on_message")
            await dispatch_command(s, "echo", {"text": "roundtrip"})
            assert last_msg(s)["content"] == "echo:roundtrip"
        finally:
            s.close()


async def test_workspace_rerun_isolation_no_stale_modules_or_duplicates():
    """Re-running the workspace re-imports fresh: no dupes, no stale sys.modules."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        folder = _write_bot_workspace(pathlib.Path(td), "isolation_bot", '''
import discord
from discord import app_commands
import helpers
from helpers import COUNTER

@app_commands.command(name="count")
async def count(interaction):
    await interaction.response.send_message(f"count:{COUNTER}")

async def main():
    pass
''')
        (folder / "helpers.py").write_text("print('importing helpers')\nCOUNTER = 0\n", encoding="utf-8")
        s = Session("t-ws-isolation")
        try:
            first = await run_script(s, "", workspace="isolation_bot", workspace_root=folder)
            assert first["ok"] is True, s.last_run
            assert sum(1 for event in s.events if "importing helpers" in event["text"]) == 1

            second = await run_script(s, "", workspace="isolation_bot", workspace_root=folder)
            assert second["ok"] is True, s.last_run
            assert list(s.commands) == ["count"]  # no duplicate registration
            # the second run re-imported helpers fresh (counter reset) and its
            # modules were unloaded afterwards — no stale entries left behind
            assert sum(1 for event in s.events if "importing helpers" in event["text"]) == 2
            assert not any(name.startswith("helpers") for name in sys.modules)
            await dispatch_command(s, "count", {})
            assert last_msg(s)["content"] == "count:0"
        finally:
            s.close()


async def test_workspace_deadline_interrupts_imported_module():
    """while True: pass in a helper is cut by the deadline, not the outer timeout."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        folder = _write_bot_workspace(pathlib.Path(td), "infinite_bot", '''
import spin

async def main():
    pass
''')
        (folder / "spin.py").write_text("while True:\n    pass\n", encoding="utf-8")
        s = Session("t-ws-deadline")
        try:
            result = await run_script(s, "", workspace="infinite_bot", workspace_root=folder, timeout=1.0)
            assert result["ok"] is False, result
            deadline_events = [event for event in s.events if "execution deadline" in event["text"]]
            assert deadline_events, [event["text"] for event in s.events]
            # ScriptStuck must win, not the outer runner timeout: a spinning
            # worker on a loaded CI runner can take longer than it does locally.
            assert result["ms"] < 10_000, f"took {result['ms']}ms: {result}"
        finally:
            s.close()


async def test_single_file_run_has_no_workspace():
    """Single-file regression: no workspace attrs, no __file__ leakage, same output."""
    s = Session("t-ws-single-file")
    try:
        result = await run_script(s, "async def main():\n    await send('plain')\n")
        assert result["ok"] is True
        assert last_msg(s)["content"] == "plain"
        assert s.workspace is None and s.workspace_root is None
        assert "__file__" not in s.env
        assert s.env["__name__"] == "playground"
        assert s.client.cogs == {}
        await dispatch_message(s, "hey")
        assert not s.modals
    finally:
        s.close()


async def test_restart_clears_workspace_state_and_route_arms_run():
    """Restart clears workspace import state; /run with a bot.py workspace runs it."""
    import json
    import tempfile

    import main as server

    with tempfile.TemporaryDirectory() as td:
        original = server.WORKSPACES_DIR
        server.WORKSPACES_DIR = pathlib.Path(td)
        folder = _write_bot_workspace(
            server.WORKSPACES_DIR, "route_bot",
            "async def main():\n    await send('workspace booted')\n")
        (folder / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
        sid = f"ws-route-{pathlib.Path(td).name}"
        s = Session(sid)
        previous_session = server.SESSIONS.get(sid)
        server.SESSIONS[sid] = s
        try:
            response = await server.run_code(_FakeReq(
                match={"sid": sid}, body={"workspace": "route_bot", "code": ""}
            ))
            data = json.loads(response.body)
            assert data["ok"] is True and data["mode"] == "workspace"
            assert s.workspace == "route_bot"
            assert "workspace booted" in s.messages[s.order[-1]]["content"]

            server.RUNTIMES[sid] = object()  # the reload route must still refuse hosted sessions
            try:
                hosted = await server.reload_code(_FakeReq(match={"sid": sid}, body={"code": "x = 1\n"}))
                assert hosted.status == 409
            finally:
                server.RUNTIMES.pop(sid, None)

            # Restart clears workspace import state but keeps the folder connected
            await server.restart(_FakeReq(match={"sid": sid}))
            assert s.workspace == "route_bot" and s.workspace_root == folder.resolve()
            assert s.env is None and s.commands == {} and s.client.cogs == {}
            assert not any(name.startswith("helpers") for name in sys.modules)
        finally:
            server.WORKSPACES_DIR = original
            if previous_session is None:
                server.SESSIONS.pop(sid, None)
            else:
                server.SESSIONS[sid] = previous_session
            s.close()


async def test_workspace_watcher_reloads_saved_file():
    import json
    import os
    import pathlib
    import tempfile

    import main as server

    with tempfile.TemporaryDirectory() as td:
        original = server.WORKSPACES_DIR
        original_interval = server._WORKSPACE_WATCH_INTERVAL
        server.WORKSPACES_DIR = pathlib.Path(td)
        server._WORKSPACE_WATCH_INTERVAL = 0.01
        folder = server.WORKSPACES_DIR / "watched_bot"
        folder.mkdir()
        bot_file = folder / "bot.py"
        bot_file.write_text(RELOAD_BASE, encoding="utf-8")
        s = Session("t-watcher")
        previous_session = server.SESSIONS.get(s.sid)
        server.SESSIONS[s.sid] = s
        # on_startup only runs under a real server; the poller is started by hand here.
        watcher = asyncio.create_task(server._watch_workspace_files(None))
        try:
            # Boot through the run-file path so the watcher arms on this file.
            response = await server.run_workspace_file(_FakeReq(
                match={"sid": s.sid},
                body={"workspace": "watched_bot", "filename": "bot.py", "code": RELOAD_BASE},
            ))
            assert json.loads(response.body)["ok"] is True
            assert s.sid in server.WORKSPACE_WATCHERS
            assert len(s.order) == 1

            # External editor saves the file; the poller must pick it up.
            bot_file.write_text(RELOAD_BASE + '''
async def on_click(interaction, custom_id, values):
    await interaction.response.send_message("auto-reloaded", ephemeral=True)
''', encoding="utf-8")
            armed = server.WORKSPACE_WATCHERS[s.sid]["mtime"]
            os.utime(bot_file, ns=(armed + 1_000_000, armed + 1_000_000))  # force a fresh mtime
            deadline = asyncio.get_running_loop().time() + 5
            while (not any("reloaded" in e["text"] for e in s.events)
                   and asyncio.get_running_loop().time() < deadline):
                await asyncio.sleep(0.01)
            assert any("reloaded" in e["text"] for e in s.events)
            assert len(s.order) == 1  # timeline kept — reload adds no message
            mid = s.order[0]  # the ORIGINAL welcome message
            await dispatch_click(s, mid, "go", [])
            assert last_msg(s)["content"] == "auto-reloaded"
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            server._disarm_workspace_watch(s)
            server.WORKSPACE_WATCHERS.clear()
            server.WORKSPACES_DIR = original
            server._WORKSPACE_WATCH_INTERVAL = original_interval
            if previous_session is None:
                server.SESSIONS.pop(s.sid, None)
            else:
                server.SESSIONS[s.sid] = previous_session
            s.close()


# --- rich mock surface: files, embed media, expanded selects ---------------------


def _tiny_png() -> bytes:
    """1x1 red PNG."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


async def test_files_serialize_as_data_uris():
    s = Session("t-files")
    await run_script(s, """
import io, discord
async def main():
    await send(content="pic", file=discord.File(io.BytesIO(TINY_PNG), "dot.png"))
""".replace("TINY_PNG", repr(_tiny_png())))
    f = last_msg(s)["files"][0]
    assert f["name"] == "dot.png"
    assert f["data_uri"].startswith("data:image/png;base64,")


async def test_non_image_file_stays_a_chip():
    s = Session("t-files2")
    await run_script(s, """
import io, discord
async def main():
    await send(file=discord.File(io.BytesIO(b'hello'), "notes.txt"))
""")
    f = last_msg(s)["files"][0]
    assert f["name"] == "notes.txt" and "data_uri" not in f


async def test_embed_media_serializes():
    s = Session("t-media")
    await run_script(s, """
import discord
async def main():
    e = discord.Embed(title="rich", color=0x123456)
    e.set_image(url="attachment://banner.png")
    e.set_thumbnail(url="attachment://icon.png")
    await send(embed=e)
""")
    e = last_msg(s)["embeds"][0]
    assert e["image"]["url"] == "attachment://banner.png"
    assert e["thumbnail"]["url"] == "attachment://icon.png"
    assert e["color"] == 0x123456


async def test_expanded_selects_serialize():
    s = Session("t-selects")
    await run_script(s, """
import discord
async def main():
    v = discord.ui.View()
    v.add_item(discord.ui.Select(placeholder="multi", custom_id="multi", max_values=3, options=[
        discord.SelectOption(label="A", value="a"),
        discord.SelectOption(label="B", value="b"),
    ]))
    v.add_item(discord.ui.UserSelect(placeholder="user", custom_id="who"))
    v.add_item(discord.ui.RoleSelect(placeholder="role", custom_id="role"))
    await send(view=v)
""")
    kinds = [c["kind"] for c in last_msg(s)["components"]]
    assert kinds == ["select", "user_select", "role_select"], kinds
    multi = last_msg(s)["components"][0]
    assert multi["max_values"] == 3


async def test_user_select_values_reach_handler():
    s = Session("t-userpick")
    await run_script(s, """
import discord
async def main():
    v = discord.ui.View()
    v.add_item(discord.ui.UserSelect(placeholder="who", custom_id="who"))
    await send(view=v)
async def on_click(interaction, custom_id, values):
    member = interaction.client.get_user(int(values[0]))
    await interaction.response.send_message(f"picked {member.name}")
""")
    await dispatch_click(s, s.order[-1], "who", ["111111111111111111"])
    assert last_msg(s)["content"] == "picked Alice"


# --- channels -----------------------------------------------------------------


async def test_create_channels_and_send_between():
    s = Session("t-chans")
    await run_script(s, """
async def main():
    ch = await client.guilds[0].create_text_channel("Announcements!")
    await ch.send('in announcements')
    await send('in playground')
    ch2 = await client.guilds[0].create_text_channel("announcements")  # dupe name
    await ch2.send('in announcements-2')
""")
    assert sorted(c.name for c in s.channels.values()) == ["announcements", "announcements-2", "playground"]
    by_channel = {}
    for mid in s.order:
        m = s.messages[mid]
        by_channel.setdefault(str(m["channel"]), []).append(m["content"])
    assert by_channel[str(s.channel.id)] == ["in playground"]
    ann = next(c for c in s.channels.values() if c.name == "announcements")
    assert "in announcements" in by_channel[str(ann.id)]
    assert "in announcements-2" in [m["content"] for m in s.messages.values()]


async def test_channel_deleted_send_raises():
    s = Session("t-chans2")
    await run_script(s, """
async def main():
    global temp
    temp = await client.guilds[0].create_text_channel("temp")
    await temp.send("gone soon")
    await temp.delete()
    try:
        await temp.send("after delete")
    except Exception as e:
        await send(f"caught {type(e).__name__}")
""")
    assert "temp" not in [c.name for c in s.channels.values()]
    assert "caught NotFound" in [s.messages[m]["content"] for m in s.order][-1]
    assert all(s.messages[m]["content"] != "gone soon" for m in s.order)  # messages went with it
    blocked = next(event for event in reversed(s.events)
                   if event.get("details", {}).get("status") == "missing_channel")
    assert blocked["kind"] == "action"
    assert blocked["details"]["operation"] == "channel.send"


def test_channel_reset_on_run():
    s = Session("t-chans3")
    s.make_channel("leftover")
    asyncio.get_event_loop_policy()
    run = asyncio.new_event_loop()
    try:
        run.run_until_complete(run_script(s, "async def main():\n    pass"))
    finally:
        run.close()
    assert [c.name for c in s.channels.values()] == ["playground"]


async def test_composer_message_delivery():
    s = await new_session_with(DEMO + "\n\nasync def on_message(message):\n    await message.reply('echo: ' + message.content)")
    await dispatch_message(s, "!ping")
    m = last_msg(s)
    assert m["content"] == "echo: !ping"
    assert m["author"]["name"] == "Playground Bot"  # the *bot* replies
    user_msg = s.messages[s.order[-2]]
    assert user_msg["author"]["bot"] is False


async def test_delete_and_fake_ids():
    s = Session("t7")
    await run_script(s, """
async def main():
    m = await send('temp')
    await m.delete()
    await send('kept')
""")
    assert "temp" not in [s.messages[mid]["content"] for mid in s.order]
    assert last_msg(s)["content"] == "kept"


async def test_interactions_recorded_on_real_discord_objects():
    """The mock layer must not corrupt real discord.py objects between runs."""
    from playground import MockInteraction

    s = await new_session_with(DEMO)
    inter = s.build_interaction(s.order[-1], "go", [])
    assert isinstance(inter, MockInteraction)
    assert isinstance(inter.user, playground.MockMember)
    e = discord.Embed(title="x")
    assert e.to_dict()["title"] == "x"  # sanity: real Embed still works


async def test_restart_boots_fresh_runtime():
    s = await new_session_with(DEMO)
    s.restart()
    assert len(s.order) == 0 and s.env is None and not s.events


class _FakeReq:
    """Just enough of an aiohttp request for the library handlers."""

    def __init__(self, match=None, body=None, query=None, cookies=None, secure=False):
        self.match_info = match or {}
        self._body = body or {}
        self.query = query or {}
        self.cookies = cookies or {}
        self.secure = secure

    async def json(self):
        return self._body


async def test_discord_oauth_unconfigured_mode():
    import json

    import main as server

    names = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI")
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        status = json.loads((await server.auth_status(_FakeReq())).body)
        assert status == {"configured": False, "authenticated": False, "user": None}
        response = await server.discord_login(_FakeReq())
        assert response.status == 503
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


async def test_discord_oauth_state_and_callback_errors():
    import main as server

    saved = {name: os.environ.get(name) for name in (
        "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"
    )}
    os.environ.update({
        "DISCORD_CLIENT_ID": "client-id",
        "DISCORD_CLIENT_SECRET": "client-secret",
        "DISCORD_REDIRECT_URI": "http://127.0.0.1:8741/auth/discord/callback",
    })
    try:
        redirect = await server.discord_login(_FakeReq())
        location = redirect.location
        cookie_state = redirect.cookies[server._OAUTH_STATE_COOKIE].value
        assert redirect.status == 302
        assert "scope=identify" in location and "guilds" not in location
        assert "client-secret" not in location
        assert cookie_state in server.OAUTH_STATES
        mismatch = await server.discord_callback(_FakeReq(query={"state": "wrong"}))
        assert mismatch.status == 400
        state_value = server._oauth_state()
        cancelled = await server.discord_callback(_FakeReq(
            query={"state": state_value, "error": "access_denied"},
            cookies={server._OAUTH_STATE_COOKIE: state_value},
        ))
        assert cancelled.status == 400
        assert "client-secret" not in str(cancelled)
    finally:
        server.OAUTH_STATES.clear()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def test_discord_oauth_success_and_logout():
    import json

    import main as server

    saved = {name: os.environ.get(name) for name in (
        "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"
    )}
    original_request = server._oauth_request
    os.environ.update({
        "DISCORD_CLIENT_ID": "client-id",
        "DISCORD_CLIENT_SECRET": "client-secret",
        "DISCORD_REDIRECT_URI": "http://127.0.0.1:8741/auth/discord/callback",
    })

    async def fake_request(method, url, **kwargs):
        if method == "POST":
            assert kwargs["data"]["client_secret"] == "client-secret"
            return 200, {"access_token": "access-token"}
        assert kwargs["headers"]["Authorization"] == "Bearer access-token"
        return 200, {"id": "42", "username": "reviewer", "global_name": "Reviewer", "avatar": "hash"}

    server._oauth_request = fake_request
    try:
        state_value = server._oauth_state()
        response = await server.discord_callback(_FakeReq(
            query={"state": state_value, "code": "code"},
            cookies={server._OAUTH_STATE_COOKIE: state_value},
        ))
        assert response.status == 302 and response.location == "/"
        cookie = response.cookies[server._AUTH_COOKIE]
        auth_id = cookie.value
        identity = server.AUTH_SESSIONS[auth_id]
        assert identity["id"] == "42" and identity["username"] == "reviewer"
        status = json.loads((await server.auth_status(_FakeReq(cookies={server._AUTH_COOKIE: auth_id}))).body)
        assert status["authenticated"] is True and status["user"] == identity
        logged_out = await server.logout(_FakeReq(cookies={server._AUTH_COOKIE: auth_id}))
        assert logged_out.status == 302 and auth_id not in server.AUTH_SESSIONS
    finally:
        server._oauth_request = original_request
        server.OAUTH_STATES.clear()
        server.AUTH_SESSIONS.clear()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def test_oauth_callback_access_log_redacts_query():
    import io
    import logging

    from aiohttp import ClientSession, web

    import main as server

    names = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI")
    saved = {name: os.environ.get(name) for name in names}
    os.environ.update({
        "DISCORD_CLIENT_ID": "client-id",
        "DISCORD_CLIENT_SECRET": "client-secret",
        "DISCORD_REDIRECT_URI": "http://127.0.0.1:8741/auth/discord/callback",
    })
    stream = io.StringIO()
    logger = logging.getLogger("test.oauth.access")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    # build_app() is deliberately transport-neutral; redaction belongs to the
    # production web.run_app() wiring and must be supplied by custom runners.
    runner = web.AppRunner(
        server.build_app(), access_log=logger, access_log_class=server._AccessLogger
    )
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        state_value = "supplied-state"
        code = "supplied-code"
        async with ClientSession() as client:
            callback = await client.get(
                f"http://127.0.0.1:{port}/auth/discord/callback"
                f"?state={state_value}&code={code}"
            )
            assert callback.status == 400
            await callback.read()
            status = await client.get(
                f"http://127.0.0.1:{port}/api/auth/status?probe=kept"
            )
            assert status.status == 200
            await status.read()
        logs = stream.getvalue()
        assert "GET /auth/discord/callback HTTP/1.1" in logs
        assert "code=" not in logs and "state=" not in logs
        assert code not in logs and state_value not in logs
        assert "GET /api/auth/status?probe=kept HTTP/1.1" in logs
    finally:
        await runner.cleanup()
        logger.removeHandler(handler)
        handler.close()
        server.OAUTH_STATES.clear()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def test_discord_oauth_provider_failures_are_generic():
    import json

    from aiohttp import ClientError

    import main as server

    names = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI")
    saved = {name: os.environ.get(name) for name in names}
    original_request = server._oauth_request
    os.environ.update({
        "DISCORD_CLIENT_ID": "client-id",
        "DISCORD_CLIENT_SECRET": "client-secret",
        "DISCORD_REDIRECT_URI": "http://127.0.0.1:8741/auth/discord/callback",
    })
    cases = (
        ("token-status", "Discord sign-in could not exchange the authorization code."),
        ("identity-status", "Discord sign-in could not retrieve your identity."),
        ("token-timeout", "Discord sign-in is temporarily unavailable."),
        ("identity-timeout", "Discord sign-in is temporarily unavailable."),
        ("token-network", "Discord sign-in is temporarily unavailable."),
        ("identity-network", "Discord sign-in is temporarily unavailable."),
    )
    try:
        for failure, expected in cases:
            async def fake_request(method, url, *, failure=failure, **kwargs):
                if method == "POST":
                    if failure == "token-status":
                        return 400, {"error": "provider detail"}
                    if failure == "token-timeout":
                        raise asyncio.TimeoutError
                    if failure == "token-network":
                        raise ClientError("provider detail")
                    return 200, {"access_token": "access-token"}
                if failure == "identity-status":
                    return 503, {"error": "provider detail"}
                if failure == "identity-timeout":
                    raise asyncio.TimeoutError
                if failure == "identity-network":
                    raise ClientError("provider detail")
                raise AssertionError(f"unexpected OAuth request for {failure}")

            server._oauth_request = fake_request
            state_value = server._oauth_state()
            response = await server.discord_callback(_FakeReq(
                query={"state": state_value, "code": "supplied-code"},
                cookies={server._OAUTH_STATE_COOKIE: state_value},
            ))
            payload = json.loads(response.body)
            assert response.status == 502
            assert payload == {"ok": False, "error": expected}
            assert not server.AUTH_SESSIONS
    finally:
        server._oauth_request = original_request
        server.OAUTH_STATES.clear()
        server.AUTH_SESSIONS.clear()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def test_script_library_roundtrip():
    import json
    import pathlib
    import tempfile

    import main as server

    with tempfile.TemporaryDirectory() as td:
        orig = (server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR)
        server.DATA_DIR = pathlib.Path(td)
        server.SCRIPTS_DIR = pathlib.Path(td)
        server.WORKSPACES_DIR = pathlib.Path(td) / "bots"
        try:
            r = await server.save_script(_FakeReq(body={"name": "my bot", "code": '"""Doc here."""\nx = 1\n'}))
            data = json.loads(r.body)
            assert data["ok"] is True and data["existed"] is False

            listing = json.loads((await server.list_scripts(None)).body)["scripts"]
            assert [s["name"] for s in listing] == ["my bot"]
            assert listing[0]["description"] == "Doc here."

            code = json.loads((await server.get_script(_FakeReq(match={"name": "my bot"}))).body)["code"]
            assert code == '"""Doc here."""\nx = 1\n'

            r = await server.save_script(_FakeReq(body={"name": "../evil", "code": "1"}))
            assert r.status == 400  # path escape rejected

            r = await server.save_script(_FakeReq(body={"name": "ok", "code": "   "}))
            assert r.status == 400  # empty script refused

            await server.delete_script(_FakeReq(match={"name": "my bot"}))
            assert list(server.SCRIPTS_DIR.glob("*.py")) == []
        finally:
            server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR = orig


DESIGN = {
    "version": 1,
    "metadata": {"name": "Test project"},
    "bot": {"username": "TestBot", "avatarUrl": ""},
    "tree": [
        {
            "type": 17,  # Container
            "accent_color": 0x5865F2,
            "spoiler": False,
            "components": [
                {"type": 10, "content": "Welcome to **the club**"},
                {"type": 9, "components": [{"type": 10, "content": "Section text"}],
                 "accessory": {"type": 11, "media": {"url": "https://x/y.png"}}},
                {"type": 1, "components": [
                    {"type": 2, "style": 3, "label": "Join", "custom_id": "join"},
                    {"type": 2, "style": 5, "label": "Docs", "url": "https://example.com"},
                ]},
                {"type": 3, "custom_id": "tier", "placeholder": "Pick a tier",
                 "options": [{"label": "Basic", "value": "basic", "description": "Free"},
                             {"label": "Pro", "value": "pro", "default": True}]},
                {"type": 12, "items": [{"media": {"url": "attachment://pic.png"}, "spoiler": True}]},
                {"type": 14, "divider": True, "spacing": 2},
            ],
        }
    ]
}


async def test_folder_workspace_roundtrip():
    import json
    import pathlib
    import tempfile

    import main as server

    with tempfile.TemporaryDirectory() as td:
        original = server.WORKSPACES_DIR
        server.WORKSPACES_DIR = pathlib.Path(td)
        try:
            folder = server.WORKSPACES_DIR / "my_bot"
            folder.mkdir()
            source = "async def main():\n    await send('folder')\n"
            nested_source = "async def main():\n    await send('nested')\n"
            (folder / "bot.py").write_text(source, encoding="utf-8")
            (folder / "cogs").mkdir()
            (folder / "cogs" / "worker.py").write_text(nested_source, encoding="utf-8")
            listing = json.loads((await server.list_workspaces(None)).body)["workspaces"]
            assert listing == [{"name": "my_bot", "files": ["bot.py", "cogs/worker.py"]}]
            request = _FakeReq(match={"workspace": "my_bot", "filename": "cogs/worker.py"})
            assert json.loads((await server.get_workspace_file(request)).body) == {
                "name": "cogs/worker.py", "code": nested_source
            }
            saved = await server.save_workspace_file(_FakeReq(
                match=request.match_info, body={"code": "updated = True\n"}
            ))
            assert json.loads(saved.body) == {"ok": True, "name": "cogs/worker.py"}
            assert (folder / "cogs" / "worker.py").read_text(encoding="utf-8") == "updated = True\n"
            for workspace, filename in (("../bad", "bot.py"), ("my_bot", "../outside.py"),
                                        ("my_bot", "cogs/../../outside.py"),
                                        ("my_bot", "cogs\\worker.py"), ("my_bot", "cogs/worker.txt")):
                try:
                    await server.save_workspace_file(_FakeReq(
                        match={"workspace": workspace, "filename": filename}, body={"code": "x"}
                    ))
                except server.web.HTTPBadRequest:
                    pass
                else:
                    raise AssertionError(f"unsafe workspace path was accepted: {workspace}/{filename}")
            outside = pathlib.Path(td) / "outside.py"
            outside.write_text("secret = True\n", encoding="utf-8")
            try:
                (folder / "escape.py").symlink_to(outside)
            except OSError:  # Windows may not allow symlink creation without Developer Mode.
                pass
            else:
                try:
                    await server.get_workspace_file(_FakeReq(
                        match={"workspace": "my_bot", "filename": "escape.py"}
                    ))
                except server.web.HTTPBadRequest:
                    pass
                else:
                    raise AssertionError("workspace symlink escape was accepted")
        finally:
            server.WORKSPACES_DIR = original


async def test_workspace_run_actions_use_saved_project_vs_editor_buffer():
    import json
    import pathlib
    import tempfile

    import main as server

    with tempfile.TemporaryDirectory() as td:
        original_workspace_dir = server.WORKSPACES_DIR
        server.WORKSPACES_DIR = pathlib.Path(td)
        folder = server.WORKSPACES_DIR / "run_semantics"
        (folder / "cogs").mkdir(parents=True)
        saved_entry = "# saved bot entry\n"
        saved_file = "async def main():\n    await send('saved file')\n"
        (folder / "bot.py").write_text(saved_entry, encoding="utf-8")
        (folder / "cogs" / "worker.py").write_text(saved_file, encoding="utf-8")

        sid = f"run-semantics-{pathlib.Path(td).name}"
        session = Session(sid)
        previous_session = server.SESSIONS.get(sid)
        previous_runtime = server.RUNTIMES.get(sid)
        original_run_project = server.bot_worker.run_worker_project
        original_run_script = server.run_script
        captured = {}

        class FakeRuntime:
            async def shutdown(self):
                captured["shutdown"] = True

            def status(self):
                return {"bot": "saved bot", "cogs": [], "commands": []}

        async def fake_run_project(_session, project, on_exception=None):
            captured["project_entry"] = (project / "main.py").read_text(encoding="utf-8")
            captured["on_exception"] = on_exception
            return FakeRuntime()

        async def fake_run_script(_session, code, **_kwargs):
            captured["file_buffer"] = code
            captured["run_kwargs"] = _kwargs
            return {"ok": True, "ms": 1.0}

        server.SESSIONS[sid] = session
        server.bot_worker.run_worker_project = fake_run_project
        server.run_script = fake_run_script
        try:
            # A bot.py workspace now runs through the playground runtime (the
            # saved bot.py is the entry; the editor buffer is passed through
            # but ignored by the real run_script for entry runs).
            project_response = await server.run_code(_FakeReq(
                match={"sid": sid}, body={"workspace": "run_semantics", "code": "unsaved bot buffer"}
            ))
            project = json.loads(project_response.body)
            assert project["ok"] and project["mode"] == "workspace"
            assert captured["file_buffer"] == "unsaved bot buffer"
            assert captured["run_kwargs"]["workspace"] == "run_semantics"
            assert captured["run_kwargs"]["workspace_root"] == folder
            captured.clear()

            # A folder with a different entry (main.py) keeps the hosted boot.
            main_folder = server.WORKSPACES_DIR / "hosted_semantics"
            main_folder.mkdir()
            (main_folder / "main.py").write_text("# hosted entry\n", encoding="utf-8")
            hosted_response = await server.run_code(_FakeReq(
                match={"sid": sid}, body={"workspace": "hosted_semantics", "code": "editor text"}
            ))
            hosted = json.loads(hosted_response.body)
            assert hosted["ok"] and hosted["mode"] == "project"
            assert captured["project_entry"] == "# hosted entry\n"
            assert callable(captured["on_exception"])
            captured.clear()

            file_response = await server.run_workspace_file(_FakeReq(
                match={"sid": sid}, body={
                    "workspace": "run_semantics", "filename": "cogs/worker.py",
                    "code": "async def main():\n    await send('unsaved active buffer')\n",
                }
            ))
            file_result = json.loads(file_response.body)
            assert file_result["ok"] and file_result["mode"] == "file"
            assert file_result["file"] == "cogs/worker.py"
            assert captured["file_buffer"].endswith("unsaved active buffer')\n")
            assert (folder / "cogs" / "worker.py").read_text(encoding="utf-8") == saved_file
            assert captured["shutdown"] is True  # the hosted runtime from the main.py boot above

            invalid = await server.run_workspace_file(_FakeReq(
                match={"sid": sid}, body={
                    "workspace": "run_semantics", "filename": "../outside.py", "code": "x = 1\n"
                }
            ))
            assert invalid.status == 400
        finally:
            server.bot_worker.run_worker_project = original_run_project
            server.run_script = original_run_script
            server.WORKSPACES_DIR = original_workspace_dir
            if previous_session is None:
                server.SESSIONS.pop(sid, None)
            else:
                server.SESSIONS[sid] = previous_session
            if previous_runtime is None:
                server.RUNTIMES.pop(sid, None)
            else:
                server.RUNTIMES[sid] = previous_runtime
            session.close()


async def test_empty_and_syntax_workspace_errors_keep_available_diagnostics():
    import json
    import pathlib
    import tempfile

    import bot_runtime
    import main as server

    with tempfile.TemporaryDirectory() as td:
        original_workspace_dir = server.WORKSPACES_DIR
        server.WORKSPACES_DIR = pathlib.Path(td)
        sid = f"empty-project-{pathlib.Path(td).name}"
        session = Session(sid)
        previous_session = server.SESSIONS.get(sid)
        server.SESSIONS[sid] = session
        folder = server.WORKSPACES_DIR / "invalid_project"
        folder.mkdir()
        try:
            empty = await server.run_code(_FakeReq(
                match={"sid": sid}, body={"workspace": "invalid_project"}
            ))
            empty_data = json.loads(empty.body)
            assert empty.status == 500 and empty_data["ok"] is False
            empty_error = empty_data["last_run"]["exception"]
            assert {key: empty_error[key] for key in ("type", "file", "line", "workspace")} == {
                "type": "RuntimeError", "file": None, "line": None, "workspace": "invalid_project",
            }
            assert "no Python entrypoint found" in empty_data["last_run"]["error"]

            (folder / "main.py").write_text("import discord\ndef broken(:\n", encoding="utf-8")
            syntax = await server.run_code(_FakeReq(
                match={"sid": sid}, body={"workspace": "invalid_project"}
            ))
            syntax_data = json.loads(syntax.body)
            assert syntax.status == 500 and syntax_data["ok"] is False
            syntax_error = syntax_data["last_run"]["exception"]
            assert {key: syntax_error[key] for key in ("type", "file", "line", "workspace")} == {
                "type": "SyntaxError", "file": "main.py", "line": 2, "workspace": "invalid_project",
            }
            assert "invalid_project/main.py" in syntax_data["last_run"]["error"]
            assert str(bot_runtime._SANDBOX_ROOT) not in syntax_data["last_run"]["error"]
            assert state(session)["last_run"] == syntax_data["last_run"]
        finally:
            server.WORKSPACES_DIR = original_workspace_dir
            if previous_session is None:
                server.SESSIONS.pop(sid, None)
            else:
                server.SESSIONS[sid] = previous_session
            session.close()


async def test_script_timeout_error_is_not_reported_as_boot_deadline_over_http():
    import pathlib
    import tempfile

    from aiohttp.test_utils import TestClient, TestServer

    import main as server

    with tempfile.TemporaryDirectory() as td:
        original_workspace_dir = server.WORKSPACES_DIR
        sid = f"script-timeout-{pathlib.Path(td).name}"
        session = Session(sid)
        previous_session = server.SESSIONS.get(sid)
        previous_runtime = server.RUNTIMES.get(sid)
        server.WORKSPACES_DIR = pathlib.Path(td)
        server.SESSIONS[sid] = session
        folder = server.WORKSPACES_DIR / "timeout_project"
        folder.mkdir()
        (folder / "main.py").write_text("raise TimeoutError('from script')\n", encoding="utf-8")
        app = server.web.Application()
        app.router.add_post("/api/session/{sid}/run", server.run_code)
        client = TestClient(TestServer(app))
        try:
            await client.start_server()
            response = await client.post(
                f"/api/session/{sid}/run", json={"workspace": "timeout_project"}
            )
            data = await response.json()
            assert response.status == 500
            assert data["error"] == "TimeoutError: from script"
            assert session.last_run is not None
            assert session.last_run["exception"]["type"] == "TimeoutError"
            exception = data["last_run"]["exception"]
            assert {key: exception[key] for key in ("type", "message", "file", "line")} == {
                "type": "TimeoutError", "message": "from script", "file": "main.py", "line": 1,
            }
            assert session.last_run == data["last_run"] == state(session)["last_run"]
        finally:
            await client.close()
            server.WORKSPACES_DIR = original_workspace_dir
            runtime = server.RUNTIMES.pop(sid, None)
            if runtime is not None:
                await runtime.shutdown()
            if previous_runtime is not None:
                server.RUNTIMES[sid] = previous_runtime
            if previous_session is None:
                server.SESSIONS.pop(sid, None)
            else:
                server.SESSIONS[sid] = previous_session
            session.close()


async def test_concurrent_workspace_runs_replace_runtime_without_orphans():
    import pathlib
    import tempfile

    from aiohttp.test_utils import TestClient, TestServer

    import bot_worker
    import main as server

    with tempfile.TemporaryDirectory() as td:
        original_workspace_dir = server.WORKSPACES_DIR
        sid = f"concurrent-project-{pathlib.Path(td).name}"
        session = Session(sid)
        previous_session = server.SESSIONS.get(sid)
        previous_runtime = server.RUNTIMES.get(sid)
        server.WORKSPACES_DIR = pathlib.Path(td)
        server.SESSIONS[sid] = session
        folder = server.WORKSPACES_DIR / "concurrent_project"
        folder.mkdir()
        (folder / "main.py").write_text(
            "import discord\nfrom discord.ext import commands\n"
            "bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())\n"
            "bot.run('offline-simulated-token')\n", encoding="utf-8",
        )
        created = []
        original_boot = server.bot_worker.run_worker_project

        async def recording_run_project(target_session, project, tag=None, on_exception=None):
            runtime = await original_boot(target_session, project, tag=tag,
                                          on_exception=on_exception)
            created.append(runtime)
            return runtime

        app = server.web.Application()
        app.router.add_post("/api/session/{sid}/run", server.run_code)
        client = TestClient(TestServer(app))
        try:
            server.bot_worker.run_worker_project = recording_run_project
            await client.start_server()
            responses = await asyncio.gather(*(
                client.post(f"/api/session/{sid}/run", json={"workspace": "concurrent_project"})
                for _ in range(2)
            ))
            assert [response.status for response in responses] == [200, 200]
            current = server.RUNTIMES[sid]
            assert isinstance(current, bot_worker.WorkerProjectRuntime)
            assert current.session is session
            assert created and created[-1] is current
            assert current.process is not None and current.process.returncode is None
            # exactly one live worker: every replaced worker process is gone
            for stale in created[:-1]:
                assert stale.process is None or stale.process.returncode is not None
        finally:
            server.bot_worker.run_worker_project = original_boot
            await client.close()
            server.WORKSPACES_DIR = original_workspace_dir
            server.RUNTIMES.pop(sid, None)
            for stale in created:
                await stale.shutdown()
            if previous_runtime is not None:
                server.RUNTIMES[sid] = previous_runtime
            if previous_session is None:
                server.SESSIONS.pop(sid, None)
            else:
                server.SESSIONS[sid] = previous_session
            session.close()

async def test_project_boot_timeout_cancellation_cleans_runtime():
    import pathlib
    import tempfile

    from aiohttp.test_utils import TestClient, TestServer

    import main as server

    with tempfile.TemporaryDirectory() as td:
        original_workspace_dir = server.WORKSPACES_DIR
        original_wait = server.asyncio.wait
        original_bump = server._bump
        server.WORKSPACES_DIR = pathlib.Path(td)
        sid = f"boot-timeout-{pathlib.Path(td).name}"
        session = Session(sid)
        previous_session = server.SESSIONS.get(sid)
        previous_runtime = server.RUNTIMES.get(sid)
        server.SESSIONS[sid] = session
        folder = server.WORKSPACES_DIR / "slow_project"
        folder.mkdir()
        # Blocks during import, so the worker can never answer the boot request
        # no matter how fast the runner is. A main() that merely awaits forever
        # is not enough: script-mode boot does not wait for main() to finish, so
        # the run succeeds and the test depends on startup being slow.
        (folder / "main.py").write_text(
            "import time\n\ntime.sleep(600)\n\nasync def main():\n    pass\n",
            encoding="utf-8",
        )
        bumps = []
        created = []
        original_boot = server.bot_worker.run_worker_project

        async def slow_run_project(target_session, project, tag=None, on_exception=None):
            runtime = await original_boot(target_session, project, tag="smoke-timeout",
                                          on_exception=on_exception)
            created.append(runtime)
            return runtime

        async def short_wait(tasks, timeout=None):
            if timeout == 90:
                # The worker blocks for 600s, so this only has to be short enough
                # to keep the test quick; it no longer races worker startup.
                timeout = 5.0
            return await original_wait(tasks, timeout=timeout)

        server.asyncio.wait = short_wait
        server.bot_worker.run_worker_project = slow_run_project
        server._bump = lambda changed_sid: bumps.append(changed_sid)
        app = server.web.Application()
        app.router.add_post("/api/session/{sid}/run", server.run_code)
        app.router.add_post("/api/session/{sid}/restart", server.restart)
        client = TestClient(TestServer(app))
        try:
            await client.start_server()
            response = await client.post(
                f"/api/session/{sid}/run", json={"workspace": "slow_project"}
            )
            data = await response.json()
            assert response.status == 504 and not data["ok"], (response.status, data)
            assert data["error"] == "project boot exceeded 90s", data
            # nothing survives the deadline: no runtime, no worker process
            assert server.RUNTIMES.get(sid) is None, sorted(server.RUNTIMES)
            for runtime in created:
                assert runtime.process is None or runtime.process.returncode is not None, (
                    f"worker {runtime.worker_pid} outlived the boot deadline")
            assert session.last_run is None and state(session)["last_run"] is None, session.last_run
            errors = [event for event in session.events if event.get("cls") == "error"]
            assert not errors, [event.get("text") for event in errors]
            assert bumps and set(bumps) == {sid}, bumps
        finally:
            server.bot_worker.run_worker_project = original_boot
            server.asyncio.wait = original_wait
            server._bump = original_bump
            await client.close()
            server.WORKSPACES_DIR = original_workspace_dir
            runtime = server.RUNTIMES.pop(sid, None)
            if runtime is not None:
                await runtime.shutdown()
            for runtime in created:
                await runtime.shutdown()
            if previous_runtime is not None:
                server.RUNTIMES[sid] = previous_runtime
            if previous_session is None:
                server.SESSIONS.pop(sid, None)
            else:
                server.SESSIONS[sid] = previous_session
            session.close()

async def test_project_system_exit_cleanup_through_route():
    import json
    import pathlib
    import tempfile

    import bot_runtime
    import main as server

    with tempfile.TemporaryDirectory() as td:
        original_workspace_dir = server.WORKSPACES_DIR
        server.WORKSPACES_DIR = pathlib.Path(td)
        sid = f"boot-errors-{pathlib.Path(td).name}"
        session = Session(sid)
        previous_session = server.SESSIONS.get(sid)
        previous_runtime = server.RUNTIMES.get(sid)
        server.SESSIONS[sid] = session
        folder = server.WORKSPACES_DIR / "system_exit"
        folder.mkdir()
        (folder / "main.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
        try:
            response = await server.run_code(_FakeReq(
                match={"sid": sid}, body={"workspace": "system_exit"}
            ))
            data = json.loads(response.body)
            assert response.status == 500 and not data["ok"]
            assert data["last_run"]["exception"]["type"] == "SystemExit"
            start = next(event for event in server.state(session)["events"]  # worker timeline
                         if event.get("details", {}).get("operation") == "project.run")
            sandbox = pathlib.Path(bot_runtime._SANDBOX_ROOT) / start["details"]["sandbox"]
            assert not sandbox.exists() and str(sandbox) not in bot_runtime.sys.path
            assert str(sandbox) not in data["last_run"]["error"]
            assert bot_runtime.asyncio.run is bot_runtime._ORIGINAL_ASYNCIO_RUN

            good = server.WORKSPACES_DIR / "retry"
            good.mkdir()
            (good / "main.py").write_text("async def main():\n    pass\n", encoding="utf-8")
            retry = await server.run_code(_FakeReq(
                match={"sid": sid}, body={"workspace": "retry"}
            ))
            assert retry.status == 200 and json.loads(retry.body)["ok"]
        finally:
            server.WORKSPACES_DIR = original_workspace_dir
            runtime = server.RUNTIMES.pop(sid, None)
            if runtime is not None:
                await runtime.shutdown()
            if previous_runtime is not None:
                server.RUNTIMES[sid] = previous_runtime
            if previous_session is None:
                server.SESSIONS.pop(sid, None)
            else:
                server.SESSIONS[sid] = previous_session
            session.close()


async def test_library_test_suite_runs_isolated_scripts():
    import json
    import pathlib
    import tempfile

    import main as server

    with tempfile.TemporaryDirectory() as td:
        original = server.SCRIPTS_DIR
        server.SCRIPTS_DIR = pathlib.Path(td)
        try:
            server.SCRIPTS_DIR.joinpath("good.py").write_text(
                "async def main():\n    await send('good')\n", encoding="utf-8"
            )
            server.SCRIPTS_DIR.joinpath("bad.py").write_text(
                "async def main():\n    raise RuntimeError('bad')\n", encoding="utf-8"
            )
            server.SCRIPTS_DIR.joinpath("nested").mkdir()
            server.SCRIPTS_DIR.joinpath("nested", "ignored.py").write_text("1", encoding="utf-8")
            reports = json.loads((await server.test_scripts(_FakeReq(None))).body)["reports"]
            assert [(report["name"], report["ok"]) for report in reports] == [
                ("bad", False), ("good", True)
            ]
            assert reports[1]["messages"] == 1
        finally:
            server.SCRIPTS_DIR = original


async def test_project_state_validation_and_roundtrip():
    import bridge
    from project_state import validate_project

    assert validate_project(DESIGN) is DESIGN
    code = bridge.design_to_code(DESIGN)
    s = Session("t-project-state")
    await run_script(s, code)
    assert last_msg(s)["v2"][0]["v2"] == "container"
    for invalid, message in [
        ({"tree": []}, "version"),
        ({"version": 1, "tree": [{"type": 999}]}, "supported"),
        ({"version": 1, "tree": [{"type": 17, "components": "bad"}]}, "array"),
    ]:
        try:
            validate_project(invalid)
        except (TypeError, ValueError) as error:
            assert message in str(error)
        else:
            raise AssertionError("invalid project was accepted")


async def test_bridge_fixture_roundtrips():
    import json
    from pathlib import Path

    import bridge
    from project_state import validate_project

    fixture_dir = Path(__file__).parent / "fixtures" / "bridge_roundtrip"
    expected = {
        "container-section-thumbnail.discordv2proj.json": "container",
        "section-button.discordv2proj.json": "section",
        "gallery-one.discordv2proj.json": "gallery",
        "gallery-ten.discordv2proj.json": "gallery",
        "separators.discordv2proj.json": "separator",
        "nested-spoiler-controls.discordv2proj.json": "container",
    }
    for filename, kind in expected.items():
        project = json.loads((fixture_dir / filename).read_text(encoding="utf-8"))
        code = bridge.design_to_code(project)
        validate_project(project)
        session = Session(filename)
        try:
            result = await run_script(session, code)
            assert result["ok"], filename
            roots = last_msg(session)["v2"]
            assert roots[0]["v2"] == kind, filename
            if filename == "container-section-thumbnail.discordv2proj.json":
                assert roots[0]["children"][1]["accessory"]["v2"] == "thumbnail"
            elif filename == "section-button.discordv2proj.json":
                assert roots[0]["accessory"]["kind"] == "button"
            elif filename == "gallery-one.discordv2proj.json":
                assert len(roots[0]["items"]) == 1
            elif filename == "gallery-ten.discordv2proj.json":
                assert len(roots[0]["items"]) == 10
            elif filename == "separators.discordv2proj.json":
                assert [node["spacing"] for node in roots] == [1, 2]
            else:
                assert roots[0]["spoiler"] is True
                assert roots[0]["children"][0]["children"][0]["v2"] == "actionrow"
        finally:
            session.close()

    nested = Session("nested-controls")
    try:
        project = json.loads((fixture_dir / "nested-spoiler-controls.discordv2proj.json").read_text(encoding="utf-8"))
        script = bridge.design_to_code(project) + """
async def on_click(interaction, custom_id, values):
    await interaction.response.send_message(custom_id)
"""
        await run_script(nested, script)
        message = last_msg(nested)
        await dispatch_click(nested, message["id"], "deep_button", [])
        await dispatch_click(nested, message["id"], "deep_select", ["one"])
        assert [nested.messages[mid]["content"] for mid in nested.order[-2:]] == ["deep_button", "deep_select"]
    finally:
        nested.close()


def _check_embeder_vendoring_requires_an_explicit_source():
    project = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "scripts/vendor_embeder.py", "--help"],
        cwd=project, capture_output=True, text=True, check=True,
    )
    assert "path to the DiscordEmbeder checkout" in result.stdout
    missing = subprocess.run(
        [sys.executable, "-X", "utf8", "scripts/vendor_embeder.py"],
        cwd=project, capture_output=True, text=True, check=False,
    )
    assert missing.returncode == 2 and "source" in missing.stderr


def _check_embeder_vendoring_injects_bridge_from_source():
    from scripts import vendor_embeder

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "upstream"
        (source / "dist").mkdir(parents=True)
        (source / "dist" / "index.html").write_text("<html><body>upstream</body></html>", encoding="utf-8")
        project = root / "playground"
        (project / "embeder").mkdir(parents=True)
        (project / "embeder" / "bridge-inject.fragment.html").write_text("<!-- bridge -->", encoding="utf-8")
        original_root = vendor_embeder.ROOT
        vendor_embeder.ROOT = project
        try:
            vendor_embeder.vendor(source)
            built = (project / "embeder" / "index.html").read_text(encoding="utf-8")
            assert built == "<html><body>upstream<!-- bridge --></body></html>"
            marker = (project / "embeder" / "VENDORED_FROM.txt").read_text(encoding="utf-8")
            assert marker.startswith(f"source: {source}\ncommit: unknown\n")
            assert marker.endswith("+00:00\n")
        finally:
            vendor_embeder.ROOT = original_root


async def test_embeder_vendoring_requires_an_explicit_source():
    _check_embeder_vendoring_requires_an_explicit_source()


async def test_embeder_vendoring_injects_bridge_from_source():
    _check_embeder_vendoring_injects_bridge_from_source()


async def test_embeder_provenance_endpoint():
    import json
    import re

    import main as server

    marker_text = (server.EMBEDER_DIR / "VENDORED_FROM.txt").read_text(encoding="utf-8")
    fragment = (server.EMBEDER_DIR / "bridge-inject.fragment.html").read_text(encoding="utf-8")
    built = (server.EMBEDER_DIR / "index.html").read_text(encoding="utf-8")
    dynamic_url = 'window.location.origin + "/embeder'
    assert dynamic_url in fragment and dynamic_url in built
    assert "127.0.0.1:8741/embeder" not in built
    vendor_script = (Path(__file__).resolve().parent.parent / "scripts" / "vendor_embeder.py").read_text(encoding="utf-8")
    assert "DEFAULT_SOURCE" not in vendor_script and "E:/" not in vendor_script
    response = await server.embeder_info(None)
    data = json.loads(response.body)
    marker = re.search(r"^commit: ([0-9a-f]{40})$", marker_text, re.MULTILINE)
    assert data["ok"] is True and marker
    assert data["provenance"] == marker_text


async def test_bridge_route_validates_before_generation():
    import json

    import main as server

    response = await server.bridge_design_to_code(_FakeReq(body={"design": DESIGN}))
    assert json.loads(response.body)["ok"] is True
    response = await server.bridge_design_to_code(_FakeReq(body={"design": {"version": 1, "tree": []}}))
    assert response.status == 400


async def test_bridge_design_to_code():
    import bridge

    code = bridge.design_to_code(DESIGN)
    assert "ui.LayoutView(timeout=None)" in code
    assert "ui.Container(" in code and "accent_colour=0x5865f2," in code
    assert 'ui.Section(' in code and 'accessory=ui.Thumbnail("https://x/y.png")' in code
    assert 'ui.Button(label="Join", style=discord.ButtonStyle.success, custom_id="join")' in code
    assert 'ui.Button(label="Docs", style=discord.ButtonStyle.link, url="https://example.com")' in code
    assert 'ui.Select(' in code and 'discord.SelectOption(label="Pro", value="pro", default=True)' in code
    assert 'discord.MediaGalleryItem("attachment://pic.png", spoiler=True)' in code
    assert "discord.SeparatorSpacing.large" in code
    assert "await send(view=build_view())" in code


async def test_design_message_roundtrip():
    """The generated code sends a real LayoutView; the mock captures it as v2."""
    import bridge

    s = Session("t-v2")
    await run_script(s, bridge.design_to_code(DESIGN))
    msg = last_msg(s)
    assert msg["components"] is None  # classic path not used
    v2 = msg["v2"]
    assert len(v2) == 1 and v2[0]["v2"] == "container"
    assert v2[0]["accent_color"] == 0x5865F2
    kinds = [c.get("v2") or c.get("kind") for c in v2[0]["children"]]
    assert kinds == ["text", "section", "actionrow", "select", "gallery", "separator"], kinds
    row = v2[0]["children"][2]
    assert [c["kind"] for c in row["children"]] == ["button", "button"]
    section = v2[0]["children"][1]
    assert section["accessory"]["v2"] == "thumbnail"


async def test_v2_clickable_components_reach_handler():
    """Buttons/selects nested in a LayoutView must dispatch on_click like classic ones."""
    import bridge

    script = bridge.design_to_code(DESIGN) + """

async def on_click(interaction, custom_id, values):
    await interaction.response.send_message(f"got {custom_id}: {values}", ephemeral=True)
"""
    s = Session("t-v2click")
    await run_script(s, script)
    msg = last_msg(s)
    await dispatch_click(s, msg["id"], "join", [])
    await dispatch_click(s, msg["id"], "tier", ["pro"])
    replied = [m["content"] for m in s.messages.values() if m["content"].startswith("got ")]
    assert "got join: []" in replied, replied
    assert "got tier: ['pro']" in replied, replied


async def test_v2_max_component_stress_roundtrip():
    """The bridge/playground survive a dense valid Components V2 payload."""
    import bridge

    children = [
        {"type": 10, "content": "x" * 1000} for _ in range(3)
    ]
    children += [
        {"type": 9, "components": [{"type": 10, "content": f"Section {i}"}],
         "accessory": {"type": 11, "media": {"url": "https://example.com/thumb.png"}}}
        for i in range(2)
    ]
    children += [
        {"type": 1, "components": [
            {"type": 2, "style": 1, "label": f"Button {row}-{col}",
             "custom_id": f"b-{row}-{col}"} for col in range(5)
        ]}
        for row in range(2)
    ]
    children += [
        {"type": 3, "custom_id": "max-select",
         "options": [{"label": f"Option {i}", "value": str(i)} for i in range(25)]},
        {"type": 12, "items": [{"media": {"url": "https://example.com/gallery.png"}}]},
        {"type": 14, "divider": True, "spacing": 2},
    ]
    smaller = [{"type": 10, "content": "small"} for _ in range(2)]
    smaller += [{"type": 9, "components": [{"type": 10, "content": "small section"}],
                 "accessory": {"type": 11, "media": {"url": "https://example.com/thumb.png"}}}]
    smaller += [{"type": 1, "components": [
        {"type": 2, "style": 1, "label": f"Small {i}", "custom_id": f"small-{i}"}
        for i in range(5)
    ]}, {"type": 3, "custom_id": "small-select",
         "options": [{"label": str(i), "value": str(i)} for i in range(25)]},
        {"type": 12, "items": [{"media": {"url": "https://example.com/gallery.png"}}]},
        {"type": 14, "divider": True, "spacing": 2}]
    script = bridge.design_to_code({
        "version": 1, "tree": [
            {"type": 17, "components": children}, {"type": 17, "components": smaller}
        ]
    }) + """

async def on_click(interaction, custom_id, values):
    await interaction.response.send_message(f"got {custom_id}: {values}")
"""
    s = Session("t-v2-max")
    try:
        result = await run_script(s, script)
        assert result["ok"] is True
        msg = last_msg(s)
        assert len(msg["v2"]) == 2
        assert len(msg["v2"][0]["children"]) == 10
        def count(node):
            total = 1 + sum(count(child) for child in node.get("children", []))
            return total + (count(node["accessory"]) if node.get("accessory") else 0)
        assert sum(count(root) for root in msg["v2"]) == 40
        rows = [c for c in msg["v2"][0]["children"] if c.get("v2") == "actionrow"]
        assert sum(len(row["children"]) for row in rows) == 10
        select = next(c for c in msg["v2"][0]["children"] if c.get("kind") == "select")
        assert len(select["options"]) == 25
        await dispatch_click(s, msg["id"], "b-1-4", [])
        assert last_msg(s)["content"] == "got b-1-4: []"
        await dispatch_click(s, msg["id"], "max-select", ["24"])
        assert last_msg(s)["content"] == "got max-select: ['24']"
    finally:
        s.close()


async def test_desktop_auto_shutdown_after_last_websocket_disconnects():
    from aiohttp.test_utils import TestClient, TestServer

    import main as server

    old_grace, old_interval = server._DESKTOP_CLOSE_GRACE, server._DESKTOP_POLL_INTERVAL
    server._DESKTOP_CLOSE_GRACE = 0.03
    server._DESKTOP_POLL_INTERVAL = 0.002
    app = server.build_app(auto_shutdown=True)
    shutdown_callback = asyncio.Event()

    async def on_shutdown(_app):
        shutdown_callback.set()

    app.on_shutdown.append(on_shutdown)
    assert server.build_app()[server._AUTO_SHUTDOWN] is False
    session = Session("desktop-test")
    server.SESSIONS[session.sid] = session
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        ws = await client.ws_connect("/api/session/desktop-test/ws")
        assert app[server._DESKTOP_CLIENT_CONNECTED]
        await ws.close()
        await asyncio.wait_for(app[server._DESKTOP_SHUTDOWN_REQUESTED].wait(), timeout=1)
        assert not server.WS_CLIENTS.get("desktop-test")
    finally:
        await client.close()
        assert shutdown_callback.is_set()
        assert session.sid not in server.SESSIONS
        assert not server.WS_CLIENTS
        server._DESKTOP_CLOSE_GRACE, server._DESKTOP_POLL_INTERVAL = old_grace, old_interval


async def test_data_directory_defaults_to_project_root_in_dev_mode():
    import main as server

    frozen_present = hasattr(server.sys, "frozen")
    original_frozen = getattr(server.sys, "frozen", None)
    original_env = os.environ.pop("SCRIPTPLAYGROUND_DATA_DIR", None)
    original_data = (server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR)
    try:
        if hasattr(server.sys, "frozen"):
            del server.sys.frozen
        assert server.data_directory() == Path(__file__).resolve().parents[1]
        os.environ["SCRIPTPLAYGROUND_DATA_DIR"] = str(Path(".test-artifacts") / "data-test")
        assert server.data_directory() == (Path(".test-artifacts") / "data-test").resolve()
    finally:
        server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR = original_data
        if frozen_present:
            server.sys.frozen = original_frozen
        elif hasattr(server.sys, "frozen"):
            del server.sys.frozen
        if original_env is None:
            os.environ.pop("SCRIPTPLAYGROUND_DATA_DIR", None)
        else:
            os.environ["SCRIPTPLAYGROUND_DATA_DIR"] = original_env


async def test_frozen_data_directory_uses_windows_local_app_data():
    import main as server

    original_env = os.environ.pop("SCRIPTPLAYGROUND_DATA_DIR", None)
    frozen_present = hasattr(server.sys, "frozen")
    original_frozen = getattr(server.sys, "frozen", None)
    original_platform = server.sys.platform
    old_local = os.environ.get("LOCALAPPDATA")
    try:
        server.sys.frozen = True
        server.sys.platform = "win32"
        with tempfile.TemporaryDirectory() as directory:
            os.environ["LOCALAPPDATA"] = directory
            path = server.data_directory()
            assert path == Path(directory) / "ScriptPlayground"
            assert path.is_dir()
    finally:
        server.sys.platform = original_platform
        if frozen_present:
            server.sys.frozen = original_frozen
        else:
            del server.sys.frozen
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local
        if original_env is not None:
            os.environ["SCRIPTPLAYGROUND_DATA_DIR"] = original_env


async def test_data_dir_bootstrap_preserves_user_files():
    import main as server

    original = (server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR)
    try:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            server.configure_data_directory(data)
            server.bootstrap_default_scripts()
            assert {p.name for p in server.SCRIPTS_DIR.glob("*.py")} >= {"demo.py", "poll_bot.py"}
            assert (data / "scenarios" / "Greeting.scenario.json").is_file()
            assert (data / "scenarios" / "Profile-aware greeting.scenario.json").is_file()
            assert (data / "designs" / "Club Welcome.discordv2proj.json").is_file()
            user_script = server.SCRIPTS_DIR / "demo.py"
            user_script.write_text("# keep my version", encoding="utf-8")
            server.bootstrap_default_scripts()
            assert user_script.read_text(encoding="utf-8") == "# keep my version"
    finally:
        server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR = original


async def test_browser_process_close_uses_five_second_grace():
    import launcher

    original_grace, original_interval = launcher._BROWSER_EXIT_GRACE, launcher._BROWSER_POLL_INTERVAL
    launcher._BROWSER_EXIT_GRACE = 0.01
    launcher._BROWSER_POLL_INTERVAL = 0.001

    class FakeProcess:
        exited = False

        def poll(self):
            return 0 if self.exited else None

    process = FakeProcess()
    stop = asyncio.Event()
    task = asyncio.create_task(launcher._wait_for_browser_exit(process, stop))
    try:
        await asyncio.sleep(0.005)
        process.exited = True
        await asyncio.wait_for(task, timeout=0.2)
        assert stop.is_set()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        launcher._BROWSER_EXIT_GRACE, launcher._BROWSER_POLL_INTERVAL = original_grace, original_interval


async def test_desktop_auto_shutdown_ignores_startup_without_clients():
    from aiohttp.test_utils import TestClient, TestServer

    import main as server

    old_grace, old_interval = server._DESKTOP_CLOSE_GRACE, server._DESKTOP_POLL_INTERVAL
    server._DESKTOP_CLOSE_GRACE = 0.03
    server._DESKTOP_POLL_INTERVAL = 0.002
    app = server.build_app(auto_shutdown=True)
    client = TestClient(TestServer(app))
    try:
        await client.start_server()
        await asyncio.sleep(server._DESKTOP_CLOSE_GRACE + 0.08)
        assert not app[server._DESKTOP_CLIENT_CONNECTED]
        assert not app[server._DESKTOP_SHUTDOWN_REQUESTED].is_set()
    finally:
        await client.close()
        server._DESKTOP_CLOSE_GRACE, server._DESKTOP_POLL_INTERVAL = old_grace, old_interval


async def test_cpu_bound_startup_is_interrupted():
    """The dedicated loop must recover from a startup ``while True: pass``."""
    s = Session("t-cpu")
    try:
        result = await run_script(s, "while True:\n    pass\n", timeout=1.0)
        assert result["ok"] is False
        assert any("execution deadline" in event["text"] for event in s.events)
    finally:
        s.close()


async def test_rerun_cancels_previous_main_task():
    """A second Run must not leave the previous bot loop producing ghost messages."""
    s = Session("t-rerun")
    try:
        await run_script(s, """
import asyncio
async def main():
    await send("first")
    while True:
        await asyncio.sleep(0.01)
""", timeout=1)
        assert last_msg(s)["content"] == "first"
        await run_script(s, """
async def main():
    await send("second")
""")
        assert [s.messages[mid]["content"] for mid in s.order] == ["second"]
    finally:
        s.close()


async def test_timeline_retention_under_message_stress():
    """Long-running mocks stay bounded so state serialization remains predictable."""
    s = Session("t-retention")
    for i in range(2_500):
        s.add_message(content=str(i))
    assert len(s.order) == 2_000
    assert len(s.messages) == 2_000
    assert s.messages[s.order[0]]["content"] == "500"
    s.log("x", "noise")
    for i in range(1_200):
        s.log("x", str(i))
    assert len(s.events) == 1_000
    s.close()


async def test_parallel_session_stress():
    """Independent sessions should run concurrently without sharing timeline state."""
    sessions = [Session(f"stress-{i}") for i in range(12)]
    try:
        code = "async def main():\n    await send('ready')\n"
        results = await asyncio.gather(*(run_script(s, code) for s in sessions))
        assert all(result["ok"] for result in results)
        assert all(last_msg(s)["content"] == "ready" for s in sessions)
    finally:
        for s in sessions:
            s.close()


# --- reactions -----------------------------------------------------------------


async def test_reaction_toggle_roundtrip():
    s = Session("t-react")
    await run_script(s, "async def main():\n    await send('react to me')\n")
    mid = s.order[-1]
    my_id = s.user_id

    added = s.toggle_reaction(mid, "🔥", user_id=my_id, actor="user")
    assert added is True
    stored = next(m for m in state(s)["messages"] if m["id"] == mid)
    assert stored["reactions"] == [{"emoji": "🔥", "users": [str(my_id)]}]

    # toggling again removes the user and the empty pill
    added = s.toggle_reaction(mid, "🔥", user_id=my_id, actor="user")
    assert added is False
    stored = next(m for m in state(s)["messages"] if m["id"] == mid)
    assert stored.get("reactions", []) == []

    # a second user stacks the count; the bot reacts through MockMessage
    s.toggle_reaction(mid, "🔥", user_id=111111111111111111, actor="user")
    handle = playground.MockMessage(s, mid)
    await handle.add_reaction("🔥")
    stored = next(m for m in state(s)["messages"] if m["id"] == mid)
    assert stored["reactions"][0]["users"] == ["111111111111111111", str(playground.BOT_ID)]

    # remove_reaction only drops the caller's own reaction
    await handle.remove_reaction("🔥")
    stored = next(m for m in state(s)["messages"] if m["id"] == mid)
    assert stored["reactions"][0]["users"] == ["111111111111111111"]
    s.close()


async def test_reaction_requires_add_reactions_permission():
    s = Session("t-react-denied")
    await run_script(s, "async def main():\n    await send('no reactions here')\n")
    mid = s.order[-1]
    s.set_user(111111111111111111)  # Alice
    s.active_user.permission_override = discord.Permissions(view_channel=True, send_messages=True)
    try:
        await s.toggle_reaction(mid, "👍", user_id=111111111111111111, actor="user")
        raise AssertionError("expected discord.Forbidden")
    except discord.Forbidden:
        pass
    denied = next(e for e in reversed(s.events)
                  if e.get("details", {}).get("status") == "denied"
                  and e["details"].get("permission") == "add_reactions")
    assert denied["kind"] == "action"
    stored = next(m for m in state(s)["messages"] if m["id"] == mid)
    assert stored.get("reactions", []) == []
    s.close()


# --- discord payload limits (400 Invalid Form Body, code 50035) -----------------


async def test_oversize_content_is_rejected_like_real_discord():
    s = Session("t-limits-content")
    try:
        s.add_message("x" * 2001)
        raise AssertionError("expected discord.HTTPException")
    except discord.HTTPException as error:
        assert error.status == 400
        assert error.code == 50035
        assert "content" in str(error)
        assert not s.messages  # nothing was stored
    await run_script(s, "async def main():\n    await send('ok' * 10)\n")
    assert len(s.messages) == 1  # normal sends still work
    s.close()


async def test_embed_limit_violations_are_rejected():
    s = Session("t-limits-embed")
    try:
        fat = discord.Embed(title="t" * 257)
        s.add_message(embed=fat)
        raise AssertionError("expected discord.HTTPException")
    except discord.HTTPException as error:
        assert error.status == 400 and error.code == 50035
        assert "title" in str(error)
    try:
        crowded = discord.Embed()
        for i in range(26):
            crowded.add_field(name=f"f{i}", value="v")
        s.add_message(embed=crowded)
        raise AssertionError("expected discord.HTTPException")
    except discord.HTTPException:
        pass
    s.add_message(embed=discord.Embed(title="fine", description="d" * 4096))  # at-limit passes
    assert len(s.messages) == 1
    s.close()


async def test_component_limit_violations_are_rejected():
    s = Session("t-limits-components")
    try:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="L" * 81, custom_id="big"))
        s.add_message(view=view)
        raise AssertionError("expected discord.HTTPException")
    except discord.HTTPException as error:
        assert error.status == 400 and error.code == 50035
        assert "label" in str(error)
    # (6-buttons-per-row is unconstructible through discord.ui.View itself —
    # discord.py raises "item would not fit at row 0" client-side — so the
    # mock's row guard only ever fires for hand-built serialized trees.)
    ok = discord.ui.View()
    ok.add_item(discord.ui.Button(label="ok", custom_id="ok", row=0))
    s.add_message(view=ok)
    assert len(s.messages) == 1
    s.close()


# --- simulated voice / uploads / moderation -------------------------------------


async def test_voice_simulation_roundtrip():
    s = Session("t-voice")
    try:
        s.voice_action("join", channel_id="999999")
        raise AssertionError("expected ValueError for unknown channel")
    except ValueError:
        pass
    s.voice_action("join", channel_id=str(s.channel.id))
    voice = state(s)["voice"]
    assert voice["channel"] == str(s.channel.id) and voice["name"] == "playground"
    s.voice_action("deafen")
    voice = state(s)["voice"]
    assert voice["self_deaf"] and voice["self_mute"]
    s.voice_action("unmute")  # unmute also shows the speaking ring
    voice = state(s)["voice"]
    assert not voice["self_mute"] and voice["speaking"] == [str(s.user_id)]
    joined = [e for e in s.events if e.get("details", {}).get("operation") == "voice.simulate"]
    assert len(joined) >= 3
    s.voice_action("leave")
    voice = state(s)["voice"]
    assert voice["channel"] is None and voice["speaking"] == []
    try:
        s.voice_action("dance")
        raise AssertionError("expected ValueError for unknown action")
    except ValueError:
        pass
    s.close()


async def test_uploads_reach_on_message_attachments():
    s = Session("t-uploads")
    await run_script(s, "import json\n"
                        "async def on_message(message):\n"
                        "    await send(f\"got {len(message.attachments)} file(s): {message.attachments[0]['name']}\")\n")
    entry = s.add_upload("photo.png", 1024, "image/png", "data:image/png;base64,AAAA")
    assert entry["name"] == "photo.png"
    assert any(e.get("details", {}).get("operation") == "message.attachment" for e in s.events)
    await dispatch_message(s, "here is my upload",
                           files=[{"name": "photo.png", "data_uri": "data:image/png;base64,AAAA"}])
    stored = next(m for m in state(s)["messages"] if m["content"] == "here is my upload")
    assert stored["files"][0]["name"] == "photo.png"
    assert stored["files"][0]["data_uri"].startswith("data:image/png")
    replied = next(m for m in state(s)["messages"] if "got 1 file(s)" in m["content"])
    assert "photo.png" in replied["content"]
    assert state(s)["uploads"][0]["size"] == 1024
    s.close()


async def test_moderation_permissions_and_roundtrip():
    s = Session("t-moderation")
    carol = 333333333333333333
    alice = 111111111111111111
    # a member without moderation perms is refused with a denied log entry
    s.set_user(alice)
    s.active_user.permission_override = discord.Permissions(view_channel=True, send_messages=True)
    try:
        s.kick_member(carol)
        raise AssertionError("expected discord.Forbidden")
    except discord.Forbidden:
        pass
    assert s.guild.get_member(carol) is not None
    # the owner can kick; a kicked member is simply gone (kick ≠ ban)
    s.set_user(123456789012345678)
    s.kick_member(carol)
    assert s.guild.get_member(carol) is None
    # re-add via the public add path to prove members can come back
    carol2 = s.add_member("Carol").id
    assert s.guild.get_member(carol2) is not None
    # ban removes the member and registers them for unban (unban recreates them)
    s.ban_member(carol2)
    assert s.guild.get_member(carol2) is None
    s.unban_member(carol2)
    assert s.guild.get_member(carol2) is not None
    # timeout shows in member_details and clears at zero minutes
    s.timeout_member(carol2, 10)
    assert next(m for m in state(s)["member_details"] if m["id"] == str(carol2))["timed_out"] is True
    s.timeout_member(carol2, 0)
    assert next(m for m in state(s)["member_details"] if m["id"] == str(carol2))["timed_out"] is False
    # bots and the owner are protected
    try:
        s.ban_member(playground.BOT_ID)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        s.ban_member(123456789012345678)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    kicked = [e for e in s.events if e.get("details", {}).get("operation") in
              ("member.kick", "member.ban", "member.timeout", "member.unban")]
    assert len(kicked) >= 5
    s.close()


async def test_gateway_member_join_reaches_module_handler():
    """Add-user mutation + dispatch_event -> on_member_join gets the MockMember."""
    s = Session("t-gw-join")
    try:
        result = await run_script(s, '''
SEEN = []
async def on_member_join(member):
    SEEN.append(member.name)
    await send(f"welcome {member.name}")
''')
        assert result["ok"], s.last_run
        member = s.add_member("Newbie")
        await dispatch_event(s, "member_join", {"user_id": member.id})
        assert "Newbie" in s.env["SEEN"]
        actions = [e for e in s.events if e["kind"] == "action"]
        assert any(a["details"].get("content") == "welcome Newbie" for a in actions)
        event = [e for e in s.events if e.get("details", {}).get("event") == "member_join"][-1]
        assert event["kind"] == "event" and event["details"]["status"] == "success"
        for key in ("actor", "target", "channel", "summary"):
            assert key in event["details"], event["details"]
    finally:
        s.close()


async def test_gateway_member_remove_receives_captured_member():
    """on_member_remove fires with the stale member object (leave via mutation)."""
    s = Session("t-gw-leave")
    try:
        result = await run_script(s, '''
NAMES = []
async def on_member_remove(member):
    NAMES.append(member.name)
''')
        assert result["ok"], s.last_run
        member = s.add_member("Leaver")
        captured = s.remove_member(member.id)
        await dispatch_event(s, "member_remove", {"_member": captured})
        assert s.env["NAMES"] == ["Leaver"]
        assert s.guild.get_member(member.id) is None  # really gone from the guild
    finally:
        s.close()


async def test_gateway_channel_and_role_create_events():
    """Channel/role creation dispatches the matching on_guild_* handlers."""
    s = Session("t-gw-create")
    try:
        result = await run_script(s, '''
CHANNELS = []
ROLES = []
async def on_guild_channel_create(channel):
    CHANNELS.append(channel.name)
async def on_guild_role_create(role):
    ROLES.append(role.name)
''')
        assert result["ok"], s.last_run
        channel = s.create_text_channel_ui("event-bus")
        role = s.create_role("Boosters")
        await dispatch_event(s, "guild_channel_create", {"channel_id": channel.id})
        await dispatch_event(s, "guild_role_create", {"_role": role})
        assert s.env["CHANNELS"] == ["event-bus"]
        assert s.env["ROLES"] == ["Boosters"]
    finally:
        s.close()


async def test_gateway_raw_reaction_payload_fields():
    """RawReactionActionEvent carries the right message_id, emoji, user_id, event_type."""
    s = Session("t-gw-react")
    try:
        result = await run_script(s, '''
PAYLOADS = []
async def on_raw_reaction_add(payload):
    PAYLOADS.append((payload.message_id, payload.user_id, str(payload.emoji), payload.event_type))
async def on_raw_reaction_remove(payload):
    PAYLOADS.append((payload.message_id, payload.user_id, str(payload.emoji), payload.event_type))
''')
        assert result["ok"], s.last_run
        member = s.add_member("Reactor")
        handle = await s.channel.send("react here")
        mid = handle.id
        await dispatch_event(s, "raw_reaction_add", {"message_id": mid, "user_id": member.id, "emoji": "🔥"})
        await dispatch_event(s, "raw_reaction_remove", {"message_id": mid, "user_id": member.id, "emoji": "🔥"})
        adds, removes = s.env["PAYLOADS"]
        assert adds == (int(mid[1:]), member.id, "🔥", "REACTION_ADD"), adds
        assert removes[3] == "REACTION_REMOVE" and removes[:3] == adds[:3]
        assert adds[0] == 1 and adds[1] == member.id
    finally:
        s.close()


async def test_gateway_event_without_handler_logs_and_survives():
    """A no-handler event logs a clear no_handler action and does not raise."""
    s = Session("t-gw-noop")
    try:
        result = await run_script(s, "x = 1")
        assert result["ok"], s.last_run
        role = s.create_role("Doomed")
        await dispatch_event(s, "guild_role_delete", {"_role": role})
        last = s.events[-1]
        assert last["kind"] == "action" and last["details"]["status"] == "no_handler"
        assert "no handler defined" in last["text"]
        assert s.last_run["ok"]  # nothing raised
    finally:
        s.close()


async def test_gateway_cog_listener_fires_for_member_join():
    """Slice 1's registered cog listeners now receive gateway events."""
    s = Session("t-gw-cog")
    try:
        result = await run_script(s, '''
import discord
from discord.ext import commands

class Greeter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    @commands.Cog.listener()
    async def on_member_join(self, member):
        await send(f"cog greets {member.name}")

async def setup(bot):
    await bot.add_cog(Greeter(bot))
''')
        assert result["ok"], s.last_run
        member = s.add_member("CogJoiner")
        await dispatch_event(s, "member_join", {"user_id": member.id})
        actions = [e for e in s.events if e["kind"] == "action"]
        assert any(a["details"].get("content") == "cog greets CogJoiner" for a in actions)
        assert not any(a["details"].get("status") == "no_handler" for a in actions)
    finally:
        s.close()


async def test_gateway_member_update_roles_snapshot():
    """on_member_update receives (before, after) role states around a mutation."""
    s = Session("t-gw-update")
    try:
        result = await run_script(s, '''
CHANGES = []
async def on_member_update(before, after):
    CHANGES.append(([r.name for r in before.roles], [r.name for r in after.roles]))
''')
        assert result["ok"], s.last_run
        member = s.add_member("RoleTarget")
        from playground import _member_before_snapshot
        moderators = next(r for r in s.guild.roles if r.name == "Moderators")
        before = _member_before_snapshot(member)
        s.grant_role(member.id, moderators.id)
        await dispatch_event(s, "member_update", {"user_id": member.id, "_before": before})
        before_names, after_names = s.env["CHANGES"][-1]
        assert "Moderators" not in before_names and "Moderators" in after_names
        event = [e for e in s.events if e.get("details", {}).get("event") == "member_update"][-1]
        assert event["details"]["roles_before"] == []
        assert event["details"]["roles_after"] == ["Moderators"]
    finally:
        s.close()


async def test_gateway_profile_nickname_edit_fires_member_update():
    """PUT members/{id}/profile with a new display_name fires on_member_update."""
    s = Session("t-gw-nickname")
    import main as server

    server.SESSIONS[s.sid] = s
    try:
        result = await run_script(s, '''
CHANGES = []
async def on_member_update(before, after):
    CHANGES.append((before.display_name, after.display_name))
''')
        assert result["ok"], s.last_run
        member = s.add_member("NickTarget")
        request = _FakeReq(match={"sid": s.sid, "user_id": str(member.id)},
                           body={"display_name": "NickChanged"})
        response = await server.update_member_profile(request)
        import json

        body = json.loads(response.body)
        assert body["ok"], body
        import time

        deadline = time.monotonic() + 1.0
        while server._PENDING_EVENT_TASKS and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert s.env["CHANGES"] == [("NickTarget", "NickChanged")], s.env["CHANGES"]
        event = [e for e in s.events if e.get("details", {}).get("event") == "member_update"][-1]
        assert event["details"]["nickname_before"] == "NickTarget"
        assert event["details"]["nickname_after"] == "NickChanged"
        assert event["details"]["status"] == "success"
        # Unchanged display_name must NOT fire the event again.
        request2 = _FakeReq(match={"sid": s.sid, "user_id": str(member.id)},
                            body={"display_name": "NickChanged"})
        response2 = await server.update_member_profile(request2)
        assert json.loads(response2.body)["ok"]
        updates = [e for e in s.events if e.get("details", {}).get("event") == "member_update"]
        assert len(updates) == 1
    finally:
        server.SESSIONS.pop(s.sid, None)
        s.close()


async def test_gateway_reaction_route_fires_raw_event_end_to_end():
    """HTTP reaction route: toggle -> awaited raw event -> handler -> state."""
    s = Session("t-gw-route")
    import main as server

    server.SESSIONS[s.sid] = s
    try:
        result = await run_script(s, '''
REACTIONS = []
async def on_raw_reaction_add(payload):
    REACTIONS.append((str(payload.emoji), payload.user_id))
''')
        assert result["ok"], s.last_run
        handle = await s.channel.send("route target")
        import main as server

        request = _FakeReq(match={"sid": s.sid},
                           body={"message_id": handle.id, "emoji": "🎉"})
        response = await server.react(request)
        import json

        body = json.loads(response.body)
        assert body["ok"], body
        assert body["added"] is True
        assert s.env["REACTIONS"] == [("🎉", s.user_id)]
        event = [e for e in s.events if e.get("details", {}).get("event") == "raw_reaction_add"][-1]
        assert event["details"]["status"] == "success"
    finally:
        server.SESSIONS.pop(s.sid, None)
        s.close()


async def test_gateway_reaction_add_remove_constructed_event():
    """UI reaction toggle fires on_reaction_add/remove with a real discord.Reaction."""
    s = Session("t-gw-rx-nonraw")
    import main as server

    server.SESSIONS[s.sid] = s
    try:
        result = await run_script(s, '''
SEEN = []
async def on_reaction_add(reaction, user):
    SEEN.append(("add", str(reaction.emoji), reaction.count, reaction.me, user.name,
                 reaction.message.content, type(reaction).__name__))
async def on_reaction_remove(reaction, user):
    SEEN.append(("remove", str(reaction.emoji), user.name))
''')
        assert result["ok"], s.last_run
        handle = await s.channel.send("reaction target")
        await server.react(_FakeReq(match={"sid": s.sid},
                                    body={"message_id": handle.id, "emoji": "⭐"}))
        await _drain_event_tasks(server)
        assert s.env["SEEN"] == [("add", "⭐", 1, False, "You", "reaction target", "Reaction")], s.env["SEEN"]
        await server.react(_FakeReq(match={"sid": s.sid},
                                    body={"message_id": handle.id, "emoji": "⭐"}))
        await _drain_event_tasks(server)
        assert s.env["SEEN"][-1] == ("remove", "⭐", "You"), s.env["SEEN"]
        event = [e for e in s.events if e.get("details", {}).get("event") == "reaction_add"][-1]
        assert event["details"]["status"] == "success"
    finally:
        server.SESSIONS.pop(s.sid, None)
        s.close()


async def test_message_payload_includes_reactions_and_references():
    """REST message payloads carry reaction state; replies resolve their reference."""
    s = Session("t-slice3-payloads")
    try:
        assert (await run_script(s, "READY = 1"))["ok"], s.last_run
        handle = await s.channel.send("base message")
        await handle.add_reaction("🎉")
        s.add_member("Replier")
        s.add_message("the reply", reference=handle.id,
                      author=s.guild.get_member(s.next_custom_user_id - 1))

        from bot_runtime import ProjectRuntime, ProjectTransport
        runtime = ProjectRuntime.__new__(ProjectRuntime)
        runtime.session = s
        transport = ProjectTransport.__new__(ProjectTransport)
        transport.runtime = runtime
        payload = transport._message_payload(s.messages[handle.id])
        assert payload["reactions"] == [{"count": 1, "me": True,
                                         "count_details": {"burst": 0, "normal": 1},
                                         "emoji": {"name": "🎉", "animated": False, "id": None}}]
        reply_stored = next(m for m in s.messages.values() if m.get("reference"))
        reply_payload = transport._message_payload(reply_stored)
        assert reply_payload["type"] == 19
        assert reply_payload["referenced_message"]["content"] == "base message"

        # The real constructed Message resolves the reply through the shim state.
        from playground import _real_message
        real_reply = _real_message(s, reply_stored)
        assert real_reply.type.name == "reply"
        assert real_reply.reference.resolved.content == "base message"
        # The mock handle exposes the same reference through MockMessage.referenced_message.
        from playground import MockMessage
        mock_reply = MockMessage(s, reply_stored["id"], reply_stored.get("author_obj"))
        assert mock_reply.referenced_message is not None
        assert mock_reply.referenced_message.content == "base message"
    finally:
        s.close()


async def _drain_event_tasks(server, timeout: float = 2.0) -> None:
    """Wait until fire-and-notify event tasks finish (they hop threads)."""
    import time

    deadline = time.monotonic() + timeout
    while server._PENDING_EVENT_TASKS and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


async def test_v2_component_own_callback_and_modal():
    """V2 section buttons fire their own callback; ui.Modal subclasses own submits."""
    s = Session("t-v2-advanced")
    try:
        result = await run_script(s, '''
import discord
from discord import ui

class TicketModal(ui.Modal, title="Open a ticket"):
    topic = ui.TextInput(label="Topic", max_length=60)
    async def on_submit(self, interaction):
        await interaction.response.send_message(f"ticket: {self.topic.value}", ephemeral=True)

class OpenButton(ui.Button):
    async def callback(self, interaction):
        await interaction.response.send_modal(TicketModal())

class Panel(ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        container = ui.Container(accent_colour=0x5865f2)
        container.add_item(ui.Section(ui.TextDisplay("Own cb"),
            accessory=ui.Button(label="Ping", style=discord.ButtonStyle.primary, custom_id="ping")))
        container.add_item(ui.Section(ui.TextDisplay("Modal"),
                                       accessory=OpenButton(label="Modal", custom_id="modal")))
        self.add_item(container)

async def main():
    await send(view=Panel())

async def on_click(interaction, custom_id, values):
    await interaction.response.send_message(f"fallback {custom_id}", ephemeral=True)
''')
        assert result["ok"], s.last_run
        msg = last_msg(s)
        assert msg["v2"][0]["v2"] == "container"
        assert msg["v2"][0]["children"][1]["accessory"]["kind"] == "button"

        # A plain v2 button with no own callback still goes through on_click.
        await dispatch_click(s, msg["id"], "ping", [])
        actions = [a for a in s.events if a["kind"] == "action"]
        assert actions[-1]["details"].get("content") == "fallback ping"

        # A subclassed v2 section accessory fires ITS callback -> opens the modal.
        await dispatch_click(s, msg["id"], "modal", [])
        modal = next(m for m in s.modals if not m.get("dismissed"))
        assert modal["title"] == "Open a ticket"
        cid = modal["items"][0]["custom_id"]

        # Submitting runs the ui.Modal subclass's on_submit with the value on
        # the TextInput child, exactly like real discord.py.
        await dispatch_submit(s, modal["id"], {cid: "linux crash"})
        actions = [a for a in s.events if a["kind"] == "action"]
        assert actions[-1]["details"].get("content") == "ticket: linux crash"
    finally:
        s.close()


async def main() -> None:
    tests = [
        (name, fn) for name, fn in sorted(globals().items())
        if name.startswith("test_") and asyncio.iscoroutinefunction(fn)
    ]
    failures = []
    for name, fn in tests:
        try:
            await fn()
            print(f"PASS {name}")
        except Exception as err:  # noqa: BLE001 - harness: record and continue
            failures.append(name)
            print(f"FAIL {name}: {err!r}")
    if failures:
        print(f"\n{len(failures)} failed, {len(tests) - len(failures)} passed")
        sys.exit(1)
    print(f"\nall {len(tests)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
