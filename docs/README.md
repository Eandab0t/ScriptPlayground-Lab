# ScriptPlayground documentation

This folder is a shareable context pack for developers and AI reviewers. It explains what ScriptPlayground is, how the current code is shaped, what is intentionally out of scope, and prompts that produce useful feedback instead of generic rewrites.

## Start here

1. [Project brief](PROJECT_BRIEF.md) — product purpose, current capabilities, and constraints.
2. [Architecture](ARCHITECTURE.md) — data flow, ownership, runtime model, and boundaries.
3. [Compatibility](COMPATIBILITY.md) — what the mock Discord runtime supports and where it differs from Discord.
4. [Roadmap](ROADMAP.md) — ranked next steps and deliberate non-goals.
5. [AI prompts](AI_PROMPTS.md) — copy/paste prompts for reviews, implementation plans, audits, and product critique.

## Ground rules for reviewers

- Review the existing product before proposing a rewrite.
- Preserve the local-only, no-token, no-network runtime.
- Prefer the smallest change that proves a behavior.
- Treat `playground.py` as the current runtime owner and `main.py` as the HTTP owner.
- Do not invent voice, multi-guild, arbitrary-folder, or plugin features unless the task explicitly asks for them.
- Separate concrete defects from future ideas.

The repository's top-level `README.md` remains the user-facing setup guide. These files are the deeper context pack intended for collaboration and external AI opinions.
