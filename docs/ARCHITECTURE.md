# Architecture

## Current ownership

```text
main.py
  HTTP routes, optional Discord OAuth identity flow, in-memory auth/session lookup, static files, workspace/library/design APIs

playground.py
  Mock Discord objects, session state, permission resolution,
  script execution, interaction dispatch, serialization

project_state.py
  Canonical Components V2 project validation

bridge.py
  Validated project state → runnable discord.py source
  Bridge fixtures exercise generated LayoutView code against the mock runtime.

scripts/vendor_embeder.py
  Copies the authoritative Embeder dist and writes embeder/VENDORED_FROM.txt.

static/index.html
  Single-file browser UI, dark-first desktop Discord-like shell, optional sign-in entry point, editor, preview, selectors, local display settings, state/event/action inspectors, and API calls

The browser theme is owned by the `:root` semantic tokens in `static/index.html`; density and message display preferences are UI-only localStorage settings and do not enter session state or runtime behavior.

tests/test_smoke.py
  Offline behavioral contract for the runtime, bridge, server, and stress cases
```

## Optional identity flow

When `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, and `DISCORD_REDIRECT_URI` are all configured, `/auth/discord/login` creates a cryptographically random, server-memory OAuth state, mirrors it in an HttpOnly short-lived state cookie, and redirects to Discord with the `identify` scope only. The callback consumes and validates both state copies, exchanges the code server-side, fetches `/users/@me` server-side, and stores only the returned identity behind an HttpOnly session cookie. `/auth/logout` removes that in-memory session. Missing configuration returns a clear disabled response and the browser keeps its offline entry point.

OAuth identity is separate from `Session.active_user`: it does not populate simulated members, read guilds, call the gateway, or send REST operations on the user's behalf. Tokens and client secrets never enter browser state or session JSON. Callback provider failures are returned as generic errors. The production `web.run_app()` entry point uses `_AccessLogger` to omit the callback query string so authorization codes and state values are not logged; callers embedding `build_app()` must configure that access logger explicitly. State/session data is intentionally memory-only for this local tool; the event-loop execution model is not a security sandbox.

## Runtime flow

```text
Browser action
   ↓ HTTP route in main.py
Session lookup
   ↓
playground dispatch function
   ↓ dedicated event-loop thread
User script receives MockInteraction / MockMessage
   ↓
MockChannel captures or rejects the operation
   ↓
Session timeline and events
   ↓
state(session) JSON
   ↓ websocket nudge or browser refresh
static/index.html renders the result; State, Events, and Actions inspectors summarize the same state JSON, including active-user and bot permission results.
```

## Interaction identity

`Session` owns the selected simulated user. New interactions read `session.active_user`; they do not maintain their own user copy.

- Message/composer author: active user.
- Button/select interaction: active user, type `component`.
- Modal submission: active user, type `modal_submit`.
- Slash command: active user, type `application_command`.
- `interaction.permissions`: resolved permissions for the active user in the interaction channel.
- `interaction.app_permissions`: resolved permissions for the simulated bot in that channel.

## Permission flow

```text
member.guild_permissions
   ↓
administrator? ── yes → all permissions
   │ no
@everyone overwrite
   ↓
combined role overwrites
   ↓
member-specific overwrite
   ↓
resolved channel permissions
   ↓
MockChannel operation or interaction metadata
```

`MockChannel.send()` is the shared enforcement seam for bot and explicit-member sends. Interaction response and follow-up sends route through it rather than maintaining separate permission checks. Denied operations carry the resolver step that decided the failure, and the existing console/API path exposes that diagnostic.

## Bridge and vendored UI boundaries

- `bridge.py` consumes validated project state and generates runnable `discord.py` source; `tests/fixtures/bridge_roundtrip/` proves nesting, accessories, gallery limits, separators, spoiler state, and nested dispatch.
- `embeder/index.html` is a committed local build from the external DiscordEmbeder source. `embeder/VENDORED_FROM.txt` records its source commit, and `GET /api/embeder/info` reports the same marker without contacting the network.

## State boundaries

- Session state is in memory and scoped to one browser/runtime session.
- OAuth state and authenticated identity sessions are owned by `main.py`; they are separate from simulated Discord session state and are not persisted.
- `Session.events` remains the single event/action stream. Entries carry a `kind` and optional JSON-safe `details`; attempted, denied, blocked, missing, and unanswered operations are recorded there, and the browser filters and expands that stream without creating another state owner. Expanded event/action details stay open across ordinary state refreshes.
- `state(session)` derives the State inspector's active-user and bot permission summaries from `MockChannel.permission_check()`; the UI does not calculate permissions independently.
- Saved designs are JSON files under `scripts/designs/`; bridge fixtures live under `tests/fixtures/bridge_roundtrip/`.
- Saved scripts are Python files under `scripts/`.
- Connected bot workspaces are under `bots/`.
- Test logs and compiler output belong under `.test-artifacts/` and must not be committed.

## Known structural debt

`playground.py` and `static/index.html` are still large, multi-responsibility files. Splitting them could improve maintainability, but it should happen only as a separately scoped refactor with tests protecting the current runtime contract. Do not use this documentation as permission to rewrite them during a feature task.
