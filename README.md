# ScriptPlayground

A **local Discord bot playground** — no token, no server, no network. The UI
uses Discord's dark palette and `gg sans`/Discord's standard fallback font stack.
Write
plain `discord.py` code in the web editor, press **Run**, and see exactly what
your bot would post: embeds, buttons, select menus, and modals, rendered in a
Discord-styled chat. Then *use* the UI: click the buttons, pick from the
selects, fill in the modals, type `/` for the **slash-command palette** — your
code's handlers run against mocked `Interaction` objects.

```
┌─────────────┬──────────────────────┬──────────────────┐
│  channels   │  # playground        │  bot.py  [▶ Run] │
│  (live —    │  the viewed          │  your code       │
│  switch!)   │  channel, live       │  console + timing│
└─────────────┴──────────────────────┴──────────────────┘
```

## Quick start

```bash
pip install -r requirements.txt
python main.py            # opens http://127.0.0.1:8741 in your browser
```

Options: `--port 8741`, `--host 127.0.0.1`, `--no-browser`.

## DiscordEmbeder (Components V2 builder)

The [DiscordEmbeder](https://github.com/Eandab0t/DiscordEmbeder) visual
Components V2 builder ships alongside the playground: open
**http://127.0.0.1:8741/embeder** (or the ✦ button in the chat header). Its
dist is vendored — the E: repo stays authoritative. To refresh after an
upstream rebuild:

```bash
python scripts/vendor_embeder.py   # copies dist + injects the bridge button
```

### The bridge

- **▶ Test in playground** (floating button in the builder) sends the current
  design to `/api/bridge/design-to-code`, saves it under
  `scripts/designs/<name>.discordv2proj.json`, generates runnable discord.py
  (`ui.LayoutView` — a faithful port of the builder's own exporter, see
  `bridge.py`), and pushes it into the playground editor via `postMessage`.
- **✦ Design** (editor library bar) imports any saved design from the library
  the same way. Project files are the builder's own `.discordv2proj.json`.
- **V2 in the timeline** — messages sent with a `LayoutView` render as real
  Components V2: accent containers, sections with thumbnails, galleries,
  separators, spoiler blur, and *clickable* buttons/selects nested anywhere in
  the tree (they dispatch `on_click` exactly like classic components).
- The builder's webhook test-send is untouched — it still POSTs straight from
  the browser to whatever webhook you paste.

## Folder workspaces and visual blocks

Put a bot folder under `bots/<name>/bot.py`, then choose it in **Bot folder → 📂 Connect**.
The existing editor runs the connected file against the same offline Discord mock; **Save** writes
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
`discord.ui.Modal`s exactly as you would in a shipped bot. The playground
mocks only the transport — `interaction.response.send_message(...)`,
`channel.send(...)`, `message.edit(...)` — and renders what you gave it.

## What's mocked

| Real Discord | Playground stand-in |
|---|---|
| Gateway / REST | In-process capture; nothing leaves your machine |
| `discord.Interaction` | `MockInteraction` (response / followup / edit_original_response; command callbacks get `interaction.command` set) |
| `discord.Message` | `MockMessage` (edit / delete / reply / react / pin) |
| Guild, members, roles | Fixed fixtures: **You**, Alice, Bob, Carol, 3 roles |
| Channels | Scripts call `await guild.create_text_channel("name")` (name normalized, dupes get `-2`), `channel.send(...)`, `channel.delete()`; the sidebar lists channels with unread badges and switches timelines |
| Files / attachments | Real inline thumbnails for small images (data URIs); other files as name chips |
| Embed media | `set_image` / `set_thumbnail` render; `attachment://` URLs resolve against the message's files |
| Select menus | String multi-selects plus `UserSelect` / `RoleSelect` / `ChannelSelect` / `MentionableSelect`, all clickable |

Members' IDs and names are listed in `playground.py` (`USER_ID`, `MEMBER_IDS`,
`ROLE_NAMES`) if you want to reference them from your scripts.

## Good to know

- **Handlers run on a dedicated event-loop thread per session.** CPU-bound
  startup code is interrupted by a deadline, while a wedged async script is
  cancelled on timeout; **↻ Restart** also cancels the old runtime before
  creating a new one.
- **Runtime state is bounded:** scripts are capped at 1 MB, the timeline retains
  2,000 messages, and the console retains 1,000 events. The browser uses the
  websocket for instant updates and falls back to polling only when it disconnects.
- The editor autosaves to `localStorage`; **Ctrl+Enter** runs.
- Library **Save** writes to `scripts/<name>.py`; `demo.py` is the fresh-page default.
- Ephemeral messages render in the timeline with an "only visible to you" note.
- Message **edits** are tracked (`(edited ×n)`); deleted messages vanish.
- Mentions (`<@id>`, `<@&id>`, `<#id>`) resolve against the fixture members, roles, and live channels.
- The old bot features (credits economy, script library/showcase, sandboxed
  execution, `/plot`, SQLite storage) were removed in the pivot — this is now
  a *UI prototyping tool*, not a runnable bot. The git history still has them.

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
