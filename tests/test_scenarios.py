"""Focused behavior checks for replayable scenarios and editor error locations."""

import asyncio
import json
import tempfile
from pathlib import Path

import main as server
from playground import Session, run_scenario, run_script, validate_scenario


class FakeRequest:
    def __init__(self, *, match=None, body=None):
        self.match_info = match or {}
        self._body = body or {}

    async def json(self):
        return self._body


async def test_scenario_dispatches_as_actor_and_assertions():
    session = Session("scenario-flow")
    try:
        await run_script(session, """
import discord
async def main():
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open", custom_id="open"))
    embed = discord.Embed(title="Ticket")
    embed.add_field(name="Status", value="open")
    await send(embed=embed, view=view)
async def on_click(interaction, custom_id, values):
    channel = await interaction.guild.create_text_channel("ticket-1")
    await interaction.response.send_message(f"Created #{channel.name}")
async def on_message(message):
    if message.content == "hello":
        await message.reply(f"Hello, {message.author.name}!")
""")
        initial_id = session.order[-1]
        result = await run_scenario(session, {
            "version": 1,
            "name": "Ticket flow",
            "steps": [
                {"assert": "embed_field", "message_id": initial_id,
                 "name": "Status", "value": "open"},
                {"assert": "component_exists", "message_id": initial_id, "custom_id": "open"},
                {"action": "click", "message_id": initial_id, "custom_id": "open", "as": "Alice"},
                {"assert": "channel_exists", "name": "ticket-1"},
                {"action": "message", "content": "hello", "as": "Alice", "channel": "ticket-1"},
                {"assert": "message_exists", "content": "Hello, Alice!", "channel": "ticket-1"},
                {"assert": "event_occurred", "interaction": "component", "custom_id": "open"},
            ],
        })
        assert result["ok"] is True, result
        assert [step["passed"] for step in result["results"]] == [True] * 7
        assert result["results"][2]["message_id"] == initial_id
        assert session.active_user.name == "Alice"
        created = session.messages[result["results"][4]["message_id"]]
        assert created["author"]["name"] == "Alice"
        action = next(event for event in session.events if event["kind"] == "action"
                      and event.get("details", {}).get("operation") == "channel.send"
                      and event.get("details", {}).get("content") == "Created #ticket-1")
        event = next(entry for entry in session.events if entry["id"] == action["event_id"])
        assert action["id"] in event["action_ids"]
    finally:
        session.close()


async def test_scenario_failure_stops_at_step_with_runtime_state():
    session = Session("scenario-failure")
    try:
        await run_script(session, "async def main():\n    await send('ready')\nasync def on_message(message):\n    pass\n")
        result = await run_scenario(session, {
            "version": 1,
            "name": "Fail clearly",
            "steps": [
                {"action": "message", "content": "!unknown", "as": "Bob"},
                {"assert": "content", "equals": "expected"},
                {"assert": "message_exists"},
            ],
        })
        assert result["ok"] is False, result
        assert result["failed_step"] == 2, result
        assert len(result["results"]) == 2, result
        assert result["runtime_state"]["active_user"] == "Bob", result
        messages = result["runtime_state"]["messages"]
        contents = [message["content"] for message in messages]
        assert "ready" in contents and "!unknown" in contents, result
        assert "!unknown" in result["results"][1]["actual"], result
        assert result["runtime_state"]["last_run"]["ok"] is True
    finally:
        session.close()


async def test_scenario_submits_modal_and_dispatches_command():
    session = Session("scenario-interactions")
    try:
        await run_script(session, """
import discord
from discord import app_commands
modal = discord.ui.Modal(title="Name")
modal.add_item(discord.ui.TextInput(label="Name", custom_id="name"))
@app_commands.command(name="echo")
async def echo(interaction, text: str):
    await interaction.response.send_message(f"command:{text}")
async def main():
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open", custom_id="open"))
    await send(view=view)
async def on_click(interaction, custom_id, values):
    await interaction.response.send_modal(modal)
async def on_submit(interaction, values, modal_id):
    await interaction.response.send_message(f"submitted:{values['name']}")
""")
        message_id = session.order[-1]
        result = await run_scenario(session, {
            "version": 1,
            "name": "Modal and command",
            "steps": [
                {"action": "click", "message_id": message_id, "custom_id": "open", "as": "Alice"},
                {"action": "submit", "modal_id": "latest", "values": {"name": "Alice"}},
                {"assert": "content", "equals": "submitted:Alice"},
                {"action": "command", "name": "echo", "args": {"text": "hello"}, "as": "Bob"},
                {"assert": "content", "equals": "command:hello"},
                {"assert": "event_occurred", "interaction": "modal_submit"},
                {"assert": "event_occurred", "interaction": "application_command"},
            ],
        })
        assert result["ok"] is True, result
        assert [step["passed"] for step in result["results"]] == [True] * 7
        assert session.active_user.name == "Bob"
        modal_event = next(event for event in session.events
                           if event.get("details", {}).get("interaction") == "modal_submit")
        assert any(action.get("event_id") == modal_event["id"] for action in session.events)
    finally:
        session.close()


async def test_scenario_click_requires_an_enabled_component():
    session = Session("scenario-invalid-click")
    try:
        await run_script(session, """
import discord
async def main():
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Disabled", custom_id="disabled", disabled=True))
    await send(view=view)
""")
        message_id = session.order[-1]
        result = await run_scenario(session, {
            "version": 1,
            "name": "Disabled click",
            "steps": [{"action": "click", "message_id": message_id, "custom_id": "disabled"}],
        })
        assert result["ok"] is False and result["failed_step"] == 1, result
        assert "disabled" in result["results"][0]["actual"].lower()
        assert not any(event.get("details", {}).get("interaction") == "component"
                       for event in session.events)
    finally:
        session.close()


async def test_scenario_handler_error_fails_with_runtime_context():
    session = Session("scenario-script-error")
    try:
        await run_script(session, "async def main():\n    pass\nasync def on_message(message):\n    raise LookupError('callback boom')\n")
        result = await run_scenario(session, {
            "version": 1,
            "name": "Callback failure",
            "steps": [{"action": "message", "content": "trigger", "as": "Alice"}],
        })
        assert result["ok"] is False and result["failed_step"] == 1, result
        assert "LookupError: callback boom" in result["results"][0]["actual"], result
        assert result["results"][0]["expected"].endswith("without a simulator denial, missing handler, or script error"), result
        assert result["runtime_state"]["messages"][-1]["content"] == "trigger", result
        assert session.last_run["exception"] == {
            "type": "LookupError", "message": "callback boom", "file": "<playground>", "line": 4,
        }, result
        assert any(event.get("details", {}).get("status") == "script_error"
                   for event in result["runtime_state"]["recent_events"]), result
    finally:
        session.close()


async def test_scenario_validation_rejects_unknown_or_malformed_steps():
    base = {"version": 1, "name": "bad", "steps": [{"action": "message", "content": "hello"}]}
    assert validate_scenario(base)["name"] == "bad"
    for invalid in (
        {**base, "version": 2},
        {**base, "steps": [{"action": "voice", "user": "Alice"}]},
        {**base, "steps": [{"action": "click", "message_id": "latest", "custom_id": "x", "values": {"bad": "x"}}]},
        {**base, "steps": [{"assert": "event_occurred"}]},
    ):
        try:
            validate_scenario(invalid)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"invalid scenario was accepted: {invalid!r}")


async def test_starter_greeting_scenario_passes_with_demo():
    project = Path(__file__).resolve().parent.parent
    session = Session("starter-greeting")
    try:
        await run_script(session, (project / "scripts" / "demo.py").read_text(encoding="utf-8"))
        scenario = json.loads((project / "scripts" / "scenarios" / "Greeting.scenario.json").read_text(encoding="utf-8"))
        sample = (project / "static" / "index.html").read_text(encoding="utf-8")
        assert '{"assert": "message_exists", "content": "Hello, <@111111111111111111>!"}' in sample
        result = await run_scenario(session, scenario)
        assert result["ok"] is True, result
        assert all(step["passed"] for step in result["results"]), result
    finally:
        session.close()


async def test_scenario_file_routes_roundtrip_and_validate():
    with tempfile.TemporaryDirectory() as directory:
        original = (server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR)
        server.DATA_DIR = Path(directory)
        server.SCRIPTS_DIR = Path(directory)
        server.WORKSPACES_DIR = Path(directory) / "bots"
        scenario = {"version": 1, "name": "roundtrip", "steps": [
            {"action": "message", "content": "!hello", "as": "Alice"},
            {"assert": "message_exists", "content": "Hello"},
        ]}
        try:
            saved = json.loads((await server.save_scenario(FakeRequest(body={"scenario": scenario}))).body)
            assert saved == {"ok": True, "name": "roundtrip", "existed": False}
            path = Path(directory) / "scenarios" / "roundtrip.scenario.json"
            assert path.read_text(encoding="utf-8").endswith("\n")
            listed = json.loads((await server.list_scenarios(None)).body)
            assert listed["scenarios"] == [{"name": "roundtrip", "steps": 2}]
            loaded = await server.get_scenario(FakeRequest(match={"name": "roundtrip"}))
            assert json.loads(loaded.body) == scenario
            invalid = await server.save_scenario(FakeRequest(body={"scenario": {**scenario, "version": 3}}))
            assert invalid.status == 400
        finally:
            server.DATA_DIR, server.SCRIPTS_DIR, server.WORKSPACES_DIR = original


async def test_script_error_maps_user_frame_and_syntax_line():
    session = Session("error-locations")
    try:
        runtime = await run_script(session, "async def main():\n    await send('before')\n    raise ValueError('boom')\n")
        assert runtime["ok"] is False
        assert session.last_run["exception"] == {
            "type": "ValueError", "message": "boom", "file": "<playground>", "line": 3,
        }
        syntax = await run_script(session, "async def main():\n  broken =\n")
        assert syntax["ok"] is False
        assert session.last_run["exception"]["type"] == "SyntaxError"
        assert session.last_run["exception"]["file"] == "<playground>"
        assert session.last_run["exception"]["line"] == 2
    finally:
        session.close()


async def main():
    tests = [
        test_scenario_dispatches_as_actor_and_assertions,
        test_scenario_failure_stops_at_step_with_runtime_state,
        test_scenario_submits_modal_and_dispatches_command,
        test_scenario_click_requires_an_enabled_component,
        test_scenario_handler_error_fails_with_runtime_context,
        test_scenario_validation_rejects_unknown_or_malformed_steps,
        test_starter_greeting_scenario_passes_with_demo,
        test_scenario_file_routes_roundtrip_and_validate,
        test_script_error_maps_user_frame_and_syntax_line,
    ]
    failures = []
    for test in tests:
        try:
            result = test()
            if asyncio.iscoroutine(result):
                await result
            print(f"PASS {test.__name__}")
        except Exception as error:  # noqa: BLE001 - tiny standalone regression harness
            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {error!r}")
    if failures:
        raise SystemExit(1)
    print(f"all {len(tests)} focused checks passed")


if __name__ == "__main__":
    asyncio.run(main())
