# Bug files

One file per product defect: `bugs/<slug>.md` under the project (`bugs_root` in `tms-agent-kit.toml`). The slug is a few English words about the defect: `model-label-after-switch.md`. Evidence goes into a folder named like the file: `bugs/model-label-after-switch/`.

```markdown
---
severity: minor          # critical | major | minor | trivial
status: open             # open | fixed | closed; default open
cases: [24488]           # global ids of the cases that show the defect
found: 2026-11-10        # the date of the run that found it
ticket: HH-322863        # optional: the change under test
url: https://jira/BUG-1  # optional: the tracker issue, once the user filed it
---

# After a model switch, the answer is labeled with the previous model

One or two sentences: what is wrong and who notices it. Whether it is related to the ticket.

## Environment

Stand, build or commit, user. No tokens or passwords.

## Steps

1. Open the agent and start a new topic.
2. Pick "Claude Haiku 4.5" and send "Capital of Italy?".
3. In the same topic pick "Qwen3.8" and send "Capital of Spain?".

## Expected

The second answer is labeled "Qwen3.8", also after a reload.

## Actual

The second answer is labeled "Claude Haiku 4.5". After a reload the label stays: the message is stored with the wrong model.

## Cause

`src/store/message.ts:120` keeps the model of the topic. Confirmed by the code, or "a guess".

## Evidence

![label after the switch](model-label-after-switch/02-answer.png)

- Run `runs/2026-11-10-HH-322863.md`, case 24488, step 3.
- Request ids, log lines (masked).
```

Rules:

- Frontmatter: `severity` is required. Unknown keys are errors. The report lists invalid files.
- The first `# ` line is the title: the defect in one line, as the user sees it.
- `cases` links the bug to the cases. The report shows the bug next to their results. Do not link the bug file from a run file: a link to a local file in a case section is uploaded to the TMS as an attachment. Run notes may link it.
- Severity:
  - `critical`: data loss, security, the main flow is down, no workaround;
  - `major`: a feature is broken or gives wrong results;
  - `minor`: wrong behavior with a small impact or an easy workaround;
  - `trivial`: cosmetics, texts.
- Status:
  - `open`: reproduces;
  - `fixed`: a later run passed the cases; write the run in the body;
  - `closed`: not a defect, a duplicate or won't fix; write why in the body.
- Copy the key screenshots and logs into the bug folder. Run evidence under `runs/` is not committed.
- Bug files may be committed. Mask tokens, passwords, cookies and personal data, as in evidence.
- The tool never sends bug files to the TMS or a tracker. File a tracker issue only when the user asks; then put its link into `url`.
