---
name: test
description: Test a change in a product repository end to end with the test cases kept in this repository - understand the change, write or update test cases, build a test plan, run it, upload the results to the TMS and build an HTML report. Use when the user runs /test with a path to a product repository and optionally a ticket, branch or feature, or asks to test a feature, a branch or a ticket.
---

# Test a change

Usage: `/test <path to product repository> [ticket, branch or what to test]`.

You are the QA engineer. This repository holds the test cases, plans and runs (`projects/<name>/`); the product code is in the repository the user names. Use the `tms-test-cases` skill for case files and the `tms-test-run` skill for plans and runs. They describe the formats and commands; this skill describes the order of work.

## Ground rules

- The product repository is read-only for you: read code, docs and git history. Ask before anything that changes it or its environment: editing files, switching branches, installing dependencies, migrations. Never commit there.
- Follow `AGENTS.md` of this repository: which TMS project belongs to which product repository, and whether pushes need the user's go-ahead.
- If the user asked for part of the work only ("write cases", "run the regress"), do that part.

## 1. Understand the change

1. Read `AGENTS.md`, `CLAUDE.md` and `README.md` of the product repository. They are not loaded for you automatically.
2. Find the change:
   - a branch: `git -C <repo> log --oneline <main>..<branch>` and `git -C <repo> diff --stat <main>...<branch>`, then the relevant files;
   - a ticket: search the log and branch names for its key (`git -C <repo> log --all --oneline --grep <KEY>`);
   - a feature description: find the code that implements it.
3. Write down for yourself, in a few bullets: what changed for the user, the expected behavior, and what could break nearby.
4. If the expected behavior is unclear and no spec or ticket text explains it, ask the user. Do not guess requirements from the code alone: the code may be the bug.

## 2. Open the TMS project

1. Find the project folder for the product repository in `AGENTS.md`. If it is not listed, ask.
2. Work from that folder, so the CLI finds its `tms-agent-kit.toml`: `tms-agent-kit check`, then `tms-agent-kit pull`.

## 3. Cases

1. Search the existing cases for the area: section folders, case names, `grep` over the case texts.
2. For each behavior from step 1, decide: an existing case covers it, an existing case needs an update, or a new case is needed.
3. Add regression cases for the nearby areas that could break.
4. Write and update the cases (`tms-test-cases`). Put new cases in the section folder of the product area, in the style of their neighbors.
5. `tms-agent-kit diff`, then push. Before pushing, list for the user the new and changed cases, one line each.

## 4. Plan

1. Use one plan per ticket or feature: `plans/<ticket or feature>.md`. Update it if it exists.
2. Name: `<ticket>: <feature>`. Description: what changed (from step 1), the environment, what is out of scope.
3. `## Cases`: the new and changed cases first, then the regression cases, each with a short comment why it is there.
4. `tms-agent-kit plan diff`, then `plan push`.

## 5. Environment

1. You need a running product: a stand URL from the user, the plan or the product docs, or a local start with the product's documented scripts (ask first if it installs anything).
2. You need tools to drive it: browser automation for UI, HTTP requests for APIs, logs. If something is missing, stop and tell the user what exactly.
3. Write the environment into the plan description, so the run can be repeated.

## 6. Run

Follow `tms-test-run`: `run new`, execute each case, record each result with its time and evidence right away, then `run push`.

Measure the time of every case with `date +%s` before and after it, and write it as `Time:`. Never estimate it: the times are used for analytics. Also note the time when you start step 1 of this skill and when you finish step 7.

## 7. HTML report

After the run, build the report of this testing session from the project folder:

```bash
tms-agent-kit report --run runs/<run file>.md   # writes reports/<run file>.html
```

The page opens on the run: totals, time per case and in total, the slowest cases, comments and evidence. Reports go to `reports/` of the project and are not committed. Build it also when the run was not uploaded, and rebuild it after any correction of the run file. Do not write report pages by hand.

## 8. Report back

Tell the user:

- what changed in the product, in two or three sentences;
- cases created and updated (global ids), the plan number;
- run totals (`8 passed, 1 failed, 1 blocked`) and the run time (sum of `Time:`); the whole session time, from step 1 to step 7;
- the path of the HTML report;
- each failure: case, what happened, evidence file, and the suspected cause in the product code (`path:line`) if you found it; mark it as a guess unless you confirmed it;
- what was blocked or not covered, and why;
- suggested next steps, such as bugs to file. Do not file bugs or change the product unless asked.
