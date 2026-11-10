---
name: tms-test-cases
description: Read, write, review and reorganize test cases kept as Markdown files that tms-agent-kit syncs with a TMS (Test IT). Use when the user asks to create, edit, move or delete test cases, to cover a feature with test cases, or to pull or push cases between the repository and Test IT.
---

# Test cases in Markdown, synced with the TMS

Test cases live as Markdown files in the repository. The `tms-agent-kit` CLI pulls them from the TMS and pushes local edits back. You edit files with your normal tools; the CLI is the only way to talk to the TMS.

## Before you start

1. Find `tms-agent-kit.toml`. Its `root` is the case folder (default `cases/`). Run commands from the folder with the config or below it.
2. Run `tms-agent-kit check`. It verifies the token and lists section folders. If `AGENTS.md` says how to run the tool (for example `uv run tms-agent-kit`), do that. If the command is not found, ask the user how the tool is installed.
3. The API token is in `TESTIT_TOKEN` or `.env`. Never read, print, copy or commit `.env`.
4. Run `tms-agent-kit pull` before editing, so you work on the current TMS state.

## Layout

```
cases/                       # one folder per TMS section; the folder name is the section name
  Авторизация/
    Вход/
      1042-вход-с-верным-паролем.md   # <globalId>-<slug>.md, one case per file
.tms/                        # tool state: never edit
```

- The folder a file is in is its section. Moving the file moves the case. A new folder becomes a new section on push.
- Files and folders starting with `_` or `.` are ignored. Keep notes in `_notes.md` if you need them.
- Shared steps are files with `type: SharedSteps`. Do not edit them unless asked: many cases use them.

## Case file

```markdown
---
duration_ms: 300000
priority: high
tags: [smoke]
---

# Login with a valid password

## Preconditions

### Step 1

#### Action

User `qa@example.com` exists.

## Steps

### Step 1

#### Action

Open the login page and submit valid credentials.

#### Expected

The dashboard opens.
```

This is a complete new case. Pull adds the rest of the frontmatter after push. The full format, all fields and allowed values: [case-format.md](case-format.md).

Key rules:

- A new case is a new file without `id`. Never copy an existing case file with its frontmatter: the copy has the same `id` and is the same case. Copy the body only.
- Do not edit read-only fields: `id`, `global_id`, `version_id`, `version_number`, `modified_date`, `automated`.
- Sections: `## Description`, `## Preconditions`, `## Steps`, `## Postconditions`, in this order. Each step is `### Step N` with `#### Action`, `#### Expected`, `#### Test data`, `#### Comments`.
- A line in a text field that starts with `#` must be written as `\#`.
- A test case (`type: TestCases`, the default) needs `duration_ms` greater than 0.

## Writing good cases

- One behavior per case. The name says what is checked, not how: "Limit card shows the provider name".
- Steps are concrete actions with concrete data. Expected results are observable: what appears, what text, what state.
- Put setup in Preconditions, not in the first steps. Put shared flows (login) in shared steps if the project has them: `Shared step: <globalId>` as the step body.
- Follow the existing naming, tags and folder structure of the project. Read a few neighboring cases first.
- Base cases on the source of truth the user gives you (spec, ticket, code). Do not invent behavior; ask when the expected result is unclear.

## Sync workflow

```bash
tms-agent-kit pull                 # TMS -> files
# edit, create, move files
tms-agent-kit diff                 # local changes vs last pull; fix errors it reports
tms-agent-kit push                 # prints the plan and a confirmation token; writes nothing
tms-agent-kit push --confirm TOKEN # applies exactly the plan that was printed
```

- `diff` and `push` report format errors with file and line. Fix them and run again.
- Show the `push` plan to the user and wait for their go-ahead before `--confirm`, unless the user told you to push without asking.
- The token covers the plan. If you edit anything after `push`, run `push` again for a new token.
- After push, new files are renamed to `<globalId>-<slug>.md`. Re-read files from disk before editing again.
- Exit codes: `0` nothing to do or success, `1` changes pending (normal after `diff` and `push` without `--confirm`), `2` error or conflict.

## Conflicts and deletes

- A conflict means the case changed in the TMS after your last pull. Do not force anything. Tell the user; `pull` updates unchanged files, and `pull --force` discards local edits only with the user's consent.
- Deleting a file does nothing by default. `push --allow-delete` archives those cases in the TMS. Use it only when the user asked to delete cases.
- The tool never deletes sections. An empty section is reported as a warning; tell the user.

## Report back

Tell the user what you created, changed and moved (counts and file names), the push result, and anything you could not do.
