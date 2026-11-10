"""TMS-agnostic test case model. The Markdown format serializes exactly these fields."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CASE_TYPES = ("TestCases", "CheckLists", "SharedSteps")
STATES = ("NotReady", "Ready", "NeedsWork")
PRIORITIES = ("Lowest", "Low", "Medium", "High", "Highest")


@dataclass(frozen=True)
class Step:
    action: str = ""
    expected: str = ""
    test_data: str = ""
    comments: str = ""
    # Reference to a shared step: its global id, or its UUID when the global id is unknown.
    shared_step: str | None = None


@dataclass(frozen=True)
class Case:
    """Editable content of a test case."""

    name: str
    type: str = "TestCases"
    state: str = "NotReady"
    priority: str = "Medium"
    duration_ms: int = 0
    tags: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    preconditions: tuple[Step, ...] = ()
    steps: tuple[Step, ...] = ()
    postconditions: tuple[Step, ...] = ()


@dataclass(frozen=True)
class CaseMeta:
    """Read-only TMS fields. They identify the case and its version; push ignores edits."""

    id: str | None = None
    global_id: int | None = None
    version_id: str | None = None
    version_number: int | None = None
    modified_date: str | None = None
    automated: bool = False
    # Set when the Markdown file cannot represent the TMS data exactly. Push refuses such cases.
    lossy: bool = False


@dataclass(frozen=True)
class CaseFile:
    meta: CaseMeta
    case: Case


def normalize_text(value: str | None) -> str:
    """Canonical form of a text field: LF line endings, no blank edges, no trailing spaces."""
    if not value:
        return ""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
