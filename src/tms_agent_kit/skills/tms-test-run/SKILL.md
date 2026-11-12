---
name: tms-test-run
description: Run manual test plans with tms-agent-kit and report results to a TMS (Test IT). Use when the user asks to run a regression, smoke test or test plan, to check a feature against its test cases, to create or change a test plan, or to upload test results with screenshots so people can review the run in Test IT.
---

# Test plans and runs

You act as the tester. A plan file lists the cases; a run file records what you observed; `tms-agent-kit` turns both into a test plan and a test run in the TMS, where people review the results.

The cases are Markdown files synced by the same tool. Edit them with the `tms-test-cases` skill, if you have it.

## Before you start

1. Run `tms-agent-kit check`, then `tms-agent-kit pull` (or the way `AGENTS.md` says to run the tool, for example `uv run tms-agent-kit`). Pull updates case files that have no local edits and never overwrites local edits. A plan can only reference cases that are in the TMS.
2. Find the folders in `tms-agent-kit.toml`: `root` (cases, default `cases/`), `plans_root` (default `plans/`), `runs_root` (default `runs/`), `reports_root` (HTML reports, default `reports/`).
3. Never read, print or commit `.env`: it holds the API token.

Exit codes of every command: `0` done or nothing to do, `1` changes pending (the normal result of `plan diff`, and of `plan push` and `run push` without `--confirm`), `2` error or conflict.

## 1. The plan

A plan file in `plans/`:

```markdown
---
tags: [PROJ-123]
---

# PROJ-123 regress

Goal, scope, environment (stand URL, accounts, build).

## Cases

- Авторизация/Вход/          — a folder: every case in it
- 1050                       — a case global id
- #1047 free comment         — the rest of the line is a comment
```

Then:

```bash
tms-agent-kit plan diff                   # offline, all plan files: what plan push would change; errors with line numbers
tms-agent-kit plan push                   # prints the changes and a token
tms-agent-kit plan push --confirm TOKEN   # creates or updates the plan in the TMS
```

If the user names an existing plan file, use it. Do not change its case list without being asked. Format details: [plan-and-run-files.md](plan-and-run-files.md).

## 2. The run file

```bash
tms-agent-kit run new plans/proj-123-regress.md   # writes runs/<date>-proj-123-regress.md
```

The file has one `## <globalId> <name>` section per case with an empty `Result:` and `Time:`. The run uses the plan's first configuration or the project default; `--configuration NAME` picks another.

One run file is one test run in the TMS. If a run file of this plan has no `id` yet, it was not uploaded: continue it instead of starting another. A file with an `id` is uploaded; to test again, run `run new`, which adds `-2`, `-3` to the file name.

## 3. Execute the cases

First make sure you can run the cases: the environment (stand URL, build, accounts, test data) comes from the plan description or from the user, and you need tools to drive it. If you cannot run any case, stop and ask the user. Do not fill a whole run with `blocked` unless the user wants that record.

For each case, in file order:

1. Read the case file: preconditions, every step's action, expected result and test data.
2. Start the clock: `date +%s`. Keep the number.
3. Do the steps with the tools you have: browser automation, HTTP requests, the CLI, logs. Use the environment from the plan description or from the user.
4. Compare what you observe with each expected result.
5. Stop the clock (`date +%s` again) once the result and the evidence are clear.
6. Write the result and the time into the run file right away, before the next case. A long run then survives an interruption.

```markdown
## 1045 Login with a wrong password

Result: failed
Time: 4m 30s

No error message; the page shows "500 Internal Server Error".

- Step 2: failed, no error message ![page after step 2](2026-11-10-proj-123-regress/1045-step-2.png)

[browser console](2026-11-10-proj-123-regress/1045-console.txt)
```

Outcomes:

| Result | When |
|---|---|
| `passed` | You did every step and saw every expected result. |
| `failed` | An expected result did not happen: a product defect. Say what you saw. |
| `blocked` | You could not finish the case: the environment is down, a precondition cannot be met, you lack a tool or access. Say what blocked you. |
| `skipped` | You decided not to run it and the reviewer should see that (out of scope, agreed with the user). Say why. |
| empty | Not executed. Nothing is uploaded for the case. |

Rules:

- Never mark a case `passed` without doing its steps and seeing the result. If you could not check something, the case is `blocked`, not `passed`.
- Do not change a case to make it pass. If a case looks wrong or outdated, record what happened and tell the user.
- Comments state facts: what you did, what you saw, what you expected. Include error texts and URLs.
- `Time:` is the time spent on the case, from the clock: `45s`, `4m 30s`, `1h 5m`. It covers the steps, checks and evidence of this case. It excludes waiting for the user and the environment setup shared by all cases. If you did not measure it, leave `Time:` empty; never estimate. The time is uploaded as the result duration and used for analytics.
- Add a `- Step N: <outcome>, <comment>` line for the step that failed or blocked. `N` counts the steps under `## Steps` of the case file; preconditions do not count. Lines for passed steps are optional.

Evidence:

- Save screenshots and logs next to the run file, in a folder named like it: `runs/2026-11-10-proj-123-regress/`. Name files after the case and step: `1045-step-2.png`.
- Link them with relative paths. An image or link to a local file becomes an attachment: on a `- Step N:` line, of that step; elsewhere, of the result. The links are removed from the comment sent to the TMS.
- Attach evidence for every failed case and for a case blocked by the product. For passed cases, attach a screenshot when the expected result is visual. A block of the whole environment (stand down, no access) needs no file: state the reason in the comment.
- One file may be linked from several cases; it is uploaded once.
- Evidence is uploaded to the TMS and may be committed. Do not capture tokens, passwords, cookies or personal data. Crop or skip such screenshots.

## 4. Upload

```bash
tms-agent-kit run push runs/2026-11-10-proj-123-regress.md                 # prints the results and a token
tms-agent-kit run push runs/2026-11-10-proj-123-regress.md --confirm TOKEN # uploads them
```

- Errors (unknown case, wrong step number, missing file) have line numbers. Fix them and run again.
- Show the summary to the user and wait for their go-ahead before `--confirm`, unless the user told you to upload without asking.
- Test IT accepts results only for automated cases. The first upload links each executed case to its own autotest `tms-agent-kit-<globalId>`, and the case becomes automated in the TMS. The dry-run lists these cases. Tell the user about it the first time.
- After the upload, the run file has an `id`, and the TMS shows the run in the plan.

Corrections after upload: change `Result:`, `Time:` or the comment, or add attachment links, and run `run push` again. Step results cannot change, and uploaded results and files cannot be removed. To test again, start a new run with `run new`.

## 5. HTML report

When the run is finished (uploaded, or complete if the user did not want an upload), build the report:

```bash
tms-agent-kit report --run runs/2026-11-10-proj-123-regress.md   # writes reports/2026-11-10-proj-123-regress.html
```

It is offline and reads only local files. The page opens on this run: results, time per case, evidence. It also shows the whole project: coverage, other runs, what needs attention. Reports are not committed. Do not write your own report pages; extend the run comments instead.

## Report back

Tell the user:

- the totals (`5 passed, 1 failed, 1 blocked`), the run time (the `; time` part of the `run push` total) and the plan number in the TMS;
- each failed and blocked case: global id, name, what happened, the evidence file;
- cases you did not execute and why;
- anything that looked wrong in the cases themselves;
- the path of the HTML report.
