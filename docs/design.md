# Design

Status: implemented for Test IT (cloud, REST API v2): `check`, `pull`, `diff`, `push`. Allure TestOps is out of scope for this document.

## Goals

- Mirror a TMS project's test cases as Markdown files in Git.
- Let an agent read and edit cases with local tools only.
- Push local edits back safely: dry-run first, explicit confirmation, conflict detection.

## Non-goals

- Reports and analytics. The TMS stays the source of truth for them. Test plans and runs are designed in [test-plans.md](test-plans.md).
- Real-time sync or a background daemon.
- Lossless editing of every TMS field. Fields the format does not model are preserved, not editable.

## Assumptions

- Implementation language: Python 3.11+.
- HTTP client: `httpx`. The generated `testit-api-client` is not used: it is large and pins many models; we need about ten endpoints.
- One local root per TMS project.

## Repository layout (user side)

```
tms-agent-kit.toml          # config, committed
.env                        # TESTIT_TOKEN, never committed
.tms/                       # tool state
  state.json                # project, section -> folder map, attribute names, shared step ids
  base/<case-uuid>.json     # raw TMS snapshot from the last pull (merge base)
  archived/                 # files of cases deleted in the TMS
  plans/<plan-uuid>.json    # what the last plan push sent
  runs/<run-uuid>.json      # what the last run push sent, with result ids
cases/                      # case root, configurable
  1001-top-level-case.md    # the single top-level section maps to the root itself
  Authentication/           # one folder per section
    1042-login-with-valid-password.md
    Password reset/
      1057-reset-via-email.md
```

- File name: `<globalId>-<slug>.md`. The global ID keeps names unique and greppable. The slug is derived from the case name on pull and cut at a word boundary after 60 characters.
- Identity is the `id` in frontmatter, not the path. Renaming a file does not create a new case.
- Section folder names are section names. Characters invalid in file names are replaced with `_`. Sibling sections whose folder names clash get an id suffix. `state.json` keeps the mapping; `check` prints it, together with local folders that are not sections yet.
- Pull creates a folder for every section, including empty ones, and removes empty folders of deleted sections. Git does not track empty folders.
- Files and folders whose names start with `_` or `.` are ignored. Users can keep notes there.
- Shared steps are ordinary files with `type: SharedSteps` in their section folder.

## Case file format

```markdown
---
tms: testit
id: 6f1c2e3a-0000-0000-0000-000000000000
global_id: 1042
version_id: 9a7b...           # from last pull; used for conflict detection
version_number: 7
modified_date: '2026-11-01T10:15:00Z'
type: TestCases               # TestCases | CheckLists | SharedSteps
state: Ready                  # NotReady | Ready | NeedsWork
priority: Medium              # Lowest | Low | Medium | High | Highest
duration_ms: 300000           # as in the API; must be > 0 for TestCases
tags: [auth, smoke]
attributes:
  Component: Login            # attribute names, mapped to ids via .tms/state.json
automated: false              # read-only
---

# Login with valid password

## Description

Free text.

## Preconditions

### Step 1

#### Action

User account `qa@example.com` exists.

## Steps

### Step 1

#### Action

Open the login page.

#### Expected

The login form is shown.

### Step 2

Shared step: 1003

### Step 3

#### Action

Submit valid credentials.

#### Expected

The dashboard opens.

#### Test data

`qa@example.com` / `secret`
```

Rules:

- Section order is fixed: Description, Preconditions, Steps, Postconditions. Empty sections are omitted, except Steps.
- The parser rejects unknown headings, text outside sections, and steps without field headings. Errors carry line numbers, including frontmatter errors.
- `type`, `state` and `priority` are matched case-insensitively (`high` reads as `High`). Pull writes the canonical form.
- Step order is file order. The number in `### Step N` is informational and rewritten on pull.
- Step fields: `Action`, `Expected`, `Test data`, `Comments`, in any order. Empty fields are omitted.
- A shared step reference is a step whose body is `Shared step: <globalId>`.
- Lines in text fields that start with `#` are written as `\#...` and read back without the backslash. Text therefore cannot break the structure. Lines inside code fences are not escaped.
- Frontmatter is optional. A new case needs only `# <name>` and, for TestCases, `duration_ms` in the frontmatter. Omitted fields take defaults: `TestCases`, `NotReady`, `Medium`.
- A new file named `<globalId>-...` whose global id belongs to a pulled case is rejected: it is most likely that case with its frontmatter removed, and pushing it would create a duplicate.
- Read-only frontmatter fields (`id`, `global_id`, `version_*`, `modified_date`, `automated`) are ignored on push. Editing them is reported by `diff` as a warning.
- The serializer is deterministic: pull of an unchanged case produces a byte-identical file.

### Lossy cases

A case is lossy when rendering and parsing it does not give back the same model, for example text with an unclosed code fence. Pull marks such files with `lossy: true`. Edits of the affected fields are blocked; edits of other fields are pushed normally.

Text is normalized for comparison: LF line endings, no trailing spaces, no blank lines at the edges. Fields that were not edited are sent back to the TMS in their raw form, so normalization never rewrites TMS data.

## Sync model

Three states per case:

- **local**: the Markdown file.
- **base**: `.tms/base/<id>.json`, the raw API response from the last pull.
- **remote**: the current TMS state.

The base snapshot serves two purposes:

1. `diff` compares local against base without network calls to show what the user changed.
2. `push` replaces only the changed fields in the fresh remote object. Fields the Markdown format does not model (iterations, auto tests, attachments, parameters, links, step ids) are kept, so a full-replace `PUT` does not erase them.

### Pull

1. `GET /api/v2/projects/{projectId}`: project UUID and attribute names.
2. `GET /api/v2/projects/{projectId}/sections`: section tree, mapped to folders.
3. `POST /api/v2/projects/{projectId}/workItems/search` with `{"isDeleted": false}`: short models of all cases. They include `versionId`.
4. `GET /api/v2/workItems/{id}` only for cases whose `versionId` differs from base, or that have no base.
5. For each changed case:
   - local file unchanged vs base: rewrite the file (moving it if the section or name changed) and the base.
   - local file changed or deleted: keep it and report a conflict. `pull --force` discards the local edit.
6. Cases missing from the search result were deleted in the TMS. An unchanged local file moves to `.tms/archived/`. A changed one is reported as a conflict.

An unchanged project costs three requests. Pull never overwrites a file that belongs to another case, even with `--force`.

Pagination: `Skip`/`Take` query parameters; totals in the `Pagination-Total-Items` response header.

### Diff

`tms-agent-kit diff [--remote]`

- Offline by default: local vs base.
- `--remote`: also fetch each changed case and flag conflicts (remote `versionId` differs from base, or the case is archived).
- Output: one line per change, then warnings, a total line and unified diffs. Line kinds: `section` (new section), `create`, `move` (section change only), `update`, `delete`. Each case line names the target section (`from ... to ...` for moves) and step counts.
- A warning flags a section that loses its last case and has no subsections. The tool never deletes sections; a human decides whether to remove it in the TMS.
- Exit code: 0 no changes, 1 changes, 2 errors or conflicts.

### Push

`tms-agent-kit push [--allow-delete] [--confirm TOKEN]`

1. Build the plan as `diff --remote` does. Errors or conflicts block the push.
2. Without `--confirm`: print the plan (one line per change, without diffs; `diff` shows them) and a token, write nothing, exit 1.
3. With `--confirm TOKEN`: rebuild the plan. The token is a hash of the plan, including file contents and base versions. A different token means the plan changed; push stops.
4. For each change:
   1. `GET /api/v2/workItems/{id}` and compare `versionId` with base. Skip the case on mismatch.
   2. Build the `PUT /api/v2/workItems` body from the fresh remote object plus the changed local fields.
   3. `PUT`. Then `GET` again and write the result to the file and base.
5. Report per-case results. A failure in one case does not roll back others.

The token makes the confirmation explicit without an interactive prompt. An agent shows the plan to the human and runs `--confirm` only after the human agrees.

Writes are never retried: a retried write could be applied twice.

Concurrency: the update request has no version field, so the TMS does not enforce optimistic locking. Step 4.1 narrows the race window but cannot close it.

### Create

A file without `id` in frontmatter is a new case. `push` creates it with `POST /api/v2/workItems`, then writes the returned case to `<globalId>-<slug>.md` and removes the draft file. The target section is the containing folder.

### Sections

A folder that holds a new or moved case but is not a section yet becomes a new section, and so do its missing parent folders. The plan lists them first; `push` creates them with `POST /api/v2/sections` (parents first) before writing cases. The section name is the folder name. A folder whose name would change on the next pull (for example, a trailing dot) is rejected. Empty folders are not pushed.

The tool never deletes, renames or moves sections. Section preconditions and postconditions are not synced.

### Delete

- Removing a local file does nothing on push by default; `diff` reports it as ignored.
- `push --allow-delete` archives such cases with `DELETE /api/v2/workItems/{id}`. This is a soft delete; the case can be restored in the TMS.
- The tool never calls `purge`.

### Move

Moving a file to another section folder changes `sectionId`. It is pushed as part of the normal `PUT`. Moving into a new folder creates the section first.

## Text fields

Text is passed through unchanged in both directions. Text sent through the API is stored as given: Markdown stays Markdown (verified, see below). The Test IT web editor stores HTML (TipTap markup such as `<p class="tiptap-text">`), so cases edited in the web UI show raw HTML in Markdown files. A converter is planned (candidates: `markdownify` for HTML to Markdown, `markdown-it-py` back); the lossy check will guard against conversions that do not round-trip.

## Code structure

- `config.py`: config and token loading.
- `model.py`: TMS-agnostic `Case`, `Step`, `CaseMeta`.
- `casefile.py`: Markdown rendering and parsing.
- `workspace.py`: files, `.tms/` state, section-to-folder mapping.
- `sync.py`: pull, plan, push.
- `plans.py`, `plan_sync.py`: plan files and plan push.
- `runs.py`, `run_sync.py`: run files and run push.
- `report.py`: the offline HTML report. Read-only over the same files; it reuses the diff logic of `sync.py` and `plan_sync.py` for unpushed changes.
- `testit/client.py`: HTTP client.
- `testit/mapping.py`: Test IT JSON <-> model, PUT/POST bodies, Test IT validation rules.

An adapter protocol will be extracted from `sync.py` and `testit/` when the second TMS is added. With one implementation the interface would be guesswork.

## Configuration

`tms-agent-kit.toml`:

```toml
tms = "testit"
base_url = "https://example.testit.software"
project_id = 42      # UUID or global id
root = "cases"
plans_root = "plans"      # test plan files, see test-plans.md
runs_root = "runs"        # test run files
reports_root = "reports"  # HTML pages written by `report`
```

The roots must not overlap.

Token: `TESTIT_TOKEN` environment variable, or the nearest `.env` from the config folder up to the git repository root (only the config folder outside a git repository). Sent as `Authorization: PrivateToken <token>`. Never written to config or state.

## Skills

`src/tms_agent_kit/skills/` holds the skills in the Agent Skills format (`SKILL.md` with `name` and `description` frontmatter, reference files next to it). `tms-agent-kit skills install` copies them into a repository; a manifest with file hashes lets updates keep local edits.

- `test`: the end-to-end flow for a product repository: understand the change, cases, plan, environment, run, report. It uses the two skills below.
- `tms-test-cases`: the case file format, writing guidelines, and the workflow: pull, edit, `diff`, show the `push` plan to the human, `push --confirm` after the human agrees.
- `tms-test-run`: plan and run files, executing cases, outcomes, evidence, `run push`. See [test-plans.md](test-plans.md).

The skills describe only CLI behavior and file formats. They do not depend on the agent. Project-specific data (attributes, sections) comes from `check` and the pulled files, not from the skills.

## Milestones

1. Package skeleton, config, Test IT client with pagination and auth. Done.
2. `pull` (full and incremental), serializer, deterministic output. Done.
3. `diff` (offline and `--remote`). Done.
4. `push` for updates, creates and opt-in deletes. Done.
5. Section preconditions/postconditions; HTML conversion if the web editor stores HTML.
6. Agent skills. Done.
7. Allure TestOps adapter.

Tests use an in-memory fake of the Test IT API. No test writes to a real TMS.

## Verified behavior

Checked on 2026-11-10 against a sandbox project:

1. Text sent through the API is stored and returned as given. Markdown is not converted to HTML. The web editor stores TipTap HTML; an empty field it touched is `<p class="tiptap-text"></p>`.
2. `PUT /api/v2/workItems` accepts new steps without `id`; the TMS assigns one. Existing step ids are kept when sent back.
3. A tags-only update creates a new version: `versionNumber` increments and `versionId` changes. Attribute-only edits are not verified; the project has no attributes.
4. The search response includes `versionId` and `versionNumber`. Pull compares them with base, so no `modifiedDate` filter is needed.
5. No rate-limit headers were observed. Limits are unknown.
6. `POST /api/v2/workItems` rejects `duration: 0` for TestCases. `diff` reports it before push.
7. For version 1 of a case, `versionId` equals the case `id`.
8. A project has one top-level section named after the project.
9. `PUT /api/v2/workItems` with another `sectionId` moves the case; no separate move call is needed.
10. `POST /api/v2/sections` with `name`, `projectId`, `parentId` and empty `attachments` creates a section; Cyrillic names with spaces round-trip as folder names.

## Open questions

1. Do attribute-only edits create a new version? Needs a project with attributes.
2. Whether `.tms/base/` is committed is the end user's choice. The tool does not depend on it: a missing base means "never pulled" and requires a pull before push.
