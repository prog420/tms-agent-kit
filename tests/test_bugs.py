import pytest

from tms_agent_kit.bugs import BugFile, parse_bug
from tms_agent_kit.frontmatter import FormatError


def test_parse_bug() -> None:
    text = """---
severity: Major
cases: [24821, "#24822", 24821]
found: 2026-11-12
ticket: HH-1
---

# Model with minor version 0 goes to OpenRouter

Summary.

## Steps

1. Send it.
"""
    assert parse_bug(text) == BugFile(
        title="Model with minor version 0 goes to OpenRouter",
        severity="major",
        status="open",
        cases=(24821, 24822),
        found="2026-11-12",
        ticket="HH-1",
        body="Summary.\n\n## Steps\n\n1. Send it.",
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# T\n", "must set severity"),
        ("---\nseverity: huge\n---\n# T\n", "line 2: severity must be one of"),
        ("---\nseverity: minor\nstatus: new\n---\n# T\n", "line 3: status must be one of"),
        ("---\nseverity: minor\ncases: [abc]\n---\n# T\n", "line 3: cases must be a list"),
        ("---\nseverity: minor\nurl: ftp://x\n---\n# T\n", "url must start with http"),
        ("---\nseverity: minor\nowner: me\n---\n# T\n", "unknown frontmatter keys: owner"),
        ("---\nseverity: minor\n---\ntext\n", "line 4: a bug file must start with '# <title>'"),
        ("---\nseverity: minor\n---\n", "missing '# <title>'"),
    ],
)
def test_bug_format_errors(text: str, message: str) -> None:
    with pytest.raises(FormatError, match=message):
        parse_bug(text)
