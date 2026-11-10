# Tests

Test cases, plans and runs, synced with the TMS by [tms-agent-kit](https://github.com/prog420/tms-agent-kit). Agent instructions: [AGENTS.md](AGENTS.md).

## Setup

```bash
uv sync                          # installs tms-agent-kit
cp .env.example .env             # set TESTIT_TOKEN
cd projects/<name>
uv run tms-agent-kit check       # verify the connection
uv run tms-agent-kit pull        # download the test cases
```

Add a project:

```bash
uv run tms-agent-kit init --project <name> --base-url https://<host> --project-id <id>
```

Then add it to the Projects table in `AGENTS.md`.

## Work with an agent

Start the agent in this repository and run:

```
/test ../my-product PROJ-123
```

The agent reads the change in the product repository, updates the cases, builds a plan, runs it and uploads the results to the TMS. Every TMS write is shown as a dry-run first.

Browser automation for UI checks comes from the Playwright MCP server in `.mcp.json`. It needs Node.js.

## Update

```bash
uv lock --upgrade-package tms-agent-kit && uv sync
uv run tms-agent-kit skills install
```
