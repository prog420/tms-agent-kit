"""Markdown bug file: a product defect found while testing.

Layout:

    ---
    severity: major          critical | major | minor | trivial
    status: open             open | fixed | closed; default open
    cases: [24821]           global ids of the cases that show the defect
    found: 2026-11-12        optional
    ticket: PROJ-123         optional: the change under test
    url: https://...         optional: the tracker issue, once filed
    ---

    # <title>

    Markdown body: a summary, then `## Steps`, `## Expected`, `## Actual`, `## Cause`,
    `## Evidence`. Links to local files are relative to the bug file.

Bug files are local: the tool never sends them to the TMS or a tracker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tms_agent_kit.frontmatter import FormatError, Frontmatter, split_frontmatter
from tms_agent_kit.model import normalize_text

SEVERITIES = ("critical", "major", "minor", "trivial")
STATUSES = ("open", "fixed", "closed")
_KEYS = {"severity", "status", "cases", "found", "ticket", "url"}


@dataclass(frozen=True)
class BugFile:
    title: str
    severity: str
    status: str = "open"
    cases: tuple[int, ...] = ()
    found: str = ""
    ticket: str = ""
    url: str = ""
    body: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == "open"


def parse_bug(text: str) -> BugFile:
    lines = text.replace("\r\n", "\n").split("\n")
    front_lines, body_start = split_frontmatter(lines)
    front = Frontmatter(front_lines)
    front.check_keys(_KEYS, f"keys: {', '.join(sorted(_KEYS))}")
    severity = front.choice("severity", SEVERITIES)
    if severity is None:
        raise FormatError(f"frontmatter must set severity: {', '.join(SEVERITIES)}", 1)
    url = _text(front, "url")
    if url and not re.match(r"^https?://", url):
        raise front.error("url", "url must start with http:// or https://")

    title = None
    for index in range(body_start, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        if not line.startswith("# ") or not line[2:].strip():
            raise FormatError("a bug file must start with '# <title>'", index + 1)
        title = line[2:].strip()
        body = "\n".join(lines[index + 1 :])
        break
    if title is None:
        raise FormatError("missing '# <title>' heading")
    return BugFile(
        title=title,
        severity=severity,
        status=front.choice("status", STATUSES) or "open",
        cases=_cases(front),
        found=_text(front, "found"),
        ticket=_text(front, "ticket"),
        url=url,
        body=normalize_text(body),
    )


def _text(front: Frontmatter, key: str) -> str:
    value = front.data.get(key)
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise front.error(key, f"{key} must be a string, got {value!r}")
    return str(value).strip()


def _cases(front: Frontmatter) -> tuple[int, ...]:
    value = front.data.get("cases")
    if value is None:
        return ()
    items = value if isinstance(value, list) else [value]
    ids: list[int] = []
    for item in items:
        text = str(item).strip().removeprefix("#")
        if isinstance(item, bool) or not text.isdigit():
            raise front.error("cases", f"cases must be a list of case global ids, got {item!r}")
        ids.append(int(text))
    return tuple(dict.fromkeys(ids))
