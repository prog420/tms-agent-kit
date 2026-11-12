# Test repository

Test cases, test plans and runs of the products below, synced with the TMS by [tms-agent-kit](https://github.com/prog420/tms-agent-kit). Agents work here as QA engineers. Product code stays in its own repositories.

## Projects

| Product repository | Project folder | TMS project |
|---|---|---|
| `<path or name of the product repository>` | `projects/<name>/` | `<base_url>`, project `<id>` |

## Layout

```
projects/<name>/
  tms-agent-kit.toml   # TMS connection of the project
  cases/               # test cases, one folder per TMS section
  plans/               # test plans
  runs/                # run files with notes, and evidence; not committed
  bugs/                # bug files found while testing, with evidence
  reports/             # HTML reports of `tms-agent-kit report`; not committed
  .tms/                # tool state; never edit by hand
.claude/skills/        # agent skills; update with `uv run tms-agent-kit skills install`
.mcp.json              # MCP servers for agents (browser automation)
.env                   # TESTIT_TOKEN; never read, print or commit it
```

## Rules

- Run the CLI as `uv run tms-agent-kit ...` from the project folder, so it finds the project's `tms-agent-kit.toml`.
- Product repositories are read-only for agents. Ask before changing them or their environment.
- Before every `--confirm`, show the dry-run to the user and wait for their go-ahead.
- Run files and evidence under `projects/*/runs/` and reports under `projects/*/reports/` are not committed: the TMS keeps the results.
- Record the time spent on each executed case (`Time:` in the run file), measured, never estimated.
- Write run notes (summary, environment, observations, cleanup) into the run file and a bug file per product defect in `bugs/`. The HTML report is built from them; never write report pages by hand.
- Commit case and plan changes together with `.tms/` (except `.tms/runs/`), so every clone can push.

## Commands

- `/test <path to product repository> [ticket, branch or feature]`: test a change end to end.
- `uv run tms-agent-kit init --project NAME --base-url URL --project-id ID`: add a project.
- `uv run tms-agent-kit skills install`: update the skills after upgrading tms-agent-kit.
- `uv run tms-agent-kit report [--run RUN_FILE]`: write an HTML report with coverage, results, time, notes, bugs and evidence to `reports/` (offline).
