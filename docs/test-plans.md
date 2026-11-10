# Design: test plans and runs

Status: draft, not implemented. Test IT only.

## Goal

An agent runs the full testing flow from the repository:

1. Describe a test plan as a Markdown file: which cases, which build.
2. Create, update or archive the plan in the TMS.
3. Execute the cases and record results locally.
4. Upload the results as a test run after one human confirmation.

A human then opens the TMS and sees the plan, its runs and results as if a tester had entered them.

## Non-goals

- Autotests and their results. Test IT handles them through its own adapters.
- Managing configurations, testers, test plan attributes and dynamic suites.
- Purging plans or runs. Archiving a plan needs an explicit opt-in flag.
- Real-time progress. Results reach the TMS only when the run is pushed.

## Test IT model

- A **test plan** holds **test suites**. A suite holds work items.
- A **test point** is a work item × a **configuration** inside a plan. Every project has a default configuration (for example `Any`).
- A **test run** belongs to a plan and holds one **test result** per selected test point.
- A result has an outcome (`Passed`, `Failed`, `Blocked`, `Skipped`, `InProgress`), a comment and per-step results.
- Plan status: `New`, `InProgress`, `Paused`, `Completed`. Run state: `NotStarted`, `InProgress`, `Stopped`, `Completed`.

The tool maps one plan file to one test plan with one `Custom` suite named like the plan.

### Manual results go through autotests

The public API v2 cannot record a result for a manual test point: `POST /api/v2/testRuns/byConfigurations` and `byWorkItems` reject non-automated work items, and result endpoints need an autotest. So the agent acts as an automation runner:

- Before the first run, each case in the plan gets its own autotest, linked to the case: external id `tms-agent-kit-<globalId>`, namespace `tms-agent-kit`, one autotest step per case step (`Step 1`, `Step 2`, ...).
- Linking marks the case automated (`automated: true` in the case file) and creates a new case version. `run push` writes the updated case files and base snapshots, so no extra pull is needed.
- A case already linked to another autotest is rejected: it has real automation, and agent results would mix with it.
- The tool never deletes autotests. Unlinking is done in the TMS UI.

## Layout (user side)

```
plans/                          # configurable: plans_root
  proj-123-regress.md
runs/                           # configurable: runs_root
  2026-11-10-proj-123-regress.md
.tms/
  plans/<plan-uuid>.json        # TMS snapshot from the last plan push (merge base)
```

## Plan file

```markdown
---
id: 9a1b...            # written by push; absent for a new plan
global_id: 1203        # written by push
build: 1.42.0
tags: [PROJ-123]
configurations: [Any]  # default: the project default configuration
---

# PROJ-123 regress

Optional description: goal, scope, environment.

## Cases

- Авторизация/Вход/
- 1041
- #1047 login with an expired password
- [Неверный пароль](../cases/Авторизация/Вход/1045-вход-с-неверным-паролем.md)
```

Rules:

- Frontmatter is optional for a new plan. The plan name is the `# ` heading.
- Text between the name and `## Cases` is the description.
- Each list item under `## Cases` selects cases. The first reference in the item wins; the rest of the line is a free comment.
  - A number, with or without `#`, is a case global id.
  - A Markdown link or a path to a `.md` case file selects that case.
  - A path to a folder (relative to the case root, trailing `/`) selects every case in it, recursively.
- Unresolved references, unknown ids and shared steps are errors with line numbers.
- Order is file order; duplicates are dropped.
- The tool resolves references against local case files, so plans can only reference pulled or pushed cases.

## Commands

```bash
tms-agent-kit plan list                         # plans in the TMS: status, build, local file (read-only)
tms-agent-kit plan diff                         # what plan push would change (dry-run)
tms-agent-kit plan push [--allow-delete] [--confirm TOKEN]

tms-agent-kit run new PLAN_FILE [--configuration NAME]   # offline: write a run file to fill in
tms-agent-kit run push RUN_FILE [--confirm TOKEN]
```

Exit codes and the confirmation token work as in `push`.

### plan push

| Local | TMS action |
|---|---|
| New file (no `id`) | `POST /api/v2/testPlans` with one suite, then `POST /api/v2/testSuites/{id}/workItems` |
| Changed name, description, build, tags | `PUT /api/v2/testPlans` |
| Changed case list or configurations | set suite work items / configurations |
| File deleted, with `--allow-delete` | `DELETE /api/v2/testPlans/{id}` (archive) |

- Conflict check: name, description, build, tags, cases and configurations in the TMS must match the snapshot in `.tms/plans/<id>.json`. Otherwise the push is blocked and the line names the changed fields.
- A plan with results keeps them when cases are added. Removing a case removes its test points; the plan line warns about it.
- The plan line shows the result: `create plans/proj-123-regress.md  (8 cases x default configuration)`.

### run new

Writes `runs/<date>-<plan file name>.md` (`-2`, `-3` if taken) from the local plan file and case files. No network calls. `configuration` is written only when `--configuration` is given.

```markdown
---
plan: plans/proj-123-regress.md
configuration: Any
---

# PROJ-123 regress 2026-11-10

## 1042 Вход с верным паролем

Result:

## 1045 Вход с неверным паролем

Result: failed

No error message; the form is cleared and the page shows "500 Internal Server Error".

- Step 2: failed, no error message ![no message](2026-11-10-proj-123-regress/step-2.png)

[console log](2026-11-10-proj-123-regress/console.txt)
```

Rules for filling in:

- `Result:` takes `passed`, `failed`, `blocked` or `skipped`, case-insensitive. Empty means not executed; such cases are not uploaded.
- Text after the `Result:` line is the result comment.
- `- Step N: <outcome>[, comment]` lines set step results. Steps without a line take no step result.
- The agent reads the case files to execute the steps. The run file only holds results.

### Attachments

- A Markdown image or link to a local file is an attachment: on a `- Step N:` line, of that step; elsewhere in the case section, of the result. URLs and `#anchors` stay in the comment.
- Paths are relative to the run file, as Markdown renders them, so screenshots show in Git previews.
- The comment sent to the TMS drops images, keeps link texts, and drops lines that hold only attachment links.
- Files must be inside the workspace and outside hidden files and folders, so `.env`, `.git` and `.tms` are never uploaded. A missing file is an error with a line number.
- The dry-run lists the number of files per case and a total line with the unique files and their size. The token covers file contents.
- `run new` suggests a folder named like the run file for screenshots and logs.
- `run push` uploads each file once with `POST /api/v2/attachments` before it creates the run, so a failed upload leaves no empty run. Results and step results reference the file ids.
- On a re-push, new result attachments are uploaded and linked with `PUT /api/v2/testResults/{id}/attachments/{attachmentId}`. Pushed attachments cannot be removed (the tool deletes nothing in the TMS). Step attachments are part of the step results and are fixed after push. A file changed after push is not uploaded again.

### run push

1. Parse the run file. The plan must be pushed and have no pending local changes (`plan diff`). Executed cases must be in the plan; step numbers must exist in the case.
2. Check the TMS: the configuration exists and the plan has test points for it; each executed case has the pulled version (otherwise run `pull`) and no autotests other than its own.
3. Without `--confirm`: print one line per executed case (outcome, step results, autotest to create or link) and a total (`1 failed, 2 passed, 2 not executed`), then a token. Write nothing.
4. With `--confirm`:
   1. Create the autotest `tms-agent-kit-<globalId>` with steps `Step 1..N`, or link an existing one; fix its steps if the case step count changed. Rewrite the linked case files from the TMS (new version, `automated: true`).
   2. Create a test run for the executed cases: `POST /api/v2/testRuns/byConfigurations` with one test point selector. The plan moves to `InProgress` by itself.
   3. Write the run `id` into the run file frontmatter.
   4. Send all results in one request: `POST /api/v2/testRuns/{id}/testResults` with outcome, message (the comment) and step results by step title.
   5. Save `.tms/runs/<id>.json`: what was sent and the result ids. The run completes by itself once every test point has a result.
5. Pushing a run file that has an `id` again sends changed outcomes and comments with `PUT /api/v2/testResults/{id}`. A result changed in the TMS since the push is a conflict. Step results cannot change after push, and a pushed result cannot be removed; start a new run with `run new` instead.

Requests are sequential. Writes are never retried. If sending results fails, the run stays open in the TMS without results; remove the `id` line and push again to create a new run.

## Safety

- Every plan and run write needs the dry-run output and the confirmation token, as for cases. One confirmation covers a whole run.
- Results are attributed to the owner of the API token.
- Linking autotests changes cases (they become automated). The run plan lists the cases that will be linked.
- Plans are archived only with `--allow-delete`. Runs are never deleted.

## Verified behavior

Checked on 2026-11-10 against a sandbox project with throwaway plans and cases, archived afterwards:

1. `POST /api/v2/testSuites/{id}/workItems` replaces the suite's work items. The project default configuration is assigned to a new suite.
2. `testRuns/byConfigurations` rejects manual work items: "TestRun cannot be created with non-automated TestPoint".
3. Linking an autotest (`workItemIdsForLinkWithAutoTest`) sets `isAutomated` and bumps the case version; steps are kept.
4. A run on automated test points creates the run and moves a `New` plan to `InProgress`; no `start` call is needed.
5. `POST /api/v2/testRuns/{id}/testResults` stores outcome, message and autotest step results; the test point status follows the outcome.
6. A run completes by itself when all its test points have results; `complete` then returns 400.
7. `PUT /api/v2/testResults/{id}` sets `comment` and changes the outcome, also on a completed run. The `message` sent with the result stays as it was.
8. Test IT stores plan tags in lower case. Tag comparison is case-insensitive.
9. Test points created while a case was manual accept a run after the case is linked to an autotest; the suite does not need to be set again.
10. `POST /api/v2/attachments` (multipart, field `file`) returns the attachment id. Ids in `attachments` of a result and of its `stepResults` are stored. The result lists all of them; `autoTestStepResults` keeps step attachments on their step.
11. `PUT /api/v2/testResults/{id}` without `attachments` keeps the existing attachments.
12. `PUT /api/v2/testResults/{id}/attachments/{attachmentId}` adds an uploaded file to a result, also on a completed run.

## Milestones

1. Plan file parser and `plan diff` (offline). Done.
2. `plan push` and `plan list`. Done.
3. `run new` and `run push` with autotest linking. Done.
4. Result attachments (screenshots). Done. Agent skills for cases and test runs.
5. Optional: pull plans created in the TMS UI into plan files.
