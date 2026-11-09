# tms-agent-kit

Two-way sync between a test management system (TMS) and Markdown files in Git, plus skills for AI agents.

> Status: early development. Nothing below works yet.

## Why

Agents (Claude Code, Codex, Cursor) work slowly with test cases through TMS MCP servers:

- one MCP call per case; raw JSON fills the context;
- the agent re-learns project structure (sections, suites, shared steps, attributes) every session and makes mistakes;
- bulk work (library audit, duplicate search, editing dozens of cases) is slow and expensive.

Local files and scripts are faster. `tms-agent-kit` puts test cases in the repository as Markdown and syncs them back.

## How it works

1. **Pull.** Export the project tree from the TMS via REST API. One `.md` file per case; folders mirror sections. TMS ID, version and modification date live in the frontmatter. Updates are incremental.
2. **Read.** The agent works with files directly (grep, read). No network calls.
3. **Push.** The agent edits Markdown. `tms-agent-kit diff` shows the changes (dry-run). After human confirmation, `tms-agent-kit push` sends them to the TMS in one batch.
4. **Skills.** One `SKILL.md` per TMS describes its entities, case format and naming rules, so the agent does not re-learn them.

Only test cases are synced. The TMS remains the source of truth for runs, results and reports.

## Planned commands

```bash
tms-agent-kit pull     # TMS -> Markdown
tms-agent-kit diff     # local changes vs TMS (dry-run)
tms-agent-kit push     # Markdown -> TMS, requires confirmation
```

## Supported TMS

| TMS | Status |
|---|---|
| Test IT (cloud, REST API v2) | in progress |
| Allure TestOps | planned |

## Safety

- Every write requires a dry-run and explicit confirmation.
- Before push, the case version in the TMS is compared with the local one. Conflicts block the push.
- Deletion is disabled by default.

## Development rules

See [AGENTS.md](AGENTS.md).

## License

[Apache-2.0](LICENSE)
