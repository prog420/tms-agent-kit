# tms-agent-kit

Two-way sync between a test management system (TMS) and Markdown files in Git, plus skills for AI agents.

> Status: early development. Test IT sync of cases, plans and runs works; agent skills ship with the package.

## Why

Agents (Claude Code, Codex, Cursor) work slowly with test cases through TMS MCP servers:

- one MCP call per case; raw JSON fills the context;
- the agent re-learns project structure (sections, suites, shared steps, attributes) every session and makes mistakes;
- bulk work (library audit, duplicate search, editing dozens of cases) is slow and expensive.

Local files and scripts are faster. `tms-agent-kit` puts test cases in the repository as Markdown and syncs them back.

## How it works

1. **Pull.** Export the project tree from the TMS via REST API. One `.md` file per case; folders mirror sections. TMS ID, version and modification date live in the frontmatter. Updates are incremental.
2. **Read.** The agent works with files directly (grep, read). No network calls.
3. **Push.** The agent edits Markdown. `tms-agent-kit diff` shows the changes (dry-run). `tms-agent-kit push` prints the plan and a confirmation token. After the human agrees, `push --confirm <token>` sends the changes.
4. **Skills.** Agent skills describe the file formats and the workflows, so the agent does not re-learn them.

Cases and plans are synced both ways. Run results are uploaded; the TMS remains the source of truth for results and reports.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

The recommended layout is one test repository for all products: cases, plans, skills, MCP servers and the token live in one place, and the agent tests product repositories by path. Create it with:

```bash
mkdir my-tests && cd my-tests && git init
uvx --from git+https://github.com/prog420/tms-agent-kit tms-agent-kit init \
    --project shop --base-url https://example.testit.software --project-id 42
uv sync && cp .env.example .env    # set TESTIT_TOKEN
cd projects/shop && uv run tms-agent-kit check && uv run tms-agent-kit pull
```

`init` writes `AGENTS.md`, `CLAUDE.md`, `.gitignore` (run results and reports are not committed), `.mcp.json` (Playwright), `pyproject.toml`, `projects/<name>/tms-agent-kit.toml`, and installs the agent skills into `.claude/skills/`. It never overwrites files; run it again to add a project.

To use the tool in a single repository instead, create the config from the example and set the token:

```bash
cp tms-agent-kit.example.toml tms-agent-kit.toml   # edit base_url and project_id
cp .env.example .env                               # set TESTIT_TOKEN; keep .env out of Git
```

The environment variable `TESTIT_TOKEN` takes precedence over `.env`. The nearest `.env` from the config folder up to the git repository root is used, so projects of one repository share a root `.env`. `base_url` must use https.

## Commands

```bash
tms-agent-kit check                    # verify config, token and API access; list section folders (read-only)
tms-agent-kit pull [--force]           # TMS -> Markdown; --force discards local edits
tms-agent-kit diff [--remote]          # local changes vs last pull; --remote checks for conflicts
tms-agent-kit push [--allow-delete]    # print the plan summary and a token; writes nothing
tms-agent-kit push --confirm TOKEN     # apply the plan shown with this token

tms-agent-kit plan list                # test plans in the TMS and their local files (read-only)
tms-agent-kit plan diff                # plan file changes against the last plan push (offline)
tms-agent-kit plan push [--allow-delete] [--confirm TOKEN]   # create, update, archive plans
tms-agent-kit run new PLAN_FILE [--configuration NAME]       # write a run file to fill in (offline)
tms-agent-kit run push RUN_FILE [--confirm TOKEN]            # upload the run results
tms-agent-kit report [--run RUN_FILE] [-o FILE]   # HTML page of cases, plans, runs and evidence (offline)

tms-agent-kit init [--project NAME --base-url URL --project-id ID]   # test repository layout
tms-agent-kit skills install [--dir DIR] [--force]                 # install or update agent skills
```

Global options: `--config PATH`, `-v` (log HTTP requests and retries).

Exit codes: `0` success, `1` changes pending, `2` error or conflict, `130` interrupted.

Typical session:

```bash
tms-agent-kit pull
# edit cases/**/*.md, or add a new file without `id` to create a case
# (the folder picks the section; `check` lists section folders; a new folder becomes a new section)
tms-agent-kit diff
tms-agent-kit push                     # review the plan
tms-agent-kit push --confirm 3f2a9c1b04de
```

Testing a plan:

```bash
tms-agent-kit plan push                # plans/*.md: '# name' and a '## Cases' list of ids, files, folders
tms-agent-kit run new plans/regress.md # writes runs/<date>-regress.md
# execute the cases, fill in 'Result: passed|failed|blocked|skipped' and 'Time: 4m 30s' per case;
# link screenshots and logs with ![shot](2026-11-10-regress/step-2.png): they become attachments
tms-agent-kit run push runs/2026-11-10-regress.md   # review, then repeat with --confirm TOKEN
tms-agent-kit report --run runs/2026-11-10-regress.md   # writes reports/2026-11-10-regress.html
```

`Time:` is the time spent on the case. It is uploaded as the result duration and summed per run in the report, for analytics.

The run appears in the TMS under the plan. Test IT accepts results only for automated cases, so `run push` links each executed case to its own autotest `tms-agent-kit-<globalId>`; the case becomes automated.

The case file format and sync rules are described in [docs/design.md](docs/design.md). Plans and runs: [docs/test-plans.md](docs/test-plans.md).

## Report

`tms-agent-kit report` writes one static HTML page, by default `reports/report.html` next to `tms-agent-kit.toml` (`reports_root` in the config). With `--run RUN_FILE`, the page is named after the run and opens on it: a report on one test session. It is built from the local files and `.tms/`, makes no network calls and includes unpushed edits. The page shows:

- totals and coverage by section, by the latest result of each case;
- what needs attention: failed and blocked cases, invalid files, unpushed changes, cases without steps, cases outside any plan, planned cases never run, duplicate names;
- plans with their runs, and runs with comments, step results and evidence images;
- time spent per case and per run, from the `Time:` lines of the run files;
- every case with its steps and result history; search and filters.

Links lead to Test IT and to the local files. Evidence links are relative, so keep the page inside the workspace or pass `-o` within it. The page is self-contained: open it from disk or publish it as a CI artifact.

## Agent skills

The package ships [Agent Skills](https://agentskills.io) (`src/tms_agent_kit/skills/`):

- `test`: `/test <product repository> [ticket]`: understand the change, update cases, build a plan, run it, build the HTML report, report back.
- `tms-test-cases`: write and edit cases, pull, diff, push.
- `tms-test-run`: plans, run files, executing cases, evidence, uploading results.

```bash
tms-agent-kit skills list
tms-agent-kit skills install [--dir DIR] [--force]   # default DIR: .claude/skills in the git root
```

`skills install` matches the skills to the installed tool version. It updates files that were not changed locally and keeps local edits unless `--force` is given. A manifest (`.tms-agent-kit.json`) in each skill folder records what was installed.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

## Supported TMS

| TMS | Status |
|---|---|
| Test IT (cloud, REST API v2) | pull, diff, push |
| Allure TestOps | planned |

## Safety

- Every write requires a dry-run and a confirmation token: `push`, `plan push` and `run push` write only with the token of the plan they printed.
- Before each write, the case version in the TMS is compared with the pulled one. Conflicts block the push.
- Deletion is disabled by default. `--allow-delete` archives cases; the tool never purges them.
- Writes are never retried.

## Design

See [docs/design.md](docs/design.md).

## Development rules

See [AGENTS.md](AGENTS.md).

## License

[Apache-2.0](LICENSE)
