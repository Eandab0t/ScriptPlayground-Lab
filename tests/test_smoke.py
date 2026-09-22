"""Offline smoke tests for the playground mock layer.

Run from the project root:

    python -m tests.test_smoke

Uses throwaway Session objects; no server, no network, no files touched.
"""

import asyncio
import sys

import discord

import playground
from playground import (
    Session,
    dispatch_click,
    dispatch_command,
    dispatch_message,
    dispatch_submit,
    run_script,
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
    assert any("no `on_click`" in e["text"] for e in s.events[-2:])


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
    assert any("never answered" in e["text"] for e in s.events[-2:])


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
    assert any("not defined" in e["text"] for e in s.events[before:])
    await run_script(s, "async def main():\n    pass")  # no commands in this script
    assert s.commands == {} and s.cmd_objects == {}


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

    def __init__(self, match=None, body=None):
        self.match_info = match or {}
        self._body = body or {}

    async def json(self):
        return self._body


async def test_script_library_roundtrip():
    import json
    import pathlib
    import tempfile

    import main as server

    with tempfile.TemporaryDirectory() as td:
        orig = server.SCRIPTS_DIR
        server.SCRIPTS_DIR = pathlib.Path(td)
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
            server.SCRIPTS_DIR = orig


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
            (folder / "bot.py").write_text(source, encoding="utf-8")
            listing = json.loads((await server.list_workspaces(None)).body)["workspaces"]
            assert listing == [{"name": "my_bot", "files": ["bot.py"]}]
            request = _FakeReq(match={"workspace": "my_bot", "filename": "bot.py"})
            assert json.loads((await server.get_workspace_file(request)).body)["code"] == source
            saved = await server.save_workspace_file(_FakeReq(
                match=request.match_info, body={"code": "updated = True\n"}
            ))
            assert json.loads(saved.body)["ok"] is True
            assert (folder / "bot.py").read_text(encoding="utf-8") == "updated = True\n"
            try:
                await server.save_workspace_file(_FakeReq(
                    match={"workspace": "../bad", "filename": "bot.py"}, body={"code": "x"}
                ))
            except server.web.HTTPBadRequest:
                pass
            else:
                raise AssertionError("workspace traversal was accepted")
        finally:
            server.WORKSPACES_DIR = original


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


async def test_embeder_provenance_endpoint():
    import json
    import re

    import main as server

    marker_text = (server.EMBEDER_DIR / "VENDORED_FROM.txt").read_text(encoding="utf-8")
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
