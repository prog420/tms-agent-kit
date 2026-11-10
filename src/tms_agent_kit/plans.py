"""Markdown test plan file: optional frontmatter, `# <name>`, a description and `## Cases`.

Layout:

    ---
    id: ...             (written by push)
    global_id: 1203     (written by push)
    build: 1.42.0
    tags: [smoke]
    configurations: [Any]
    ---

    # <name>

    Free description.

    ## Cases

    - Chat/Limits/                 a section folder: every case in it
    - 1041                        a case global id, `#1041` also works
    - [Login](../cases/x.md)       a link or path to a case file

The first reference in a list item selects cases; the rest of the line is a comment.
Parsing is syntactic; references are resolved against the workspace by `plan_sync`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tms_agent_kit.frontmatter import FormatError, Frontmatter, split_frontmatter
from tms_agent_kit.model import normalize_text

_CASES_HEADINGS = {"cases", "кейсы"}
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*?)\s*$")
_EDITABLE_KEYS = {"build", "tags", "configurations"}
_META_KEYS = {"tms", "id", "global_id"}


@dataclass(frozen=True)
class CaseRef:
    """One list item under `## Cases`, before resolution."""

    line: int
    text: str


@dataclass(frozen=True)
class PlanMeta:
    id: str | None = None
    global_id: int | None = None


@dataclass(frozen=True)
class Plan:
    name: str
    description: str = ""
    build: str = ""
    tags: tuple[str, ...] = ()
    configurations: tuple[str, ...] = ()  # names; empty means the project default
    refs: tuple[CaseRef, ...] = ()


@dataclass(frozen=True)
class PlanFile:
    meta: PlanMeta
    plan: Plan


def parse_plan(text: str) -> PlanFile:
    lines = text.replace("\r\n", "\n").split("\n")
    front_lines, body_start = split_frontmatter(lines)
    front = Frontmatter(front_lines)
    front.check_keys(
        _EDITABLE_KEYS | _META_KEYS, f"editable keys: {', '.join(sorted(_EDITABLE_KEYS))}"
    )
    if front.data.get("tms", "testit") != "testit":
        raise front.error("tms", "tms must be 'testit'")
    build = front.data.get("build")
    if build is not None and not isinstance(build, str | int | float):
        raise front.error("build", "build must be a string")
    meta = PlanMeta(id=front.optional("id", str), global_id=front.optional("global_id", int))

    name: str | None = None
    description: list[str] = []
    refs: list[CaseRef] = []
    in_cases = False
    in_fence = False
    for index in range(body_start, len(lines)):
        number, line = index + 1, lines[index]
        if in_cases:
            if not line.strip():
                continue
            item = _ITEM_RE.match(line)
            if item is None:
                if line.startswith("#"):
                    raise FormatError("'## Cases' must be the last section of a plan", number)
                raise FormatError("expected a list item: '- <case id, file or folder>'", number)
            if not item.group(1):
                raise FormatError("empty list item", number)
            refs.append(CaseRef(number, item.group(1)))
            continue
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if name is None:
            if not line.strip():
                continue
            if not line.startswith("# ") or not line[2:].strip():
                raise FormatError("a plan must start with '# <name>'", number)
            name = line[2:].strip()
            continue
        section = None if in_fence else _SECTION_RE.match(line)
        if section and section.group(1).casefold() in _CASES_HEADINGS:
            in_cases = True
            continue
        description.append(line)

    if name is None:
        raise FormatError("missing '# <name>' heading")
    if not in_cases:
        raise FormatError("missing '## Cases' section")
    if not refs:
        raise FormatError("'## Cases' is empty; list at least one case, file or folder")
    plan = Plan(
        name=name,
        description=normalize_text("\n".join(description)),
        build="" if build is None else str(build).strip(),
        tags=tuple(sorted(set(front.string_list("tags") or ()))),
        configurations=front.string_list("configurations") or (),
        refs=tuple(refs),
    )
    return PlanFile(meta=meta, plan=plan)


def set_plan_meta(text: str, meta: PlanMeta) -> str:
    """Write `id` and `global_id` into the frontmatter, keeping the rest of the file as is."""
    lines = text.replace("\r\n", "\n").split("\n")
    front_lines, body_start = split_frontmatter(lines)
    kept = [line for line in front_lines if not re.match(r"^(id|global_id)\s*:", line)]
    head = [f"id: {meta.id}", f"global_id: {meta.global_id}"]
    body = lines[body_start:]
    if body_start == 0 and body and body[0].strip():
        body = ["", *body]
    return "\n".join(["---", *head, *kept, "---", *body])
