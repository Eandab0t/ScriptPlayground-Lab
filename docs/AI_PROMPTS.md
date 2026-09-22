# AI prompts

These prompts are intentionally specific. Attach this `docs/` folder and the relevant changed files when using them. Resolve every `docs/...` path relative to the `ScirptPlayground/` project root; if you upload the docs folder by itself, preserve that root context or replace the paths with the uploaded filenames.

## Product critique

```text
You are reviewing ScriptPlayground, a local Discord bot development environment. Read docs/PROJECT_BRIEF.md, docs/ARCHITECTURE.md, and docs/COMPATIBILITY.md before judging it. Compare the current product to its stated goal: let a developer write real discord.py UI code, simulate users and permissions, and inspect behavior without a real Discord server. Give five concrete improvements ranked by user value versus implementation cost. Separate missing requirements from optional ideas. Do not recommend a rewrite, remote Discord integration, or features outside the stated product.
```

## Architecture review

```text
Review the attached ScriptPlayground code as a maintainer. Trace one flow from browser action through main.py, Session, playground.py, Mock Discord objects, and state JSON back to the UI. Identify only concrete ownership violations, duplicated policy, state drift, or unsafe boundaries. For every finding cite a file and behavior, explain how to reproduce it, and propose the smallest fix. Do not split large files or add abstractions unless the current flow proves they are needed.
```

## Permission-model review

```text
Audit ScriptPlayground's mock Discord permission behavior against the documented contract. Exercise @everyone, combined role, member overwrite, administrator bypass, active-user permissions, bot app_permissions, channel sends/deletes, interaction responses, and follow-ups. Confirm denied operations expose the deciding resolution step through the existing event/API path. Look especially for checks that bypass the shared enforcement path or stale permission snapshots. Report concrete failures with a minimal regression test. Do not expand into voice, intents, multi-guild state, or a complete Discord clone.
```

## Adversarial test plan

```text
Create a compact adversarial test plan for ScriptPlayground's real behavior. Cover default You behavior, switching to Alice, invalid user selection, rapid UI state changes, button/select/modal/command metadata, denied user operations, denied bot operations, allowed operations, reruns, empty inputs, deleted channels, and cleanup. Use the existing test harness and HTTP routes. Prefer a few boundary cases over a large synthetic matrix. State which checks must run offline and which should run through the HTTP/browser surface.
```

## Feature implementation

```text
Implement the smallest complete change requested for ScriptPlayground. First read docs/PROJECT_BRIEF.md and docs/ARCHITECTURE.md. Reuse the existing Session and Mock Discord seams. Preserve the local-only runtime, default fixture behavior, and current UI. Add no new dependency, broad event bus, multi-guild model, arbitrary-folder access, or speculative compatibility layer. Add focused behavioral tests, run the full smoke suite and Ruff, isolate compilation output under .test-artifacts, and report concrete remaining gaps.
```

## Code review / ponytail pass

```text
Review only the attached diff for over-engineering. Find code that can be deleted or simplified: duplicate validation, one-use wrappers, dead branches, invented standard-library behavior, unnecessary dependencies, and tests coupled to implementation instead of user behavior. Use this format:
[delete|simplify|question] file:line — finding
Then give estimated deletions and a verdict. Do not propose unrelated features or a rewrite.
```

## Permission inspector review

```text
Review ScriptPlayground's State inspector permission summary. Trace the active-user and bot permission values from MockChannel.permission_check() through state(session) into the browser. Confirm reasons update after switching users or changing overwrites, denied results match runtime enforcement, and the UI does not implement a second permission resolver. Report only concrete discrepancies; do not expand into a permissions editor or complete Discord parity.
```

## Event and action inspector review

```text
Review ScriptPlayground's State, Events, and Actions inspector tabs. Trace Session.events from runtime operations through state(session) JSON into the browser. Confirm event/action entries remain structured, details are JSON-safe, selecting an entry shows the right details, filters refresh after Run and interactions, and no second event bus or state owner was introduced. Report only concrete UX or correctness failures; do not expand into scenarios, voice, persistence, or a frontend rewrite.
```

## Bridge and vendored-build audit

```text
Audit ScriptPlayground's Components V2 bridge and vendored Embeder build. For every fixture under tests/fixtures/bridge_roundtrip/, verify JSON → project validation → bridge.py → generated LayoutView → mock runtime, including serialized nesting and nested dispatch. Compare embeder/VENDORED_FROM.txt with the actual authoritative source commit and GET /api/embeder/info, and verify the committed embeder/index.html is the build described by that marker. Report only concrete drift or missing coverage; do not refresh artifacts or redesign the bridge during the audit.
```

## Release-candidate audit

```text
Treat the attached commit as a release candidate. Verify the PR/base/head state, changed-file integrity, local test suite, lint, compilation artifact isolation, and one real HTTP flow. Grade SPEC, DESIGN, CORRECTNESS, and QUALITY from 0-10. Correctness must be based on exercised behavior; unexercised claims are capped at 6. Report only concrete blockers, distinguishing defects from documented roadmap gaps. Do not edit code during the audit.
```

## Roadmap prioritization

```text
Given the current ScriptPlayground docs and code, choose exactly one next capability. Rank candidate ideas by user value, proof of need, implementation cost, and fit with the existing architecture. Prefer the smallest slice that improves the local Discord development loop. Explain what you are deliberately not building yet and define the behavioral tests that would prove the chosen slice.
```

## Ask for a visual explanation

```text
Create a self-contained visual explanation of ScriptPlayground's current architecture. Show the browser → HTTP → Session → mock Discord → serialized state flow, active-user identity, permission precedence, and where the code generator/project validator fit. Make uncertainty and known compatibility gaps explicit. Do not change product code. Use only the attached documentation and repository behavior as evidence.
```
