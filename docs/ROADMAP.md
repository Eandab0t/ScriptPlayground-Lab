# Roadmap

This is a prioritization guide, not a promise to build every idea.

## Highest-value next steps

The following baseline work is complete: bridge round-trip fixtures, vendored Embeder provenance, and read-only permission-denial diagnostics. Keep these behaviors covered when changing the runtime.

### 1. Add a small scenario format

- Save a sequence such as “Alice sends message → clicks button → receives response.”
- Replay it against one session.
- Reuse existing dispatch functions instead of inventing a broad event bus.

### 2. Improve workspace editing deliberately

- Decide whether the product needs multiple files before adding them.
- If yes, define a safe workspace manifest and explicit file allowlist.
- Keep execution local and prevent path traversal.

### 3. Reduce concentrated ownership when a change demands it

- Extract only a proven seam from `playground.py` or `static/index.html`.
- Preserve the current HTTP/runtime contract.
- Do not split files for aesthetics alone.

### 4. Decide on session persistence

Session state is currently memory-only. Before implementing persistence, establish whether restart recovery is worth the storage, migration, and cleanup cost; if it is, share a serializable shape with scenario replay rather than building two formats.

## Later possibilities

- More Discord channel types and threads.
- Simulated member lifecycle and event inspection.
- Permission and intent fixtures.
- Voice-state simulation without real audio.
- Server-state import/export.
- Richer block editing backed by the same canonical project state.

## Explicit non-goals for the current baseline

- Rebuilding the application around a framework.
- A complete Discord clone.
- Real Discord connectivity or token handling.
- Multi-guild architecture before a second guild use case exists.
- A plugin system or generated multi-language code.
- A full VS Code replacement.
