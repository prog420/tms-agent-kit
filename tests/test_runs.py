import pytest

from tms_agent_kit.frontmatter import FormatError
from tms_agent_kit.runs import (
    CaseResult,
    StepResult,
    duration,
    format_duration,
    parse_run,
    render_template,
    set_run_id,
)

FULL = """---
plan: plans/regress.md
configuration: Chrome
---

# Regress 2026-11-10

<!-- a hint
spanning lines -->

## 1042 Balance card

Result: failed

The card is not shown.
Second line.

- Step 2: failed, nothing renders
* step 1: Passed

## #1043 Quota card

Result: PASSED

## 1044 Not run

Result:
"""


def test_parse_full_run() -> None:
    run = parse_run(FULL)
    assert (run.plan, run.configuration, run.id) == ("plans/regress.md", "Chrome", None)
    assert run.name == "Regress 2026-11-10"
    balance, quota, not_run = run.results
    assert balance == CaseResult(
        global_id=1042,
        title="Balance card",
        line=11,
        outcome="Failed",
        comment="The card is not shown.\nSecond line.",
        steps=(StepResult(1, "Passed"), StepResult(2, "Failed", "nothing renders")),
    )
    assert (quota.global_id, quota.outcome, quota.comment) == (1043, "Passed", "")
    assert not_run.outcome is None
    assert run.executed == [balance, quota]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# Run\n", "frontmatter must name the plan file"),
        ("---\nplan: p.md\n---\n", "missing '# <run name>'"),
        ("---\nplan: p.md\n---\ntext\n", "line 4: a run file must start with '# <run name>'"),
        ("---\nplan: p.md\nfoo: 1\n---\n# R\n", "line 3: unknown frontmatter keys: foo"),
        ("---\nplan: p.md\n---\n# R\nnote\n# Other\n## 1 A\n", "line 6: run notes use '## '"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\nResult: ok\n", "line 6: result must be one of"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\nResult: passed\nResult: failed\n", "duplicate"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\n- Step 1: passed\n", "need a 'Result:'"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\nTime: 5m\n", "'Time:' needs a 'Result:'"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\nResult: passed\nTime: soon\n", "line 7: time"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\nResult: passed\nTime: 1m\nTime: 2m\n", "dupl"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\n## 1 B\n", "line 6: case #1 is listed twice"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\n## B\n", "line 6: expected '## <case global id>"),
        (
            "---\nplan: p.md\n---\n# R\n## 1 A\nResult: failed\n- Step 1: failed\n- Step 1: ok\n",
            "line 8: result must be one of",
        ),
    ],
)
def test_run_format_errors(text: str, message: str) -> None:
    with pytest.raises(FormatError, match=message):
        parse_run(text)


def test_template_round_trip_and_run_id() -> None:
    text = render_template("plans/regress.md", None, "Regress", [(1, "A"), (2, "B")])
    run = parse_run(text)
    assert run.plan == "plans/regress.md"
    results = [(r.global_id, r.title, r.outcome) for r in run.results]
    assert results == [(1, "A", None), (2, "B", None)]

    with_id = set_run_id(text, "r1")
    assert with_id.startswith("---\nid: r1\nplan: plans/regress.md\n---\n")
    assert set_run_id(with_id, "r2").count("id: ") == 1
    assert parse_run(with_id).id == "r1"


def test_links_to_local_files_are_attachments() -> None:
    run = parse_run(
        "---\nplan: p.md\n---\n# R\n## 1 A\nResult: failed\n\n"
        "Card missing ![shot](shots/a.png) see [log](<logs/con sole.txt>) and [doc](https://x.y/z).\n"
        "![](shots/b%20c.png)\n"
        "[top](#anchor)\n\n"
        "- Step 2: failed ![s](shots/s2.png), nothing\n"
    )
    (case,) = run.results
    assert case.comment == "Card missing see log and [doc](https://x.y/z).\n[top](#anchor)"
    assert [(a.path, a.line) for a in case.attachments] == [
        ("shots/a.png", 8),
        ("logs/con sole.txt", 8),
        ("shots/b c.png", 9),
    ]
    (step,) = case.steps
    assert (step.number, step.outcome, step.comment) == (2, "Failed", "nothing")
    assert [a.path for a in step.attachments] == ["shots/s2.png"]


def test_step_attachment_needs_an_outcome() -> None:
    text = "---\nplan: p.md\n---\n# R\n## 1 A\nResult: failed\n- Step 1: ![s](s.png)\n"
    with pytest.raises(FormatError, match="line 7: attachments of a step need its outcome"):
        parse_run(text)


@pytest.mark.parametrize(
    ("value", "seconds", "shown"),
    [
        ("45s", 45, "45s"),
        ("4m 30s", 270, "4m 30s"),
        ("4m30sec", 270, "4m 30s"),
        ("12 min", 720, "12m"),
        ("1h 5m", 3900, "1h 5m"),
        ("4:30", 270, "4m 30s"),
        ("1:04:30", 3870, "1h 4m"),
        ("", None, None),
    ],
)
def test_duration(value: str, seconds: int | None, shown: str | None) -> None:
    assert duration(value, 1) == seconds
    if seconds is not None:
        assert format_duration(seconds) == shown


@pytest.mark.parametrize("value", ["soon", "5 minutes", "30s 4m", "4:75", "1:60:00", "m"])
def test_bad_duration(value: str) -> None:
    with pytest.raises(FormatError, match="time must look like"):
        duration(value, 1)


def test_time_per_case_and_run_total() -> None:
    text = FULL.replace("Result: failed\n", "Result: failed\nTime: 4m 30s\n").replace(
        "Result: PASSED\n", "Result: PASSED\ntime: 90s\n"
    )
    run = parse_run(text)
    balance, quota, _ = run.results
    assert (balance.seconds, quota.seconds) == (270, 90)
    assert balance.comment == "The card is not shown.\nSecond line."
    assert run.seconds == 360
    assert parse_run(FULL).seconds is None


def test_run_notes() -> None:
    text = FULL.replace(
        "spanning lines -->\n",
        "spanning lines -->\n\n## Summary\n\nAll good.\n\n"
        "```\n# a shell comment\n## 7 not a case\n```\n",
    )
    run = parse_run(text)
    assert run.notes == "## Summary\n\nAll good.\n\n```\n# a shell comment\n## 7 not a case\n```"
    assert [r.global_id for r in run.results] == [1042, 1043, 1044]
    assert parse_run(FULL).notes == ""
