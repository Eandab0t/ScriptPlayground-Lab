"""Smoke: boot E_XPV6 fully offline and dispatch a real slash command."""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot_runtime

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("discord", "discord.gateway", "discord.client", "discord.http", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.ERROR)


def dump_events(session) -> None:
    for event in session.events:
        line = f"{event['icon']} {event['text']}"
        if event.get("cls"):
            line = f"[{event['cls']}] {line}"
        details = event.get("details") or {}
        if details.get("traceback"):
            line += "\n" + details["traceback"]
        print("  EVENT:", line[:800])


async def main() -> None:
    import playground as pg

    name = sys.argv[1] if len(sys.argv) > 1 else "E_XPV6"
    session = pg.Session("smoke")
    workspace = Path(__file__).resolve().parent.parent / "bots" / name
    try:
        runtime = await asyncio.wait_for(bot_runtime.run_project(session, workspace), timeout=60)
    except BaseException as error:
        print("BOOT FAILED:", type(error).__name__, error)
        dump_events(session)
        raise
    print("STATUS:", runtime.status())
    print("EVENTS AFTER BOOT:")
    dump_events(session)

    commands = runtime.commands_payload()
    print("COMMANDS:", sorted(commands))
    if getattr(runtime, "mode", "python") == "node":
        for name in list(commands)[:2]:
            before = len(session.order)
            await runtime.dispatch_command(name, {})
            after = len(session.order)
            print(f"DISPATCH /{name}: messages {before} -> {after}")
            for stored_id in session.order[before:after]:
                msg = session.messages[stored_id]
                print("  REPLY:", msg["author"]["name"], "·",
                      (msg["content"] or (msg["embeds"] and msg["embeds"][0].get("title")) or "?")[:100])
        await runtime.dispatch_message("<@987654321098765432> you suck bot")
        print("AFTER MENTION MESSAGE: total", len(session.order))
        await runtime.dispatch_message("i love pineapple on pizza")
        print("AFTER RAGEBAIT MESSAGE: total", len(session.order))
        await runtime.shutdown()
        session.close()
        return
    target = next((name for name, spec in commands.items()
                   if not any(p["required"] for p in spec["params"])), None)
    if target:
        before = len(session.order)
        await runtime.dispatch_command(target, {})
        after = len(session.order)
        print(f"DISPATCH /{target}: messages {before} -> {after}")
        for stored_id in session.order[before:after]:
            msg = session.messages[stored_id]
            print("  REPLY:", msg["author"]["name"], "·", (msg["content"] or "(embed)")[:120])
        if before == after:
            dump_events(session)
    else:
        print("NO zero-arg command found; skipping dispatch")

    await runtime.dispatch_message("hello bot")
    print("AFTER MESSAGE: total", len(session.order))

    await runtime.shutdown()
    session.close()


if __name__ == "__main__":
    asyncio.run(main())
