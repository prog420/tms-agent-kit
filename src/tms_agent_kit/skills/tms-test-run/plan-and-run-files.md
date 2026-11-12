# Plan and run files

## Plan file

```markdown
---
id: 9a1b...            # written by plan push; absent for a new plan
global_id: 1203        # written by plan push
build: 1.42.0          # optional
tags: [PROJ-123]      # optional
configurations: [Any] # optional; default: the project default configuration
---

# <plan name>

Optional description: goal, scope, environment.

## Cases

- Авторизация/Вход/
- 1050
- #1047 comment
- [Неверный пароль](../cases/Авторизация/Вход/1045-вход-с-неверным-паролем.md)
- `Авторизация/` checkbox and code spans work too
```

- Frontmatter is optional. Do not edit `id` and `global_id`.
- The plan name is the `# ` heading. Text before `## Cases` is the description.
- `## Cases` (or `## Кейсы`) is the last section. Every line in it is a list item (`-`, `*`, `+` or `1.`).
- The first reference in an item selects cases; the rest of the line is a comment:
  - a number, with or without `#`: a case global id;
  - a link or path to a case file;
  - a folder path relative to the case root, with a trailing `/`: every case in it, recursively.
- Shared steps, unknown ids and cases not yet pushed are errors. Duplicates are dropped.
- Configuration names must exist in the project; the error lists the known ones.
- Deleting a plan file archives the plan only with `plan push --allow-delete`.
- `tms-agent-kit plan list` shows the plans in the TMS and their local files.

## Run file

```markdown
---
id: c084...                    # written by run push
plan: plans/proj-123-regress.md
configuration: Chrome          # optional
---

# PROJ-123 regress 2026-11-10

## 1042 Login with a valid password

Result: passed
Time: 2m

## 1044 Login with a locked account

Result: failed
Time: 6m 15s

Free comment, several lines allowed.

![whole page](2026-11-10-proj-123-regress/1044-page.png)

- Step 1: passed
- Step 2: failed, no lock message ![step 2](2026-11-10-proj-123-regress/1044-step-2.png)

## 1047 Not executed yet

Result:
Time:
```

- Write it with `tms-agent-kit run new PLAN_FILE`. Do not change `plan` or `id`.
- `Result:` takes `passed`, `failed`, `blocked` or `skipped`, any case. Empty: not executed, not uploaded.
- `Time:` is the time spent on the case: `45s`, `4m 30s`, `1h 5m`, `4:30`. Optional; it needs a `Result:`. It is uploaded as the result duration (milliseconds in Test IT).
- Text under `Result:` and `Time:` is the comment. HTML comments `<!-- -->` are ignored.
- `- Step N: <outcome>[, comment]` sets the result of step N of the case (steps under `## Steps`; preconditions do not count). It needs a `Result:` for the case.
- Images and links to local files are attachments, paths relative to the run file. On a step line they belong to the step. Files must be inside the workspace and not in hidden folders. URLs stay in the comment.
- A case listed twice, a step listed twice, an unknown case or a step number beyond the case's steps is an error.
- Before `run push`, the plan must be pushed and unchanged (`plan diff` shows no changes), and the cases must match the last pull.
