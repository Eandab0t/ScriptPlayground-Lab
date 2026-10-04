# ScriptPlayground

[![CI](https://github.com/Eandab0t/ScriptPlayground-Lab/actions/workflows/ci.yml/badge.svg)](https://github.com/Eandab0t/ScriptPlayground-Lab/actions/workflows/ci.yml)

A **local Discord bot UI playground** with no Discord connection by default. The simulator makes no Discord API calls; optional OAuth signs in with `identify`, and Embeder's explicit webhook test-send posts to the URL you enter. Scripts run as ordinary Python with your account's permissions—not sandboxed—and can access local files, the network, and available credentials. Run only code you trust. The UI uses Discord's dark palette and `gg sans`/Discord's standard fallback font stack. The chat shell includes an informational member rail, animated message/channel surfaces, and a reduced-motion fallback; editor and developer tools remain ScriptPlayground surfaces.

Write plain `discord.py` code in the web editor, press **Run**, and see exactly what your bot would post: embeds, buttons, select menus, and modals, rendered in a Discord-styled chat. Then use the UI: click the buttons, pick from the selects, fill in the modals, or type `/` for the **slash-command palette** — your code's handlers run against mocked `Interaction` objects.

```
┌─────────────┬──────────────────────┬──────────────────┐
│  channels   │  # playground        │  members │ bot.py │
│  (live —    │  the viewed          │  live    │ your code│
│  switch!)   │  channel, live       │          │ console  │
└─────────────┴──────────────────────┴──────────────────┘
```

## Quick start

Requires Python 3.9 or newer. From the project root:

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py            # opens http://127.0.0.1:8741 in your browser
```

Options: `--port 8741`, `--host 127.0.0.1`, `--no-browser`. If you change the port or host, use that address for the UI and `/embeder` (for example, `http://127.0.0.1:8742/embeder`). The packaged exe accepts `--port` and `--data-dir` but not `--no-browser`; set the environment variable `SCRIPTPLAYGROUND_NO_BROWSER=1` instead.

### Browser regression tests

Install the browser-test dependencies and Chromium once, then run the user-switching/profile-editing UI checks:

```bash
npm install
npx playwright install chromium
npm run test:browser
```

Playwright starts a local simulator server for the tests; no Discord account or external service is used.

## Desktop app (Electron)

`npm install && npm start` runs ScriptPlayground as a **real desktop application**: one native window, a taskbar icon, its own process — no browser tabs. The Electron shell starts the Python server headlessly (`python -X utf8 main.py --no-browser`), opens a single window pointed at it, and shuts the server down when the window closes. If a server is already listening on the port, the shell attaches to it instead of spawning a second one, so relaunching the app never piles up windows and running bots never opens anything outside the app. Same-origin popups (the Embeder builder) stay inside the window; external links (Discord OAuth sign-in) open in the system browser.

Environment knobs: `SCRIPTPLAYGROUND_PYTHON` (interpreter path), `SCRIPTPLAYGROUND_PORT` (base port; the shell scans upward if it is busy), `SCRIPTPLAYGROUND_NO_SERVER=1` (attach-only dev mode). Build the distributable with `npm run dist` — it compiles the server bundle via PyInstaller (`ScriptPlayground-server.spec`) and packages everything with electron-builder; output lands in `release/win-unpacked/`, with the server, `static/`, `embeder/`, `scripts/`, `assets/`, and `node_shim/` shipped as read-only resources and user data kept in `%LOCALAPPDATA%\ScriptPlayground` (override with `--data-dir` / `SCRIPTPLAYGROUND_DATA_DIR`).

### Legacy launchers

The optional Windows executable bundle is built with `scripts/build_windows.ps1`; see `docs/DESKTOP_BUILD.md` for packaging and data-location details. To use the Python launcher instead, double-click `ScriptPlayground.bat`. It activates `.venv` when present, then runs `python launcher.py` in the same console. Chrome/Edge app mode is preferred; when unavailable, the default browser is used and the server shuts down 30 seconds after the last UI WebSocket disconnects. For app mode, the spawned browser process is monitored, followed by a 5-second restart grace period. `python main.py` (terminal mode) refuses to start a second instance if one already listens on the port and no longer opens a duplicate browser tab in that case.

When using a packaged exe, scripts, workspaces, scenarios, and designs are stored under `%LOCALAPPDATA%\ScriptPlayground` on Windows and survive upgrades. `--data-dir PATH` / `SCRIPTPLAYGROUND_DATA_DIR` can override this location. A first launch seeds built-in scripts, scenarios, and designs without replacing user files.

The release bundle is `dist/ScriptPlayground/`; run `ScriptPlayground.exe` inside it. To create a shortcut, right-click `ScriptPlayground.bat` → **Show more options** → **Create shortcut**. Open shortcut **Properties → Change Icon…**, browse to `assets/icon.ico`, and select it.

For Linux desktops, save this as `~/.local/share/applications/scriptplayground.desktop` (replace `/path/to/ScirptPlayground` with this checkout's absolute path; use `launcher.py` from your activated project venv if applicable):

```ini
[Desktop Entry]
Type=Application
Name=ScriptPlayground
Comment=Local Discord bot UI playground
Exec=/path/to/ScirptPlayground/.venv/bin/python /path/to/ScirptPlayground/launcher.py
Path=/path/to/ScirptPlayground
Icon=/path/to/ScirptPlayground/assets/icon.svg
Terminal=true
Categories=Development;IDE;
```

## Beta status and support

This checkout is a **pre-release 1.0 Beta candidate**, not a stable 1.0 release. The `v0.9.0-baseline` tag marks the audit starting point, not this current worktree. Requires Python 3.9+ and discord.py 2.6+ for Components V2. Known limits: one simulated guild with fixed fixture users and roles; no Discord gateway, REST bot transport, or voice; Python scripts are not isolated; and the UI is dark-first and desktop-oriented.

Use the support or issue-reporting channel provided by whoever supplied this checkout; this README does not assume a public issue-tracker URL. Include your OS, Python version, launch command, a traceback, and a minimal reproduction. Remove tokens, client secrets, cookies, and webhook URLs from reports.

### Optional Discord identity sign-in

Offline simulation remains the default. To enable the optional **Sign in with Discord** link, configure these server-side environment variables before launch and register the exact callback URI in the Discord developer portal:

```text
DISCORD_CLIENT_ID=your-application-client-id
DISCORD_CLIENT_SECRET=your-application-client-secret
DISCORD_REDIRECT_URI=http://127.0.0.1:8741/auth/discord/callback
```

The server uses Discord's authorization-code flow with the `identify` scope only. It validates a short-lived, single-use OAuth state, keeps the code exchange and identity request server-side, and stores only an in-memory identity session behind an HttpOnly cookie; that session disappears when the server restarts. It does not request `guilds`, connect the simulator to Discord, or expose the client secret/access token to the browser. Provider failures return a generic error. The production `python main.py` entry point omits the callback query string from aiohttp access logs so authorization `code` and `state` values are not logged; callers embedding `build_app()` must configure the same `_AccessLogger` explicitly. Without all three variables, the link reports **Offline mode** and no OAuth request is made.

## DiscordEmbeder (Components V2 builder)

The [DiscordEmbeder](https://github.com/Eandab0t/DiscordEmbeder) visual
Components V2 builder ships alongside the playground: open
**http://127.0.0.1:8741/embeder** (or the ✦ button in the chat header; use your configured host and port if changed). Its built files are vendored here; the upstream checkout remains authoritative. To refresh after building that checkout, run from the ScriptPlayground root and pass its path:

```bash
python scripts/vendor_embeder.py "path/to/DiscordEmbeder"
```

### The bridge

- **▶ Test in playground** (floating button in the builder) sends the current
  design to `/api/bridge/design-to-code`, saves it under
  `scripts/designs/<name>.discordv2proj.json`, generates runnable discord.py
  (`ui.LayoutView` — a faithful port of the builder's own exporter, see
  `bridge.py`), and hands it to the playground editor through shared browser
  storage.
- **✦ Design** (editor library bar) imports any saved design from the library
  the same way. Project files are the builder's own `.discordv2proj.json`.
- **V2 in the timeline** — messages sent with a `LayoutView` render as real
  Components V2: accent containers, sections with thumbnails, galleries,
  separators, spoiler blur, and *clickable* buttons/selects nested anywhere in
  the tree (they dispatch `on_click` exactly like classic components).
- The builder's webhook test-send is untouched — it still POSTs straight from
  the browser to whatever webhook you paste.

## Folder workspaces and visual blocks

Put a bot folder under `bots/<name>/` with a `main.py` or `bot.py` entry file, then choose it in
**Bot folder → 📂 Connect** (the entry file is detected automatically and loaded into the editor).
Pressing **Run** on a connected folder boots the **whole bot project offline**: the real `discord.py`
login flow runs against a fake REST transport, so `setup_hook`, cog loading, `tree.sync`, `on_ready`,
cog listeners, slash commands, component clicks, modals, and `on_message` handlers all run through
discord.py's own dispatch — no network is ever touched. That is the *supported* surface listed in
`docs/COMPATIBILITY.md`, not the whole Discord API: the simulated world is deterministic and
deliberately incomplete (no typing, presence, threads, bulk-delete gateway events, or real voice),
and each claim in that file is backed by an end-to-end test. Live tokens are scrubbed from the
copied sandbox (`.env` and hardcoded values in source files) and replaced with a placeholder.

Node bots work too: a folder whose `package.json` depends on `discord.js` runs through a bundled
`discord.js` shim (no `npm install` needed — requires `node` on PATH). Slash commands, embeds,
mention-triggered and ragebait replies all flow into the same simulator timeline. Entries may call
`asyncio.run(bot.start(…))` (Python) or use `Events.*` constants and option builders (Node).

The **Env** developer tab shows a project's configuration discovery: dotenv files found
(`.env`, `.env.example`, `.env.local`, `.env.development`, `.env.production`), which variables each
declares, which are provided, and which declared variables are missing. Secret-looking variables
(names containing `TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, `PRIVATE_KEY`, …) are always redacted —
only names and presence are shown, never values. Bot tokens are replaced with a placeholder at run
time regardless of what the project's `.env` contains.

The editor can still run a single connected file the old way; **Save** writes
back to that folder. **🧱 Blocks** is a dependency-free starter canvas for message text,
embed title/description/color, and one button. **Apply to code** generates ordinary discord.py
that can be edited further, run, and previewed in the chat.

This is intentionally a small bridge, not a full VS Code clone. Blockly is the natural next
step if the block language grows: its official workflow serializes workspace JSON and generates
code. Monaco is the natural code-editor upgrade if multi-file IntelliSense becomes necessary.

## Script library

The bar above the editor is a small local library backed by the `scripts/`
folder:

- **Load** — pick a script in the dropdown to drop it into the editor.
- **Save** — stores the editor contents under a name (prompts; saving over an
  existing name updates it). Names may use letters, digits, spaces, `-`, `_`.
- **Delete** — removes the loaded script from the library.

Three scripts ship with the playground:

| Script | Shows off |
|---|---|
| ★ `demo` | the whole surface: embeds, files-as-media, selects, modals, chat handlers, slash commands |
| `poll_bot` | `/poll` creates a live poll; voting edits the embed in place |
| `ticket_panel` | button handlers that create `#ticket-N` channels and close them again |

The ★ marks `demo`, the default script loaded on a fresh page.

## Saved scenarios

Run a script or hosted bot first, then open **Scenarios** in the toolbar to edit, save, or replay a version-1 JSON scenario against that session. Replay uses the current handlers and adds its messages/interactions to the live session; it does not reset the timeline. `Greeting` and `Profile-aware greeting` starter scenarios are included; saved files live in `scripts/scenarios/`. The profile-aware example changes Alice and Bob's simulated profiles, replays the greeting handler as both users, then asserts their replies and actor events. Its profile changes persist in the session after replay.

```json
{
  "version": 1,
  "name": "Greeting",
  "steps": [
    {"action": "message", "content": "!hello", "as": "Alice"},
    {"assert": "message_exists", "content": "Hello, <@111111111111111111>!"}
  ]
}
```

Actions can send a message, click a component, submit a modal, invoke a command, update a simulated profile, assign roles, or configure channel permission overwrites. Use `as` with a fixture/custom username, display name, or ID to choose the actor for message/click/submit/command actions. A `profile` action takes `user` plus a partial `profile` object (`username`, `display_name`, `bio`, `avatar_url`, `banner_url`, `accent_color`, `status`). A `roles` action takes `user` plus `add` and/or `remove` arrays of role names or IDs; `@everyone` is automatic. A `permissions` action takes `channel`, `target` (role or member name/ID), and an `overwrites` object mapping discord.py permission names to `true` (allow), `false` (deny), or `null` (inherit); an empty object clears that target's overwrite. Example: `{"action":"roles","user":"Alice","add":["Moderators"]}` then `{"action":"permissions","channel":"staff","target":"Moderators","overwrites":{"view_channel":true,"send_messages":false}}`. Changes remain in effect for later steps and replays, and channel permissions follow Discord's @everyone → combined roles → member precedence. Assertions can check messages, content, embed fields/descriptions, components, member profiles, channels, and events (including an `actor`). Message assertions match a substring of the stored content; mentions are stored as `<@id>` and rendered with fixture names in the chat. Scenarios also run through hosted Python and Node bots: interactions expose assigned roles and effective user/app permissions, and Node channels implement `permissionsFor(member)` with permission overwrites.

## The scripting contract

A playground script is an ordinary Python module. Define any of these
top-level functions (all optional):

| Hook | When it fires |
|---|---|
| `async def main()` | Once per **Run** — may send messages, start `asyncio` tasks, loop forever |
| `async def on_click(interaction, custom_id, values)` | A button/select in the chat was used (`values` only for selects) |
| `async def on_submit(interaction, values, modal_id)` | A modal was submitted (`values`: `{custom_id: text}`) |
| `async def on_message(message)` | You typed in the chat composer (try `!ping`) |
| `@app_commands.command()` functions | Collected automatically: type `/` in the composer to invoke them with a real parameter form (choices, defaults, `discord.Member`/role coercion included) |

Helpers injected into your namespace: `send(...)` (posts to #playground),
`print(...)` (writes to the console pane), `client` (mock client with
`.user`, `.guilds`, `.latency`), `discord` (the real module), and `Session`.

Everything else is *real discord.py*: build `discord.Embed`s, `discord.ui.View`s,
`discord.ui.Modal`s with the same code you would write for a shipped bot. The
playground mocks the transport — `interaction.response.send_message(...)`,
`channel.send(...)`, `message.edit(...)` — and renders what you gave it. The
objects are real; the *world behind them* is a simulator, so only the API surface
listed in `docs/COMPATIBILITY.md` behaves the way the real service does.

> **ScriptPlayground runs ordinary `discord.py` bots against an isolated,
> offline simulated Discord environment.** It is not a Discord emulator and it
> never connects to Discord. See `docs/COMPATIBILITY.md` for the honest support
> matrix (including what is only partially simulated).

## Two run modes

**Discord Bot Mode** (default for real bots). Paste an ordinary `discord.py`
bot into the editor — `commands.Bot`, `bot.run("fake-token")`, cogs, Views,
Modals — and press **Run**. Simulated user actions (reactions, member joins
and edits, voice, channel and role changes) reach the bot as real gateway
events, so `on_raw_reaction_add`, `on_member_join`, `on_voice_state_update` and
friends fire through discord.py's own dispatch. The module is imported inside an isolated **worker
process** (private sandbox cwd, private `sys.path`, private event loop); all
Discord networking is intercepted, so `bot.run()` never contacts discord.com
and the token is a placeholder. Simulated messages, slash commands, component
clicks and gateway events are dispatched through the *real* discord.py
machinery (`bot.process_commands`, `Interaction`, `Command`), and everything
the bot sends is recorded in the simulated world.

**Script Mode** (legacy). The module-with-hooks style above: `async def main()`,
`send(...)`, `on_click`, `app_commands` functions. Use it for UI prototyping of
interactions without writing a full bot.

The mode is detected from the source: code that constructs `commands.Bot` /
`discord.Client` or calls `bot.run(...)` runs as a bot (including inside a
`bot.py` workspace); everything else stays on the mock script runtime.

## What's mocked

| Real Discord | Playground stand-in |
|---|---|
| Gateway / REST | In-process capture; nothing leaves your machine. Payload limits are enforced like the real API — oversized content (2000), embeds (256/4096/2048/256-char fields, 25 fields, 6000-char total), button labels (80), select options (25), placeholder (150), and 40-node Components-V2 trees raise a real `discord.HTTPException`: 400 Invalid Form Body, code 50035 |
| `discord.Interaction` | `MockInteraction` (response / followup / edit_original_response; command callbacks get `interaction.command` set) |
| `discord.Message` | `MockMessage` (edit / delete / reply / react / pin) |
| Guild, members, roles | Fixed fixtures: **You**, Alice, Bob, Carol, 3 roles |
| Channels | Scripts call `await guild.create_text_channel("name")` (name normalized, dupes get `-2`), `channel.send(...)`, `channel.delete()`; the sidebar lists channels with unread badges and switches timelines |
| Files / attachments | Real inline thumbnails for small images (data URIs); other files as name chips |
| Embed media | `set_image` / `set_thumbnail` render; `attachment://` URLs resolve against the message's files |
| Select menus | String multi-selects plus `UserSelect` / `RoleSelect` / `ChannelSelect` / `MentionableSelect`, all clickable |
| Reactions | Hover a message → **＋** → emoji picker; pills show counts, who-reacted tooltips, and your-reaction highlight. Script bots: `await message.add_reaction(...)` / `remove_reaction` / `clear_reactions`; hosted bots react through the mocked REST route. Requires the actor's `add_reactions` permission |
| Voice channels | Fully simulated: join/leave from the sidebar VOICE panel, mute/deafen toggles, speaking rings — pure UI state, no audio is captured or sent |
| Uploads | Paperclip/drag-drop up to 5 files (256 KB inline limit, images shown as real thumbnails); they arrive in `on_message` as `message.attachments` |
| Quick switcher | `Ctrl+K` jumps between channels (`#name`) and simulated users (`@name`) |
| Channel management | ＋ next to the category header creates channels (same normalization as bots); right-click a member for kick/ban/timeout — all enforced through the mock permission system |
| User settings | Profile card, switch simulated user, edit profile; **Appearance** (message display, density — kept in sync with the editor's View selects); **Voice & Sound** (sound toggle + output volume, persisted per browser) |

Members' IDs and names are listed in `playground.py` (`USER_ID`, `MEMBER_IDS`,
`ROLE_NAMES`) if you want to reference them from your scripts.

## Good to know

- **Script Mode handlers run on a dedicated event-loop thread per session.**
  CPU-bound startup code is interrupted by a deadline, while a wedged async
  script is cancelled on timeout; **↻ Restart** also cancels the old runtime
  before creating a new one.
- **Discord Bot Mode runs each bot in its own OS process** (see above). A wedged
  or CPU-spinning bot is *killed* (not merely cancelled): the server enforces a
  boot deadline and a per-operation deadline, then terminates — and kills — the
  worker, so the server itself always stays responsive. **↻ Restart** or a new
  **Run** launches a clean worker.
- **Runtime state is bounded:** scripts are capped at 1 MB, the timeline retains
  2,000 messages, and the console retains 1,000 events. The browser uses the
  websocket for instant updates and falls back to polling only when it disconnects.
- The editor autosaves to `localStorage`; **Ctrl+Enter** runs. Cozy message display groups nearby messages from the same author; Compact keeps each message header visible.
- The chat timeline updates existing message rows by message ID instead of rebuilding the whole list; genuinely new messages use short motion only, and `prefers-reduced-motion` disables entrance/interaction animation.
- Library **Save** writes to `scripts/<name>.py`; `demo.py` is the fresh-page default.
- Ephemeral messages render in the timeline with an "only visible to you" note.
- Message **edits** are tracked (`(edited ×n)`); deleted messages vanish.
- Mentions (`<@id>`, `<@&id>`, `<#id>`) resolve against the fixture members, roles, and live channels.
- The old bot features (credits economy, script library/showcase, `/plot`,
  SQLite storage) were removed in the pivot — this is now a *UI prototyping
  tool*, not a deployment target. The git history still has them.
- **What this is (and is not):** it runs ordinary `discord.py` bots against an
  *offline simulated Discord world* — real `discord.py` objects, fake network.
  It never connects to real Discord and is not meant to be deployed. Voice is
  event-only: `on_voice_state_update` fires, but no audio is transported. See
  `docs/COMPATIBILITY.md` for the supported API surface and known limits.
- **Some events need the intents the real library needs.** Reactions that
  resolve the cached message (`on_reaction_add`, `on_message_delete`) need
  `message_content`; member and voice events need the privileged `members`
  intent. Without them discord.py drops the payload, exactly as it does live.

## Architecture

```
main.py           aiohttp server: sessions API + static UI + embeder + bridge
playground.py     the mock layer: fixtures, MockInteraction/Message/Channel,
                  serializer (discord.py objects -> UI JSON), script runner
bridge.py         design payload -> runnable discord.py (port of the builder's exporter)
static/index.html single-file frontend (no build step, no CDN)
embeder/          vendored DiscordEmbeder build + bridge fragment + README
scripts/          the library: demo (default) + poll_bot + ticket_panel
scripts/designs/  saved builder projects (.discordv2proj.json)
tests/            offline smoke tests for the mock layer + bridge + server
```

Data flow: editor → `POST /run` → `playground.run_script()` execs the module
on the session's loop thread → sends/clicks/modals captured as plain dicts →
UI renders the state JSON. Component clicks and modal submits round-trip
through `/click` and `/submit` into your `on_click` / `on_submit`.
