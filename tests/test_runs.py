import pytest

from tms_agent_kit.frontmatter import FormatError
from tms_agent_kit.runs import (
    CaseResult,
    StepResult,
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
        ("---\nplan: p.md\n---\n# R\nnote\n## 1 A\n", "line 5: text before the first"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\nResult: ok\n", "line 6: result must be one of"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\nResult: passed\nResult: failed\n", "duplicate"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\n- Step 1: passed\n", "need a 'Result:'"),
        ("---\nplan: p.md\n---\n# R\n## 1 A\n## 1 B\n", "line 6: case #1 is listed twice"),
        ("---\nplan: p.md\n---\n# R\n## A\n", "line 5: expected '## <case global id> <name>'"),
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
