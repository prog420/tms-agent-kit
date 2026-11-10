"""Markdown case file: YAML frontmatter plus a fixed heading structure.

Layout:

    ---
    <frontmatter>     (optional for new cases)
    ---

    # <name>

    ## Description
    ## Preconditions     (steps)
    ## Steps             (steps)
    ## Postconditions    (steps)

A step is `### Step N` followed by `#### Action`, `#### Expected`, `#### Test data`,
`#### Comments` blocks, or by a single `Shared step: <id>` line.
Headings inside fenced code blocks are content, not structure.
In text fields, lines starting with `#` outside code blocks are written with a leading
backslash (`\\## Steps`) and read back without it, so text cannot break the structure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from tms_agent_kit.frontmatter import FormatError, Frontmatter, dump_yaml, split_frontmatter
from tms_agent_kit.model import (
    CASE_TYPES,
    PRIORITIES,
    STATES,
    Case,
    CaseFile,
    CaseMeta,
    Step,
    normalize_text,
)

TMS_NAME = "testit"

_SECTIONS = ("Description", "Preconditions", "Steps", "Postconditions")
_STEP_SECTIONS = {
    "Preconditions": "preconditions",
    "Steps": "steps",
    "Postconditions": "postconditions",
}
_FIELDS = {
    "Action": "action",
    "Expected": "expected",
    "Test data": "test_data",
    "Comments": "comments",
}

_SECTION_RE = re.compile(r"^## (.+?)\s*$")
_STEP_RE = re.compile(r"^### Step(?: \d+)?\s*$")
_FIELD_RE = re.compile(r"^#### (.+?)\s*$")
_SHARED_RE = re.compile(r"^Shared step: (\S+)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HASH_RE = re.compile(r"^\\*#")
_ESCAPED_HASH_RE = re.compile(r"^\\+#")

# Kept as the public name of format errors in case files.
CaseFormatError = FormatError


# Rendering


def render(case_file: CaseFile) -> str:
    meta, case = case_file.meta, case_file.case
    front: dict[str, Any] = {"tms": TMS_NAME}
    if meta.id is not None:
        front |= {
            "id": meta.id,
            "global_id": meta.global_id,
            "version_id": meta.version_id,
            "version_number": meta.version_number,
            "modified_date": meta.modified_date,
        }
    front |= {
        "type": case.type,
        "state": case.state,
        "priority": case.priority,
        "duration_ms": case.duration_ms,
        "tags": list(case.tags),
        "attributes": dict(sorted(case.attributes.items())),
    }
    if meta.id is not None:
        front["automated"] = meta.automated
    if meta.lossy:
        front["lossy"] = True

    out = ["---", dump_yaml(front).rstrip("\n"), "---", "", f"# {case.name}", ""]
    if case.description:
        out += ["## Description", "", _escape(case.description), ""]
    for title, attr in _STEP_SECTIONS.items():
        steps: tuple[Step, ...] = getattr(case, attr)
        # Steps is always present so editors see where to add steps.
        if not steps and title != "Steps":
            continue
        out += [f"## {title}", ""]
        for number, step in enumerate(steps, start=1):
            out += _render_step(number, step)
    return "\n".join(out).rstrip("\n") + "\n"


def _render_step(number: int, step: Step) -> list[str]:
    out = [f"### Step {number}", ""]
    if step.shared_step is not None:
        return [*out, f"Shared step: {step.shared_step}", ""]
    for title, attr in _FIELDS.items():
        value = getattr(step, attr)
        if value:
            out += [f"#### {title}", "", _escape(value), ""]
    return out


def _escape(text: str) -> str:
    """Prefix lines that start with `#` outside code fences with a backslash.

    An unclosed fence is closed, so the rest of the file still parses. That text then
    does not round-trip, and the case is marked lossy.
    """
    out = []
    fence: str | None = None
    for line in text.split("\n"):
        match = _FENCE_RE.match(line)
        if match:
            fence = None if fence else match.group(1)
        elif fence is None and _HASH_RE.match(line):
            line = "\\" + line
        out.append(line)
    if fence:
        out.append(fence)
    return "\n".join(out)


def is_representable(case_file: CaseFile) -> bool:
    """True when rendering and parsing gives back the same case."""
    try:
        parsed = parse(render(case_file))
    except CaseFormatError:
        return False
    return parsed.case == case_file.case


# Parsing


@dataclass
class _StepBuilder:
    fields: dict[str, list[str]] = field(default_factory=dict)
    current: str | None = None
    loose: list[tuple[int, str]] = field(default_factory=list)

    def build(self) -> Step:
        loose = [(n, text) for n, text in self.loose if text.strip()]
        values = {attr: normalize_text("\n".join(lines)) for attr, lines in self.fields.items()}
        if loose:
            shared = _SHARED_RE.match(loose[0][1])
            if shared and len(loose) == 1 and not any(values.values()):
                return Step(shared_step=shared.group(1))
            line, _ = loose[0]
            raise CaseFormatError(
                "step content must be under '#### Action', '#### Expected', "
                "'#### Test data' or '#### Comments', or be a single 'Shared step: <id>' line",
                line,
            )
        return Step(**values)


def parse(text: str) -> CaseFile:
    lines = text.replace("\r\n", "\n").split("\n")
    front, body_start = split_frontmatter(lines)
    meta, props = _parse_frontmatter(front)

    name: str | None = None
    seen: list[str] = []
    description: list[str] = []
    step_lists: dict[str, list[Step]] = {attr: [] for attr in _STEP_SECTIONS.values()}
    section: str | None = None
    step: _StepBuilder | None = None
    in_fence = False

    def finish_step() -> None:
        nonlocal step
        if step is not None and section is not None:
            step_lists[_STEP_SECTIONS[section]].append(step.build())
        step = None

    for index in range(body_start, len(lines)):
        number = index + 1
        line = lines[index]
        if in_fence or _FENCE_RE.match(line):
            if _FENCE_RE.match(line):
                in_fence = not in_fence
            _append_content(line, number, name, section, step, description)
            continue

        if line.startswith("# "):
            if name is not None:
                raise CaseFormatError("only one '# <name>' heading is allowed", number)
            name = line[2:].strip()
            if not name:
                raise CaseFormatError("case name is empty", number)
            continue

        match = _SECTION_RE.match(line)
        if match:
            if name is None:
                raise CaseFormatError("'# <name>' must come before sections", number)
            title = match.group(1)
            if title not in _SECTIONS:
                raise CaseFormatError(
                    f"unknown section '## {title}'; expected one of: {', '.join(_SECTIONS)}",
                    number,
                )
            if title in seen or (seen and _SECTIONS.index(title) < _SECTIONS.index(seen[-1])):
                raise CaseFormatError(
                    f"section '## {title}' is duplicated or out of order; "
                    f"order: {', '.join(_SECTIONS)}",
                    number,
                )
            finish_step()
            seen.append(title)
            section = title
            continue

        if _STEP_RE.match(line):
            if section not in _STEP_SECTIONS:
                raise CaseFormatError("'### Step' is only allowed in step sections", number)
            finish_step()
            step = _StepBuilder()
            continue

        match = _FIELD_RE.match(line)
        if match:
            title = match.group(1)
            if step is None:
                raise CaseFormatError(f"'#### {title}' must be inside a '### Step'", number)
            if title not in _FIELDS:
                raise CaseFormatError(
                    f"unknown step field '#### {title}'; expected one of: {', '.join(_FIELDS)}",
                    number,
                )
            attr = _FIELDS[title]
            if attr in step.fields:
                raise CaseFormatError(f"duplicate '#### {title}' in step", number)
            step.fields[attr] = []
            step.current = attr
            continue

        if line.startswith("### ") or line.startswith("#### "):
            raise CaseFormatError(f"unexpected heading {line.strip()!r}", number)

        if _ESCAPED_HASH_RE.match(line):
            line = line[1:]
        _append_content(line, number, name, section, step, description)

    if in_fence:
        raise CaseFormatError("unclosed code fence")
    finish_step()
    if name is None:
        raise CaseFormatError("missing '# <name>' heading")

    case = Case(
        name=name,
        description=normalize_text("\n".join(description)),
        preconditions=tuple(step_lists["preconditions"]),
        steps=tuple(step_lists["steps"]),
        postconditions=tuple(step_lists["postconditions"]),
        **props,
    )
    return CaseFile(meta=meta, case=case)


def _append_content(
    line: str,
    number: int,
    name: str | None,
    section: str | None,
    step: _StepBuilder | None,
    description: list[str],
) -> None:
    if section == "Description":
        description.append(line)
    elif step is not None:
        if step.current is None:
            step.loose.append((number, line))
        else:
            step.fields[step.current].append(line)
    elif line.strip():
        where = "before '# <name>'" if name is None else "outside a section or step"
        raise CaseFormatError(f"text {where}", number)


_META_KEYS = {
    "id",
    "global_id",
    "version_id",
    "version_number",
    "modified_date",
    "automated",
    "lossy",
}
_PROP_KEYS = {"type", "state", "priority", "duration_ms", "tags", "attributes"}


def _parse_frontmatter(lines: list[str]) -> tuple[CaseMeta, dict[str, Any]]:
    front = Frontmatter(lines)
    data = front.data

    unknown = sorted(set(data) - _META_KEYS - _PROP_KEYS - {"tms"})
    if unknown:
        known = ", ".join(sorted(_PROP_KEYS))
        raise front.error(
            unknown[0], f"unknown frontmatter keys: {', '.join(unknown)}; editable keys: {known}"
        )
    if data.get("tms", TMS_NAME) != TMS_NAME:
        raise front.error("tms", f"tms must be {TMS_NAME!r}")

    meta = CaseMeta(
        id=front.optional("id", str),
        global_id=front.optional("global_id", int),
        version_id=front.optional("version_id", str),
        version_number=front.optional("version_number", int),
        modified_date=front.optional("modified_date", str),
        automated=front.optional("automated", bool) or False,
        lossy=front.optional("lossy", bool) or False,
    )

    props: dict[str, Any] = {}
    for key, allowed in (("type", CASE_TYPES), ("state", STATES), ("priority", PRIORITIES)):
        value = front.choice(key, allowed)
        if value is not None:
            props[key] = value
    duration = front.optional("duration_ms", int)
    if duration is not None:
        if duration < 0:
            raise front.error("duration_ms", "duration_ms must not be negative")
        props["duration_ms"] = duration
    tags = data.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            raise front.error("tags", "tags must be a list of non-empty strings")
        props["tags"] = tuple(sorted({t.strip() for t in tags}))
    attributes = data.get("attributes")
    if attributes is not None:
        if not isinstance(attributes, dict) or not all(isinstance(k, str) for k in attributes):
            raise front.error("attributes", "attributes must be a mapping of names to values")
        props["attributes"] = attributes
    return meta, props
