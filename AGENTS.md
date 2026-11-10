# Agent rules

Rules for AI agents (Claude Code, Codex, Cursor) working in this repository.

## Documentation

- Write all documentation, code comments and commit-facing text in English.
- Be concise. No filler, no marketing tone. One idea per sentence.

## Git

- Do not commit or push commits. The maintainer commits manually.
- Leave changes in the working tree for review.

## Safety

- Never write to a TMS without a prior dry-run (`tms-agent-kit diff`, or the plan printed by `push`). Explicit human confirmation is not required for now.
- Deletion in a TMS is disabled by default. Do not add code paths that delete without an explicit opt-in flag.
- Never commit secrets. TMS tokens come from environment variables or a local `.env` file.
