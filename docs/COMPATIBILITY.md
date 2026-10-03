# Discord compatibility notes

ScriptPlayground uses `discord.py` types where practical, but the transport is a local mock. This is a development aid, not a claim of complete Discord emulation.

## Supported surface

| Area | Current behavior |
|---|---|
| Messages | Capture content, embeds, files, classic components, Components V2, edits, deletes, replies, and log basic reaction/pin actions. |
| Components | Buttons, string selects, user/role/channel/mentionable selects, and Components V2 trees (`ui.LayoutView` with text/section/gallery/thumbnail/file/separator nodes). Subclassed components (e.g. `class OpenButton(ui.Button)`) serialize like their base kind; clicks run a subclass's own callback first — the inherited `Item.callback` no-op falls through to the `on_click` handler. Each message keeps its live view so edits re-serialize the current tree and clicks resolve the current items. |
| Modals | Open and submit text inputs through the browser. A `ui.Modal` subclass's own `on_submit` runs with values written onto its `TextInput` children (like real discord.py); the inherited default no-op falls back to the `on_submit(interaction, values, modal_id)` handler. |
| Commands | Discover `app_commands.command`, render argument forms, coerce common types and choices. |
| Users | Fixed You/Alice/Bob/Carol fixtures; session-selected active user. |
| Guild permissions | Role-derived permissions with owner/admin handling. |
| Channel permissions | `@everyone`, combined role, then member overwrite precedence. |
| Interaction permissions | `permissions` for the active user and `app_permissions` for the mock bot. |
| Permission failures | `discord.Forbidden` for denied sends, channel deletes, message deletes, interaction responses, and follow-ups; runtime events include the deciding resolution step. |
| Components V2 bridge | Validated designs generate runnable `discord.py` source; bridge fixtures cover nested accessories, galleries, separators, spoiler containers, and nested controls. |
| Embeder provenance | `GET /api/embeder/info` reports the committed marker for the vendored local build; it does not contact DiscordEmbeder at runtime. |
| Runtime inspection | The browser State, Events, and Actions tabs read the current session state; State also shows active-user and bot permission results with resolver reasons, event/action rows can be expanded to inspect JSON-safe details, including failed attempts, and no new runtime state owner is added. Events link to their related actions and Actions link back to the triggering event and current State; message context tools link to related inspector entries where retained. The chat timeline updates keyed message rows, shows an informational member rail, groups nearby same-author messages only in Cozy mode, and honors reduced motion. |
| Scenarios | Version-1 local JSON scenarios save/replay message, component-click, modal-submit, and slash-command dispatches in one session; assertions cover messages/content/embed fields/components/channels/events. Replay stops on the first failed step and returns a bounded runtime snapshot. This is a local test utility, not a gateway/event bus or isolated runtime. |
| Script errors | Startup and dispatched callback exceptions expose type, message, and the innermost `<playground>` source line when available. Callback tracebacks remain available for inspection; editor navigation applies to the current single-file source. |
| Multi-file workspaces | A `bot.py` workspace runs through the playground runtime: the entry executes with real `__file__`/`__name__`, workspace-relative plain and `from .x import` imports resolve via a workspace-scoped finder, an entry `setup(bot)` coroutine is awaited, and cog commands/listeners register through `bot.add_cog` after `add_cog` re-binds. `workspace.json` may restrict imports to a `files` allowlist; absolute paths, `..`, outside symlinks, and non-allowlisted files are rejected. Workspace modules are unloaded between runs so re-runs re-import fresh. |
| Gateway events | `dispatch_event` delivers `on_member_join`, `on_member_remove` (stale member object, like real leave payloads), `on_member_update` (before/after, roles + nickname only), `on_guild_channel_create/delete`, `on_guild_role_create/delete`, `on_raw_reaction_add/remove`, and `on_reaction_add/remove` to module handlers and Slice-1 cog listeners. `RawReactionActionEvent` is a real discord.py object (`message_id`/`user_id`/`channel_id`/`guild_id`/`emoji`/`event_type`); `.member` is always None, matching real guild payloads — resolve the actor via `guild.get_member(payload.user_id)`. Reaction toggles fire raw first, then the constructed event: `on_reaction_add/remove` receive a genuine `discord.Reaction` (`emoji`, `count`, `me`, `message` with `content`/`author`/`reference`) plus the reacting `Member`. UI: member context menu (add/remove role, leave), channel right-click delete, message reaction picker. Editing a nickname on a profile card (or PUT `members/{id}/profile` with a changed `display_name`) fires `on_member_update` with the before/after snapshots; unchanged display names do not fire. |
| Reactions | Both raw and constructed reaction events are supported. Per-message reaction state (emoji → user list) is stored on the simulator message, rendered as pills, included in REST message payloads (`reactions` with `count`/`me`/`emoji`), and used to build the real `discord.Reaction` passed to `on_reaction_add/remove`. The final removal still fires `on_reaction_remove` with `count` 0 (the simulator does not keep an empty pill, like real clients). `payload.member` on raw events is never populated (same as real guild payloads). |
| Cog commands | Cogs added via `bot.add_cog` contribute `app_commands.Command` attributes to slash discovery; entry-`setup` and cog-`cog_load` coroutines are awaited once. Cog listeners are discovered and stored but not yet dispatched. |
| Optional Discord identity | Server-side OAuth2 authorization-code flow with `identify` only, state validation, memory-scoped identity session, logout, generic provider errors, and callback-query access-log redaction on the production `web.run_app()` entry point; callers embedding `build_app()` must configure the same logger explicitly, and the flow does not import guilds or alter the simulator. |
| Channels | In-memory text channels with normalized names and duplicate suffixes. |
| Files | Small image files become inline data URIs; other files remain metadata chips. |

## Permission contract

The current default fixture intentionally allows ordinary users to send messages and keeps the bot operational. Tests may assign role permissions, channel overwrites, or a temporary member permission override to exercise failure paths.

Resolution order:

1. Guild-level permissions from `@everyone` and assigned roles.
2. Administrator bypass.
3. `@everyone` channel overwrite.
4. Combined role overwrites.
5. Member-specific overwrite.
6. The resolved permission result is checked by the operation; denial diagnostics identify the applicable overwrite/final-resolution step.

This covers the operations already represented in the playground. It does not claim to reproduce every Discord permission bit or every API-side validation rule.

## Known gaps

- No real gateway events or simulator REST requests. OAuth's identity request is the deliberate exception: it calls Discord only during an explicitly configured sign-in callback, never from simulator operations.
- Cog listeners (`@commands.Cog.listener()`) are registered but not dispatched yet; only the flat `on_message`/`on_click`/`on_submit` scripting contract routes events. Prefix commands (`commands.command`) are not modeled — slash commands only.
- Workspace runs boot the saved `bot.py` (Run ignores the editor buffer while a bot.py workspace is connected); Run File executes the unsaved editor buffer through the same import machinery, and the mtime watcher live-reloads the armed file with workspace imports resolving. Hosted project boot (`bot_runtime.run_project`) remains the path for `main.py`-entry workspaces.
- No permission-management UI.
- No multi-guild state.
- No voice or stage model.
- No threads, DMs, webhooks, forums, scheduled events, rate limits, or intent simulation.
- OAuth sessions are memory-only, do not survive restart, and do not provide guild discovery, account settings, real Discord data access, or real Discord actions. Callback-query redaction is guaranteed by the production `web.run_app()` wiring, not by arbitrary embedding callers that choose aiohttp's default access logger.
- Some mock methods are intentionally shallow and may accept arguments that real Discord would reject.

When adding a new compatibility feature, define the behavior first with a small test that follows the real user/runtime path. Avoid expanding the mock surface just to make an API listing look complete.
