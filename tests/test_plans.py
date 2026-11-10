import pytest

from tms_agent_kit.frontmatter import FormatError
from tms_agent_kit.plans import CaseRef, PlanMeta, parse_plan, set_plan_meta

FULL = """---
id: 9a1b
global_id: 1203
build: 1.42
tags: [b, a]
configurations: [Any, Chrome]
---

# PROJ-123 regress

Goal: limit cards.

```
## Cases inside a fence is description
```

## Cases

- Chat/Limits/
- 1041
* #1047 comment after the id
1. [Login](../cases/x.md)
- [ ] `Chat/Errors/` checkbox item
"""


def test_parse_full_plan() -> None:
    parsed = parse_plan(FULL)
    assert parsed.meta == PlanMeta(id="9a1b", global_id=1203)
    plan = parsed.plan
    assert plan.name == "PROJ-123 regress"
    assert plan.build == "1.42"
    assert plan.tags == ("a", "b")
    assert plan.configurations == ("Any", "Chrome")
    assert plan.description.startswith("Goal: limit cards.")
    assert "## Cases inside a fence" in plan.description
    assert [r.text for r in plan.refs] == [
        "Chat/Limits/",
        "1041",
        "#1047 comment after the id",
        "[Login](../cases/x.md)",
        "`Chat/Errors/` checkbox item",
    ]
    assert plan.refs[0] == CaseRef(line=19, text="Chat/Limits/")


def test_minimal_plan_without_frontmatter() -> None:
    parsed = parse_plan("# Smoke\n\n## Кейсы\n\n- 1\n")
    assert parsed.meta == PlanMeta()
    assert parsed.plan.name == "Smoke"
    assert parsed.plan.configurations == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "missing '# <name>'"),
        ("text\n", "line 1: a plan must start with '# <name>'"),
        ("# Plan\n\ntext\n", "missing '## Cases'"),
        ("# Plan\n## Cases\n", "'## Cases' is empty"),
        ("# Plan\n## Cases\n- 1\nnote\n", "line 4: expected a list item"),
        ("# Plan\n## Cases\n- 1\n## Notes\n", "line 4: '## Cases' must be the last section"),
        ("# Plan\n## Cases\n-  \n", "line 3: empty list item"),
        ("---\nfoo: 1\n---\n# Plan\n## Cases\n- 1\n", "line 2: unknown frontmatter keys: foo"),
        ("---\ntags: x\n---\n# Plan\n## Cases\n- 1\n", "line 2: tags must be a list"),
        ("---\nbuild: [1]\n---\n# Plan\n## Cases\n- 1\n", "line 2: build must be a string"),
    ],
)
def test_plan_format_errors(text: str, message: str) -> None:
    with pytest.raises(FormatError, match=message):
        parse_plan(text)


def test_set_plan_meta_keeps_the_file() -> None:
    text = "---\nbuild: '1.0'\nid: old\n---\n\n# Plan\n\n## Cases\n\n- 1\n"
    updated = set_plan_meta(text, PlanMeta(id="new", global_id=7))
    assert updated == "---\nid: new\nglobal_id: 7\nbuild: '1.0'\n---\n\n# Plan\n\n## Cases\n\n- 1\n"
    assert parse_plan(updated).meta == PlanMeta(id="new", global_id=7)


def test_set_plan_meta_adds_frontmatter() -> None:
    updated = set_plan_meta("# Plan\n## Cases\n- 1\n", PlanMeta(id="x", global_id=1))
    assert updated == "---\nid: x\nglobal_id: 1\n---\n\n# Plan\n## Cases\n- 1\n"
