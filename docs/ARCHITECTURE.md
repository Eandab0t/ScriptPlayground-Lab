# Architecture

## Current ownership

```text
main.py
  HTTP routes, session lookup, static files, workspace/library/design APIs

playground.py
  Mock Discord objects, session state, permission resolution,
  script execution, interaction dispatch, serialization

project_state.py
  Canonical Components V2 project validation

bridge.py
  Validated project state → runnable discord.py source

static/index.html
  Single-file browser UI, editor, preview, selectors, and API calls

tests/test_smoke.py
  Offline behavioral contract for the runtime, bridge, server, and stress cases
```

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
static/index.html renders the result
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

`MockChannel.send()` is the shared enforcement seam for bot and explicit-member sends. Interaction response and follow-up sends route through it rather than maintaining separate permission checks.

## State boundaries

- Session state is in memory and scoped to one browser/runtime session.
- Saved designs are JSON files under `scripts/designs/`.
- Saved scripts are Python files under `scripts/`.
- Connected bot workspaces are under `bots/`.
- Test logs and compiler output belong under `.test-artifacts/` and must not be committed.

## Known structural debt

`playground.py` and `static/index.html` are still large, multi-responsibility files. Splitting them could improve maintainability, but it should happen only as a separately scoped refactor with tests protecting the current runtime contract. Do not use this documentation as permission to rewrite them during a feature task.
