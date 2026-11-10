import pytest

from tms_agent_kit.casefile import CaseFormatError, is_representable, parse, render
from tms_agent_kit.model import Case, CaseFile, CaseMeta, Step

META = CaseMeta(
    id="fc819fb6-611a-42c5-9ecb-3306194cd448",
    global_id=1040,
    version_id="fc819fb6-611a-42c5-9ecb-3306194cd448",
    version_number=1,
    modified_date="2026-11-09T21:20:48.464Z",
)

FULL = CaseFile(
    meta=META,
    case=Case(
        name="Send a message",
        state="Ready",
        priority="High",
        duration_ms=60000,
        tags=("chat", "smoke"),
        attributes={"Component": "Chat", "Count": 3},
        description="First line.\n\nSecond paragraph with **bold**.",
        preconditions=(Step(action="User is logged in."),),
        steps=(
            Step(action="Open the chat.", expected="- empty input\n- disabled button"),
            Step(shared_step="1003"),
            Step(action="Type `Hello`.", expected="Reply.", test_data="Hello", comments="Flaky"),
        ),
        postconditions=(Step(action="Log out."),),
    ),
)


def test_round_trip() -> None:
    assert parse(render(FULL)) == FULL


def test_render_is_stable() -> None:
    text = render(FULL)
    assert render(parse(text)) == text


def test_render_layout() -> None:
    text = render(FULL)
    assert text.startswith("---\ntms: testit\nid: fc819fb6")
    assert "modified_date: '2026-11-09T21:20:48.464Z'" in text
    assert "tags: [chat, smoke]" in text
    assert "\n# Send a message\n" in text
    assert "### Step 2\n\nShared step: 1003\n" in text
    assert text.endswith("Log out.\n")


def test_minimal_new_case() -> None:
    parsed = parse("---\n---\n\n# New case\n")
    assert parsed.meta == CaseMeta()
    assert parsed.case == Case(name="New case")


def test_new_case_has_no_meta_and_steps_section() -> None:
    text = render(CaseFile(CaseMeta(), Case(name="New")))
    assert "id:" not in text
    assert "automated" not in text
    assert text.endswith("# New\n\n## Steps\n")


def test_fields_in_any_order_and_unnumbered_steps() -> None:
    text = """---
---

# Case

## Steps

### Step

#### Expected

E

#### Action

A
"""
    assert parse(text).case.steps == (Step(action="A", expected="E"),)


def test_headings_inside_code_fences_are_content() -> None:
    case = Case(name="Case", description="```\n## Steps\n### Step 1\n```")
    case_file = CaseFile(CaseMeta(), case)
    assert is_representable(case_file)
    assert parse(render(case_file)).case == case


def test_heading_lines_in_text_are_escaped() -> None:
    text = "# Title\n## Steps\n\\# already escaped\n#hashtag"
    case = Case(name="Case", description=text, steps=(Step(action="### Step 9"),))
    case_file = CaseFile(CaseMeta(), case)
    rendered = render(case_file)
    assert "\n\\## Steps\n" in rendered
    assert "\n\\\\# already escaped\n" in rendered
    assert parse(rendered).case == case


def test_unclosed_fence_is_closed_and_lossy() -> None:
    case_file = CaseFile(CaseMeta(), Case(name="Case", description="```\ncode"))
    assert not is_representable(case_file)
    assert parse(render(case_file)).case.description == "```\ncode\n```"


def test_dates_stay_strings() -> None:
    parsed = parse("---\nmodified_date: 2026-01-01T00:00:00Z\n---\n\n# Case\n")
    assert parsed.meta.modified_date == "2026-01-01T00:00:00Z"


def test_tags_are_sorted_and_unique() -> None:
    parsed = parse("---\ntags: [b, a, b]\n---\n\n# Case\n")
    assert parsed.case.tags == ("a", "b")


def test_crlf_input() -> None:
    assert parse(render(FULL).replace("\n", "\r\n")) == FULL


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("---\n# Case\n", "not closed"),
        ("---\n- a\n---\n# Case\n", "must be a mapping"),
        ("---\nfoo: 1\n---\n# Case\n", "unknown frontmatter keys: foo"),
        ("---\ntms: jira\n---\n# Case\n", "tms must be"),
        ("---\nstate: Done\n---\n# Case\n", "state must be one of"),
        ("---\nduration_ms: -1\n---\n# Case\n", "must not be negative"),
        ("---\nduration_ms: true\n---\n# Case\n", "duration_ms must be of type int"),
        ("---\ntags: a\n---\n# Case\n", "tags must be a list"),
        ("---\nattributes: [1]\n---\n# Case\n", "attributes must be a mapping"),
        ("---\nid: [\n---\n# Case\n", "invalid frontmatter"),
        ("---\n---\n\n## Steps\n", "must come before sections"),
        ("---\n---\n\ntext\n# Case\n", "text before"),
        ("text\n# Case\n", "line 1: text before"),
        ("---\n---\n\n# Case\ntext\n", "outside a section"),
        ("---\n---\n\n# Case\n# Other\n", "only one"),
        ("---\n---\n\n# \n", "case name is empty"),
        ("---\n---\n\n", "missing '# <name>'"),
        ("---\n---\n\n# Case\n## Notes\n", "unknown section"),
        ("---\n---\n\n# Case\n## Steps\n## Description\n", "out of order"),
        ("---\n---\n\n# Case\n## Steps\n## Steps\n", "duplicated"),
        ("---\n---\n\n# Case\n## Description\n### Step 1\n", "only allowed in step sections"),
        ("---\n---\n\n# Case\n## Steps\n#### Action\n", "must be inside"),
        ("---\n---\n\n# Case\n## Steps\n### Step 1\n#### Result\n", "unknown step field"),
        ("---\n---\n\n# Case\n## Steps\n### Step 1\n#### Action\n#### Action\n", "duplicate"),
        ("---\n---\n\n# Case\n## Steps\n### Step 1\nloose text\n", "step content must be"),
        ("---\n---\n\n# Case\n## Steps\n### Step 1\n#### Action\n### Other\n", "unexpected"),
        ("---\n---\n\n# Case\n## Description\n```\ncode\n", "unclosed code fence"),
    ],
)
def test_format_errors(text: str, message: str) -> None:
    with pytest.raises(CaseFormatError, match=message):
        parse(text)


def test_error_has_line_number() -> None:
    with pytest.raises(CaseFormatError) as exc:
        parse("---\n---\n\n# Case\n\n## Notes\n")
    assert exc.value.line == 6
    assert str(exc.value).startswith("line 6:")


def test_frontmatter_is_optional() -> None:
    parsed = parse("# Case\n\n## Steps\n\n### Step\n\n#### Action\n\nDo it.\n")
    assert parsed.meta.id is None
    assert parsed.case == Case(name="Case", steps=(Step(action="Do it."),))


def test_enums_are_case_insensitive() -> None:
    parsed = parse("---\ntype: testcases\nstate: ready\npriority: HIGH\n---\n\n# Case\n")
    assert (parsed.case.type, parsed.case.state, parsed.case.priority) == (
        "TestCases",
        "Ready",
        "High",
    )


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("---\ntags: []\nstate: Done\n---\n# Case\n", 3),
        ("---\ntms: testit\nfoo: 1\n---\n# Case\n", 3),
        ("---\ntms: testit\nduration_ms: x\n---\n# Case\n", 3),
        ("---\ntms: testit\nid: [\n---\n# Case\n", 3),
        ("---\n- a\n---\n# Case\n", 2),
    ],
)
def test_frontmatter_errors_have_line_numbers(text: str, line: int) -> None:
    with pytest.raises(CaseFormatError) as exc:
        parse(text)
    assert exc.value.line == line
