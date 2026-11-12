"""Markdown run file: results of one execution of a test plan, filled in by the agent.

Layout:

    ---
    plan: plans/regress.md   (path relative to the workspace)
    configuration: Any       (optional; default: the plan's first configuration)
    id: ...                  (written by run push)
    ---

    # <run name>

    Run notes: free Markdown before the first case, with `## Summary`, `## Environment`
    and other headings that do not start with a number. Kept locally; not uploaded.

    ## 1042 <case name>

    Result: failed
    Time: 4m 30s

    Free comment.

    - Step 2: failed, the card is not shown ![card](shots/card.png)

    [console log](logs/console.txt)

A case without a `Result:` value was not executed and is not uploaded.
`Time:` is the time spent on the case: `45s`, `4m 30s`, `1h 5m` or `4:30`.
Markdown images and links to local files are attachments: of the step on a `- Step N:` line,
of the result elsewhere. They are removed from the comment sent to the TMS.
HTML comments (`<!-- ... -->`) are ignored, so a template can carry hints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from tms_agent_kit.frontmatter import FormatError, Frontmatter, dump_yaml, split_frontmatter
from tms_agent_kit.model import normalize_text

OUTCOMES = ("Passed", "Failed", "Blocked", "Skipped")

_CASE_RE = re.compile(r"^##\s+#?(\d+)\b(.*)$")
_RESULT_RE = re.compile(r"^result\s*:\s*(.*?)\s*$", re.IGNORECASE)
_TIME_RE = re.compile(r"^time\s*:\s*(.*?)\s*$", re.IGNORECASE)
_UNITS_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s(?:ec)?)?", re.I)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_CLOCK_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")
# Markdown image or link: ![alt](target) or [text](target), target optionally in <...>.
_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_STEP_RE = re.compile(r"^\s*[-*]\s+step\s+(\d+)\s*:\s*([^,;]*?)\s*(?:[,;]\s*(.*?))?\s*$", re.I)


@dataclass(frozen=True)
class FileRef:
    """A local file linked from the run file; the path is relative to the run file."""

    path: str
    line: int


@dataclass(frozen=True)
class StepResult:
    number: int  # 1-based, in the order of the case steps
    outcome: str
    comment: str = ""
    attachments: tuple[FileRef, ...] = ()


@dataclass(frozen=True)
class CaseResult:
    global_id: int
    title: str
    line: int
    outcome: str | None  # None: not executed
    comment: str = ""
    seconds: int | None = None  # time spent on the case; None: not recorded
    steps: tuple[StepResult, ...] = ()
    attachments: tuple[FileRef, ...] = ()


@dataclass(frozen=True)
class RunFile:
    plan: str
    configuration: str | None
    id: str | None
    name: str
    results: tuple[CaseResult, ...]
    notes: str = ""  # Markdown between the run name and the first case

    @property
    def executed(self) -> list[CaseResult]:
        return [r for r in self.results if r.outcome is not None]

    @property
    def seconds(self) -> int | None:
        """Total time of the executed cases that record it; None if none does."""
        times = [r.seconds for r in self.executed if r.seconds is not None]
        return sum(times) if times else None


def outcome(value: str, line: int) -> str | None:
    if not value.strip():
        return None
    canonical = {o.casefold(): o for o in OUTCOMES}.get(value.strip().casefold())
    if canonical is None:
        raise FormatError(f"result must be one of: {', '.join(OUTCOMES)}; got {value!r}", line)
    return canonical


def duration(value: str, line: int) -> int | None:
    """Seconds from `45s`, `4m 30s`, `1h 5m`, `4:30` or `1:04:30`. Empty: None."""
    text = value.strip()
    if not text:
        return None
    units = _UNITS_RE.fullmatch(text)
    if units and any(units.groups()):
        hours, minutes, seconds = (int(g or 0) for g in units.groups())
        return hours * 3600 + minutes * 60 + seconds
    clock = _CLOCK_RE.fullmatch(text)
    if clock and int(clock.group(3)) < 60 and (clock.group(1) is None or int(clock.group(2)) < 60):
        hours, minutes, seconds = (int(g or 0) for g in clock.groups())
        return hours * 3600 + minutes * 60 + seconds
    raise FormatError(f"time must look like 45s, 4m 30s, 1h 5m or 4:30; got {value!r}", line)


def format_duration(seconds: int) -> str:
    """`45s`, `4m 30s`, `1h 5m`: the form `Time:` takes."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    return f"{secs}s"


def parse_run(text: str) -> RunFile:
    lines = _strip_html_comments(text.replace("\r\n", "\n")).split("\n")
    front_lines, body_start = split_frontmatter(lines)
    front = Frontmatter(front_lines)
    front.check_keys({"plan", "configuration", "id"}, "keys: plan, configuration, id")
    plan = front.optional("plan", str)
    if not plan:
        raise FormatError("frontmatter must name the plan file: 'plan: plans/<name>.md'", 1)

    name: str | None = None
    notes: list[str] = []
    results: list[CaseResult] = []
    current: _CaseBuilder | None = None
    fence: str | None = None  # the open code fence: its lines are plain text
    for index in range(body_start, len(lines)):
        number, line = index + 1, lines[index]
        if name is None:
            if not line.strip():
                continue
            if not line.startswith("# ") or not line[2:].strip():
                raise FormatError("a run file must start with '# <run name>'", number)
            name = line[2:].strip()
            continue
        marker = _FENCE_RE.match(line)
        if fence is not None or marker:
            if fence is None and marker:
                fence = marker.group(1)
            elif marker and marker.group(1) == fence:
                fence = None
            if current is None:
                notes.append(line)
            else:
                current.comment.append(line)
            continue
        case = _CASE_RE.match(line)
        if case:
            if current is not None:
                results.append(current.build())
            current = _CaseBuilder(int(case.group(1)), case.group(2).strip(), number)
            continue
        if current is None:
            if line.startswith("# "):
                raise FormatError("run notes use '## ' headings; '# ' is the run name", number)
            notes.append(line)
            continue
        if line.startswith("#"):
            raise FormatError("expected '## <case global id> <name>'", number)
        current.add(line, number)
    if current is not None:
        results.append(current.build())

    if name is None:
        raise FormatError("missing '# <run name>' heading")
    seen: set[int] = set()
    for r in results:
        if r.global_id in seen:
            raise FormatError(f"case #{r.global_id} is listed twice", r.line)
        seen.add(r.global_id)
        numbers = [s.number for s in r.steps]
        if len(numbers) != len(set(numbers)):
            raise FormatError(f"case #{r.global_id}: a step is listed twice", r.line)
        if r.steps and r.outcome is None:
            raise FormatError(f"case #{r.global_id}: step results need a 'Result:'", r.line)
        if r.seconds is not None and r.outcome is None:
            raise FormatError(f"case #{r.global_id}: 'Time:' needs a 'Result:'", r.line)
    return RunFile(
        plan=plan,
        configuration=front.optional("configuration", str),
        id=front.optional("id", str),
        name=name,
        results=tuple(results),
        notes=normalize_text("\n".join(notes)),
    )


@dataclass
class _CaseBuilder:
    global_id: int
    title: str
    line: int
    outcome: str | None = None
    has_result: bool = False
    seconds: int | None = None
    has_time: bool = False
    comment: list[str] = field(default_factory=list)
    steps: list[StepResult] = field(default_factory=list)
    attachments: list[FileRef] = field(default_factory=list)

    def add(self, line: str, number: int) -> None:
        result = _RESULT_RE.match(line)
        if result:
            if self.has_result:
                raise FormatError("duplicate 'Result:' line", number)
            self.has_result = True
            self.outcome = outcome(result.group(1), number)
            return
        time = _TIME_RE.match(line)
        if time:
            if self.has_time:
                raise FormatError("duplicate 'Time:' line", number)
            self.has_time = True
            self.seconds = duration(time.group(1), number)
            return
        text, refs = extract_attachments(line, number)
        step = _STEP_RE.match(text)
        if step:
            step_outcome = outcome(step.group(2), number)
            if step_outcome is not None:
                comment = step.group(3) or ""
                self.steps.append(StepResult(int(step.group(1)), step_outcome, comment, refs))
            elif refs:
                raise FormatError("attachments of a step need its outcome", number)
            return
        self.attachments += refs
        if not refs or text:
            self.comment.append(text)

    def build(self) -> CaseResult:
        return CaseResult(
            global_id=self.global_id,
            title=self.title,
            line=self.line,
            outcome=self.outcome,
            comment=normalize_text("\n".join(self.comment)),
            seconds=self.seconds,
            steps=tuple(sorted(self.steps, key=lambda s: s.number)),
            attachments=tuple(self.attachments),
        )


def extract_attachments(line: str, number: int) -> tuple[str, tuple[FileRef, ...]]:
    """Remove links to local files from a line.

    Images vanish and links keep their text. A line of nothing but such links becomes empty.
    """
    refs: list[FileRef] = []

    def target(match: re.Match[str]) -> str | None:
        value = match.group(3)
        if value.startswith("<"):
            value = value[1:-1].strip()
        if not value or value.startswith("#") or _SCHEME_RE.match(value):
            return None  # a URL or an anchor stays in the comment
        return unquote(value.split("#", 1)[0])

    def replace(match: re.Match[str]) -> str:
        path = target(match)
        if path is None:
            return match.group(0)
        refs.append(FileRef(path, number))
        return "" if match.group(1) else match.group(2)

    text = _LINK_RE.sub(replace, line)
    if not refs:
        return line, ()
    bare = _LINK_RE.sub(lambda m: m.group(0) if target(m) is None else "", line)
    if not bare.strip():
        return "", tuple(refs)
    return re.sub(r"[ \t]{2,}", " ", text).rstrip(), tuple(refs)


def _strip_html_comments(text: str) -> str:
    """Blank out HTML comments, keeping line numbers."""
    return re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)


_TEMPLATE_HINT = """<!--
Run notes go here, before the first case: ## Summary, ## Environment, ## Observations,
## Cleanup. Their headings must not start with a number. Notes are not uploaded.
Fill in one 'Result:' per executed case: passed, failed, blocked or skipped.
Leave it empty for cases you did not execute; they are not uploaded.
'Time:' is the time spent on the case, for example 4m 30s.
Text under 'Result:' is the comment. Optional step results:
- Step 2: failed, what went wrong ![screenshot]({evidence}/<case id>-step-2.png)
Links to local files are uploaded as attachments; paths are relative to this file.
Then run: tms-agent-kit run push <this file>
-->"""


def render_template(
    plan: str,
    configuration: str | None,
    name: str,
    cases: list[tuple[int | None, str]],
    evidence: str = "evidence",
) -> str:
    """A run file to fill in. `evidence` is the suggested folder for screenshots and logs."""
    front: dict[str, object] = {"plan": plan}
    if configuration:
        front["configuration"] = configuration
    hint = _TEMPLATE_HINT.format(evidence=evidence)
    out = ["---", dump_yaml(front).rstrip("\n"), "---", "", f"# {name}", "", hint, ""]
    for global_id, case_name in cases:
        out += [f"## {global_id} {case_name}", "", "Result:", "Time:", ""]
    return "\n".join(out).rstrip("\n") + "\n"


def set_run_id(text: str, run_id: str) -> str:
    """Write the run `id` into the frontmatter, keeping the rest of the file as is."""
    lines = text.replace("\r\n", "\n").split("\n")
    front_lines, body_start = split_frontmatter(lines)
    kept = [line for line in front_lines if not re.match(r"^id\s*:", line)]
    return "\n".join(["---", f"id: {run_id}", *kept, "---", *lines[body_start:]])
