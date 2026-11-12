"""`report`: one static HTML page with the cases, plans and runs of a workspace.

The report is built from local files only: case files, plan files, run files and `.tms/`.
It makes no network calls, so it shows the state of the working tree, including edits
that are not pushed yet.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Literal

from tms_agent_kit.config import Config
from tms_agent_kit.frontmatter import FormatError
from tms_agent_kit.model import Case, Step
from tms_agent_kit.plan_sync import plan_changes, resolve_cases
from tms_agent_kit.plans import parse_plan
from tms_agent_kit.runs import (
    OUTCOMES,
    CaseResult,
    FileRef,
    RunFile,
    format_duration,
    parse_run,
)
from tms_agent_kit.sync import Create, Delete, Update, load_state
from tms_agent_kit.sync import plan as case_changes
from tms_agent_kit.workspace import State, Workspace

NOT_RUN = "Not run"
_COUNTER_RE = re.compile(r"^(.*)-(\d{1,3})$")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

CaseStatus = Literal["pushed", "modified", "new"]


@dataclass
class CaseRow:
    path: Path
    folder: str  # section folder, POSIX, relative to the case root
    case: Case
    case_id: str | None
    global_id: int | None
    automated: bool
    status: CaseStatus
    plans: list[PlanRow] = field(default_factory=list)
    results: list[tuple[RunRow, CaseResult]] = field(default_factory=list)  # executed only

    @property
    def last(self) -> tuple[RunRow, CaseResult] | None:
        return self.results[-1] if self.results else None

    @property
    def last_outcome(self) -> str:
        last = self.last
        return last[1].outcome or NOT_RUN if last else NOT_RUN


@dataclass
class PlanRow:
    path: Path
    name: str = ""
    global_id: int | None = None
    status: str = "pushed"  # pushed | changed | new | error
    error: str = ""
    cases: list[CaseRow] = field(default_factory=list)
    runs: list[RunRow] = field(default_factory=list)


@dataclass
class RunRow:
    path: Path
    run: RunFile | None = None
    plan: PlanRow | None = None
    status: str = "pushed"  # pushed | changed | local | error
    error: str = ""

    @property
    def totals(self) -> Counter[str]:
        if self.run is None:
            return Counter()
        return Counter(r.outcome or NOT_RUN for r in self.run.results)

    @property
    def seconds(self) -> int | None:
        return self.run.seconds if self.run is not None else None


@dataclass
class Problem:
    kind: str
    target: str
    message: str


@dataclass
class Report:
    config: Config
    state: State
    ws: Workspace
    cases: list[CaseRow]
    plans: list[PlanRow]
    runs: list[RunRow]
    problems: list[Problem]
    deleted: list[str]  # cases deleted locally, by label

    @property
    def runnable(self) -> list[CaseRow]:
        return [c for c in self.cases if c.case.type != "SharedSteps"]


def build_report(ws: Workspace, config: Config) -> Report:
    """Collect the workspace data. Makes no network calls."""
    state = load_state(ws)
    scan = ws.scan()
    changes = case_changes(ws, state)
    status: dict[Path, CaseStatus] = {}
    for change in changes.changes:
        if isinstance(change, Update):
            status[change.path] = "modified"
        elif isinstance(change, Create):
            status[change.path] = "new"
    problems = [Problem("invalid", e.target, e.message) for e in changes.errors]

    cases: list[CaseRow] = []
    for local in sorted([*scan.by_id.values(), *scan.new], key=lambda f: f.path):
        meta = local.case_file.meta
        cases.append(
            CaseRow(
                path=local.path,
                folder=ws.section_path_of(local.path),
                case=local.case_file.case,
                case_id=meta.id,
                global_id=meta.global_id,
                automated=meta.automated,
                status=status.get(local.path, "new" if meta.id is None else "pushed"),
            )
        )
    by_id = {c.case_id: c for c in cases if c.case_id}
    by_global_id = {c.global_id: c for c in cases if c.global_id is not None}

    plan_status = {
        c.path: "new" if c.kind == "create" else "changed"
        for c in plan_changes(ws, scan).changes
        if c.path is not None
    }
    plans: list[PlanRow] = []
    for path in ws.plan_files():
        row = PlanRow(path, name=path.stem, status=plan_status.get(path, "pushed"))
        plans.append(row)
        try:
            plan_file = parse_plan(path.read_text(encoding="utf-8"))
        except (FormatError, UnicodeDecodeError) as e:
            row.status, row.error = "error", str(e)
            continue
        row.name, row.global_id = plan_file.plan.name, plan_file.meta.global_id
        planned, errors = resolve_cases(ws, scan, path, plan_file.plan.refs)
        if errors:
            row.status, row.error = "error", "; ".join(errors)
        for planned_case in planned:
            case_row = by_id.get(planned_case.case_id)
            if case_row is not None:
                row.cases.append(case_row)
                case_row.plans.append(row)
    plans_by_path = {p.path.resolve(): p for p in plans}

    runs: list[RunRow] = []
    run_files = sorted(ws.runs_root.glob("*.md"), key=_run_order) if ws.runs_root.is_dir() else []
    for path in run_files:
        run_row = RunRow(path)
        runs.append(run_row)
        try:
            run_file = parse_run(path.read_text(encoding="utf-8"))
        except (FormatError, UnicodeDecodeError) as e:
            run_row.status, run_row.error = "error", str(e)
            continue
        run_row.run = run_file
        run_row.plan = plans_by_path.get((ws.base_dir / run_file.plan).resolve())
        if run_row.plan is not None:
            run_row.plan.runs.append(run_row)
        run_row.status = _run_status(ws, run_file)
        for result in run_file.executed:
            case_row = by_global_id.get(result.global_id)
            if case_row is not None:
                case_row.results.append((run_row, result))

    problems += [Problem("invalid", ws.display(p.path), p.error) for p in plans if p.error]
    problems += [Problem("invalid", ws.display(r.path), r.error) for r in runs if r.error]
    deleted = [c.target for c in changes.changes if isinstance(c, Delete)]
    return Report(config, state, ws, cases, plans, runs, problems, deleted)


def _run_order(path: Path) -> tuple[str, int]:
    """`run new` names files `<date>-<plan>.md`, then `<date>-<plan>-2.md`, `-3`, ..."""
    match = _COUNTER_RE.match(path.stem)
    return (match.group(1), int(match.group(2))) if match else (path.stem, 1)


def _run_status(ws: Workspace, run_file: RunFile) -> str:
    if run_file.id is None:
        return "local"
    snapshot = ws.load_run_snapshot(run_file.id)
    if snapshot is None:
        return "pushed"  # pushed from another clone; the snapshot is not committed
    sent = snapshot.get("results", {})
    for result in run_file.executed:
        before = sent.get(str(result.global_id))
        if before is None or (
            before.get("outcome"),
            before.get("comment"),
            before.get("seconds"),
        ) != (result.outcome, result.comment, result.seconds):
            return "changed"
    return "pushed"


# HTML


def write_report(report: Report, output: Path, focus: RunRow | None = None) -> None:
    html = render_html(report, output.parent, datetime.now(), focus)
    report.ws.write_file(output, html)


def find_run(report: Report, path: Path) -> RunRow | None:
    path = path.resolve()
    return next((r for r in report.runs if r.path.resolve() == path), None)


def render_html(
    report: Report, out_dir: Path, generated: datetime, focus: RunRow | None = None
) -> str:
    """`focus`: a run to open at the top of the page, for a report on one test session."""
    view = _View(report, out_dir.resolve(), focus)
    title = report.state.project_name or report.config.workspace.name
    if focus is not None and focus.run is not None:
        title = focus.run.name
    body = [
        view.header(title, generated),
        view.focused(),
        view.overview(),
        view.attention(),
        view.sections(),
        view.plans(),
        view.runs(),
        view.cases(),
    ]
    return _PAGE.format(title=escape(title), css=_CSS, js=_JS, body="\n".join(body))


class _View:
    def __init__(self, report: Report, out_dir: Path, focus: RunRow | None = None) -> None:
        self.r = report
        self.out_dir = out_dir
        self.focus = focus
        state = report.state
        base = report.config.base_url.rstrip("/")
        self.project_url = (
            f"{base}/projects/{state.project_global_id}" if state.project_global_id else None
        )

    # Links

    def href(self, path: Path) -> str:
        return escape(Path(os.path.relpath(path.resolve(), self.out_dir)).as_posix(), quote=True)

    def case_link(self, row: CaseRow) -> str:
        label = f"#{row.global_id}" if row.global_id is not None else "new"
        if self.project_url and row.global_id is not None:
            url = f"{self.project_url}/tests/{row.global_id}"
            return f'<a class="id" href="{escape(url)}" target="_blank" rel="noopener">{label}</a>'
        return f'<span class="id">{label}</span>'

    def plan_url(self, plan: PlanRow | None) -> str | None:
        if self.project_url and plan is not None and plan.global_id is not None:
            return f"{self.project_url}/test-plans/{plan.global_id}/plan"
        return None

    def run_url(self, run: RunRow) -> str | None:
        plan_url = self.plan_url(run.plan)
        if plan_url and run.run is not None and run.run.id and run.plan is not None:
            return f"{self.project_url}/test-plans/{run.plan.global_id}/test-runs/{run.run.id}"
        return None

    def section_name(self, folder: str) -> str:
        section_id = self.r.state.section_by_path(folder)
        if section_id is not None:
            return self.r.state.section_label(section_id)
        return folder.replace("/", " / ") or "(root)"

    # Blocks

    def header(self, title: str, generated: datetime) -> str:
        r = self.r
        tms = (
            f'<a href="{escape(self.project_url)}" target="_blank" rel="noopener">Test IT</a>'
            if self.project_url
            else escape(r.config.base_url)
        )
        pulled = escape(r.state.last_pull or "never")
        return (
            f"<header><h1>{escape(title)}</h1><p class=muted>{tms} · last pull {pulled} · "
            f"report {generated:%Y-%m-%d %H:%M} · from local files, unpushed edits included"
            "</p>"
            "<nav>"
            + ('<a href="#run">This run</a>' if self.focus is not None else "")
            + '<a href="#attention">Attention</a><a href="#sections">Sections</a>'
            '<a href="#plans">Plans</a><a href="#runs">Runs</a><a href="#cases">Cases</a></nav>'
            "</header>"
        )

    def overview(self) -> str:
        cases = self.r.runnable
        last = Counter(c.last_outcome for c in cases)
        planned = sum(1 for c in cases if c.plans)
        tiles = [
            ("Cases", str(len(cases)), f"{sum(c.case.state == 'Ready' for c in cases)} ready"),
            ("In plans", _percent(planned, len(cases)), f"{planned} of {len(cases)}"),
            ("Executed", _percent(len(cases) - last[NOT_RUN], len(cases)), "latest result"),
            ("Passed", str(last["Passed"]), "latest result", "passed"),
            ("Failed", str(last["Failed"]), "latest result", "failed"),
            ("Blocked", str(last["Blocked"]), "latest result", "blocked"),
            ("Plans", str(len(self.r.plans)), f"{len(self.r.runs)} runs"),
        ]
        times = [run.seconds for run in self.r.runs if run.seconds is not None]
        if times:
            note = f"{len(times)} of {len(self.r.runs)} runs"
            tiles.append(("Time spent", format_duration(sum(times)), note))
        html = []
        for tile in tiles:
            label, value, note = tile[:3]
            tone = f" {tile[3]}" if len(tile) > 3 and value != "0" else ""
            html.append(
                f'<div class="tile{tone}"><div class=label>{label}</div>'
                f"<div class=value>{value}</div><div class=muted>{note}</div></div>"
            )
        return f'<section class="tiles">{"".join(html)}</section>'

    def attention(self) -> str:
        r = self.r
        cases = r.runnable
        groups: list[tuple[str, list[str]]] = [
            (
                "Failed or blocked in the latest run",
                [
                    f"{self.case_ref(c)} {_pill(c.last_outcome)} {self.run_ref(c.last[0])}"
                    for c in cases
                    if c.last and c.last_outcome in ("Failed", "Blocked")
                ],
            ),
            (
                "Invalid files",
                [f"<code>{escape(p.target)}</code>: {escape(p.message)}" for p in r.problems],
            ),
            (
                "Not pushed",
                [f"{self.case_ref(c)} {_pill(c.status)}" for c in r.cases if c.status != "pushed"]
                + [
                    f"{self.plan_ref(p)} {_pill(p.status)}"
                    for p in r.plans
                    if p.status in ("new", "changed")
                ]
                + [
                    f"{self.run_ref(run)} {_pill(run.status)}"
                    for run in r.runs
                    if run.status in ("local", "changed")
                ]
                + [f"{escape(label)} {_pill('deleted')}" for label in r.deleted],
            ),
            ("Needs work", [self.case_ref(c) for c in cases if c.case.state == "NeedsWork"]),
            (
                "Without steps",
                [
                    self.case_ref(c)
                    for c in cases
                    if c.case.type == "TestCases" and not c.case.steps
                ],
            ),
            ("Not in any plan", [self.case_ref(c) for c in cases if not c.plans]),
            ("Planned, never run", [self.case_ref(c) for c in cases if c.plans and not c.results]),
            ("Same name", self.duplicates()),
        ]
        blocks = []
        for title, items in groups:
            if not items:
                continue
            shown = "".join(f"<li>{item}</li>" for item in items)
            blocks.append(
                f"<details{' open' if len(items) <= 10 else ''}><summary>{escape(title)} "
                f"<span class=count>{len(items)}</span></summary><ul>{shown}</ul></details>"
            )
        if not blocks:
            blocks = ["<p class=muted>Nothing to look at.</p>"]
        return f'<section id="attention"><h2>Attention</h2>{"".join(blocks)}</section>'

    def duplicates(self) -> list[str]:
        by_name: dict[str, list[CaseRow]] = {}
        for c in self.r.runnable:
            by_name.setdefault(c.case.name.strip().casefold(), []).append(c)
        return [
            ", ".join(self.case_ref(c) + f" <code>{escape(c.folder)}</code>" for c in rows)
            for rows in by_name.values()
            if len(rows) > 1
        ]

    def sections(self) -> str:
        folders: dict[str, list[CaseRow]] = {}
        for c in self.r.runnable:
            folders.setdefault(c.folder, []).append(c)
        rows = []
        for folder, cases in sorted(folders.items()):
            last = Counter(c.last_outcome for c in cases)
            planned = sum(1 for c in cases if c.plans)
            rows.append(
                f'<tr><td><a href="#s-{_anchor(folder)}">'
                f"{escape(self.section_name(folder))}</a></td>"
                f"<td class=num>{len(cases)}</td><td class=num>{planned}</td>"
                + "".join(f"<td class=num>{last[o] or ''}</td>" for o in (*OUTCOMES, NOT_RUN))
                + f"<td>{_bar(last, len(cases))}</td></tr>"
            )
        head = "".join(f"<th class=num>{o}</th>" for o in (*OUTCOMES, NOT_RUN))
        return (
            '<section id="sections"><h2>Sections</h2><p class=muted>Latest result of each case.'
            "</p><div class=scroll><table><thead><tr><th>Section</th><th class=num>Cases</th>"
            f"<th class=num>In plans</th>{head}<th></th></tr></thead><tbody>{''.join(rows)}"
            "</tbody></table></div></section>"
        )

    def plans(self) -> str:
        if not self.r.plans:
            return '<section id="plans"><h2>Plans</h2><p class=muted>No plan files.</p></section>'
        rows = []
        for p in self.r.plans:
            last_run = p.runs[-1] if p.runs else None
            latest = f"{self.run_ref(last_run)} {_totals(last_run.totals)}" if last_run else "—"
            rows.append(
                f"<tr><td>{self.plan_ref(p)}</td><td>{_pill(p.status)}</td>"
                f"<td class=num>{len(p.cases)}</td><td class=num>{len(p.runs)}</td>"
                f"<td>{latest}</td></tr>"
            )
        return (
            '<section id="plans"><h2>Plans</h2><div class=scroll><table><thead><tr>'
            "<th>Plan</th><th>Status</th><th class=num>Cases</th><th class=num>Runs</th>"
            f"<th>Latest run</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
            "</section>"
        )

    def focused(self) -> str:
        run = self.focus
        if run is None or run.run is None:
            return ""
        executed = run.run.executed
        timed = [r for r in executed if r.seconds is not None]
        notes = [f"{len(executed)} of {len(run.run.results)} cases executed"]
        if timed:
            notes.append(f"time recorded for {len(timed)} of {len(executed)}")
        slowest = sorted(timed, key=lambda r: r.seconds or 0, reverse=True)[:3]
        slow = ", ".join(
            f'<a href="#c-{r.global_id}">#{r.global_id}</a> {_time(r.seconds)}' for r in slowest
        )
        return (
            f'<section id="run"><h2>This run</h2><p class=muted>{" · ".join(notes)}'
            + (f" · slowest: {slow}" if slow else "")
            + f"</p>{self.run_block(run, open_=True)}</section>"
        )

    def runs(self) -> str:
        if not self.r.runs:
            return '<section id="runs"><h2>Runs</h2><p class=muted>No run files.</p></section>'
        blocks = [self.run_block(run) for run in reversed(self.r.runs) if run is not self.focus]
        if not any(blocks):
            blocks = ["<p class=muted>No other runs.</p>"]
        return f'<section id="runs"><h2>Runs</h2>{"".join(blocks)}</section>'

    def run_block(self, run: RunRow, *, open_: bool = False) -> str:
        run_file = run.run
        if run_file is None:
            return ""
        plan = self.plan_ref(run.plan) if run.plan else escape(run_file.plan)
        config = f" · {escape(run_file.configuration)}" if run_file.configuration else ""
        tms = self.run_url(run)
        tms_link = (
            f' · <a href="{escape(tms)}" target="_blank" rel="noopener">Test IT</a>' if tms else ""
        )
        results = "".join(self.result(run, result) for result in run_file.results)
        return (
            f'<details id="r-{_anchor(run.path.stem)}"{" open" if open_ else ""}>'
            f"<summary><b>{escape(run_file.name)}</b> "
            f"<span class=id>{escape(run.path.name)}</span> "
            f"{_pill(run.status)} {_totals(run.totals)} {_time(run.seconds)}"
            f"</summary><p class=muted>Plan {plan}{config} · "
            f'<a href="{self.href(run.path)}">{escape(run.path.name)}</a>{tms_link}</p>'
            f"<div class=results>{results}</div></details>"
        )

    def result(self, run: RunRow, result: CaseResult) -> str:
        outcome = result.outcome or NOT_RUN
        parts = [
            f'<div class="result"><div class=line>{_pill(outcome)} '
            f"<span class=id>#{result.global_id}</span> {escape(result.title)} "
            f"{_time(result.seconds)}</div>"
        ]
        if result.comment:
            parts.append(f"<div class=text>{escape(result.comment)}</div>")
        if result.steps:
            steps = "".join(
                f"<li>Step {s.number}: {_pill(s.outcome)} {escape(s.comment)}"
                f"{self.files(run, s.attachments)}</li>"
                for s in result.steps
            )
            parts.append(f"<ul class=steps>{steps}</ul>")
        parts.append(self.files(run, result.attachments))
        parts.append("</div>")
        return "".join(parts)

    def files(self, run: RunRow, refs: tuple[FileRef, ...]) -> str:
        if not refs:
            return ""
        items = []
        for ref in refs:
            path = run.path.parent / ref.path
            href = self.href(path)
            if path.suffix.lower() in _IMAGE_SUFFIXES:
                items.append(
                    f'<a href="{href}" target="_blank"><img loading="lazy" src="{href}" '
                    f'alt="{escape(ref.path, quote=True)}"></a>'
                )
            else:
                items.append(f'<a class=file href="{href}">{escape(Path(ref.path).name)}</a>')
        return f"<div class=files>{''.join(items)}</div>"

    def cases(self) -> str:
        folders: dict[str, list[CaseRow]] = {}
        for c in self.r.cases:
            folders.setdefault(c.folder, []).append(c)
        groups = []
        for folder, cases in sorted(folders.items()):
            items = "".join(self.case(c) for c in cases)
            groups.append(
                f'<div class=group id="s-{_anchor(folder)}"><h3>'
                f"{escape(self.section_name(folder))} <span class=count>{len(cases)}</span></h3>"
                f"{items}</div>"
            )
        filters = "".join(
            f'<button type=button data-filter="{key}"{" class=on" if key == "" else ""}>'
            f"{label}</button>"
            for key, label in (
                ("", "All"),
                ("failed", "Failed"),
                ("blocked", "Blocked"),
                ("notrun", "Not run"),
                ("noplan", "Not in a plan"),
                ("unpushed", "Not pushed"),
            )
        )
        return (
            '<section id="cases"><h2>Cases</h2><div class=toolbar>'
            '<input type=search id=q placeholder="Search name, id, tag, text">'
            f"<div class=filters>{filters}</div></div>{''.join(groups)}"
            '<p id=empty class="muted" hidden>No matching cases.</p></section>'
        )

    def case(self, c: CaseRow) -> str:
        case = c.case
        flags = []
        if case.type != "SharedSteps":
            flags.append(c.last_outcome.lower().replace(" ", ""))
            if not c.plans:
                flags.append("noplan")
        if c.status != "pushed":
            flags.append("unpushed")
        search = " ".join(
            [
                case.name,
                str(c.global_id or ""),
                *case.tags,
                case.description,
                *(s.action + " " + s.expected for s in case.steps),
            ]
        ).casefold()
        meta = [
            _pill(case.state),
            f"<span class=tag>{escape(case.priority)}</span>",
            *(f"<span class=tag>#{escape(t)}</span>" for t in case.tags),
        ]
        if case.type != "TestCases":
            meta.insert(0, f"<span class=tag>{escape(case.type)}</span>")
        if c.automated:
            meta.append("<span class=tag>autotest</span>")
        if c.status != "pushed":
            meta.append(_pill(c.status))
        last = c.last
        result = (
            (f"{_pill(last[1].outcome or NOT_RUN)}" if last else _pill(NOT_RUN))
            if case.type != "SharedSteps"
            else ""
        )
        history = "".join(
            f"<li>{_pill(res.outcome or NOT_RUN)} {self.run_ref(run)} {_time(res.seconds)}"
            + (f" — {escape(_first_line(res.comment))}" if res.comment else "")
            + "</li>"
            for run, res in reversed(c.results)
        )
        plans = ", ".join(self.plan_ref(p) for p in c.plans) or "<span class=muted>none</span>"
        body = [
            f'<p class=muted>Plans: {plans} · <a href="{self.href(c.path)}">'
            f"{escape(c.path.name)}</a></p>"
        ]
        if case.description:
            body.append(f"<div class=text>{escape(case.description)}</div>")
        body += [
            _steps("Preconditions", case.preconditions),
            _steps("Steps", case.steps),
            _steps("Postconditions", case.postconditions),
        ]
        if history:
            body.append(f"<h4>Results</h4><ul class=history>{history}</ul>")
        return (
            f'<details class=case id="{_case_anchor(c)}" data-flags="{" ".join(flags)}" '
            f'data-search="{escape(search, quote=True)}"><summary>'
            f"{self.case_link(c)} <span class=name>{escape(case.name)}</span>"
            f"<span class=meta>{''.join(meta)} {result}</span></summary>"
            f"<div class=body>{''.join(body)}</div></details>"
        )

    # References

    def case_ref(self, c: CaseRow) -> str:
        return f'{self.case_link(c)} <a href="#{_case_anchor(c)}">{escape(c.case.name)}</a>'

    def plan_ref(self, plan: PlanRow | None) -> str:
        if plan is None:
            return ""
        url = self.plan_url(plan)
        name = escape(plan.name)
        number = f" #{plan.global_id}" if plan.global_id is not None else ""
        if url:
            return f'<a href="{escape(url)}" target="_blank" rel="noopener">{name}</a>{number}'
        return f'<a href="{self.href(plan.path)}">{name}</a>{number}'

    def run_ref(self, run: RunRow) -> str:
        name = run.run.name if run.run else run.path.name
        return f'<a href="#r-{_anchor(run.path.stem)}">{escape(name)}</a>'


def _steps(title: str, steps: tuple[Step, ...]) -> str:
    if not steps:
        return ""
    rows = []
    for number, step in enumerate(steps, 1):
        if step.shared_step:
            action = f"<i>Shared step {escape(step.shared_step)}</i>"
        else:
            action = escape(step.action)
        extra = "".join(
            f"<div class=muted>{label}: {escape(value)}</div>"
            for label, value in (("Data", step.test_data), ("Comments", step.comments))
            if value
        )
        rows.append(
            f"<tr><td class=num>{number}</td><td class=text>{action}{extra}</td>"
            f"<td class=text>{escape(step.expected)}</td></tr>"
        )
    return (
        f"<h4>{title}</h4><table class=steps><thead><tr><th class=num>#</th><th>Action</th>"
        f"<th>Expected</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _pill(value: str) -> str:
    return f'<span class="pill {value.lower().replace(" ", "")}">{escape(value)}</span>'


def _time(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return f'<span class=time title="time spent">{format_duration(seconds)}</span>'


def _totals(totals: Counter[str]) -> str:
    return " ".join(
        f'<span class="pill {o.lower().replace(" ", "")}">{totals[o]} {o.lower()}</span>'
        for o in (*OUTCOMES, NOT_RUN)
        if totals[o]
    )


def _bar(counts: Counter[str], total: int) -> str:
    if not total:
        return ""
    parts = "".join(
        f'<i class="{o.lower().replace(" ", "")}" style="width:{counts[o] * 100 / total:.2f}%" '
        f'title="{counts[o]} {o.lower()}"></i>'
        for o in (*OUTCOMES, NOT_RUN)
        if counts[o]
    )
    return f"<div class=bar>{parts}</div>"


def _percent(part: int, total: int) -> str:
    return f"{round(part * 100 / total)}%" if total else "—"


def _anchor(text: str) -> str:
    return escape("".join(ch if ch.isalnum() else "-" for ch in text) or "root", quote=True)


def _case_anchor(c: CaseRow) -> str:
    return f"c-{c.global_id}" if c.global_id is not None else f"c-{_anchor(c.path.stem)}"


def _first_line(text: str) -> str:
    line = text.strip().split("\n", 1)[0]
    return line if len(line) <= 120 else line[:117] + "..."


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="tms-agent-kit">
<title>{title} · tests</title>
<style>{css}</style>
</head>
<body>
<main>
{body}
</main>
<script>{js}</script>
</body>
</html>
"""

_CSS = """
:root {
  --bg: #fbfbfa; --surface: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --line: #e4e3df;
  --accent: #2f5fd0; --pass: #1f7a4a; --pass-bg: #e3f3e9; --fail: #b3261e; --fail-bg: #fbe5e3;
  --block: #8a5a00; --block-bg: #fdf0d5; --skip: #55555a; --skip-bg: #ececef;
  --none: #7a7a74; --none-bg: #f1f0ec; --info: #2f5fd0; --info-bg: #e5ecfb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --surface: #1f1f1d; --text: #ecebe6; --muted: #9b9a93; --line: #34332f;
    --accent: #8fb0ff; --pass: #7fd3a2; --pass-bg: #183a27; --fail: #ff9b92; --fail-bg: #45201d;
    --block: #f2c46b; --block-bg: #3d2f12; --skip: #c4c4c9; --skip-bg: #2e2e33;
    --none: #a3a29b; --none-bg: #2a2a27; --info: #8fb0ff; --info-bg: #1e2a47;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 36px 0 12px; }
h3 { font-size: 15px; margin: 20px 0 8px; }
h4 { font-size: 13px; margin: 14px 0 6px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .04em; }
code { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; }
nav { display: flex; gap: 16px; margin-top: 12px; flex-wrap: wrap; }
.muted { color: var(--muted); }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px;
  margin-top: 20px; }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 12px; }
.tile .label { color: var(--muted); font-size: 12px; }
.tile .value { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile.passed .value { color: var(--pass); } .tile.failed .value { color: var(--fail); }
.tile.blocked .value { color: var(--block); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; background: var(--surface); }
th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left;
  vertical-align: top; }
th { font-weight: 600; font-size: 12px; color: var(--muted); }
.num { text-align: right; font-variant-numeric: tabular-nums; width: 1%; white-space: nowrap; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
details { background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
  margin: 6px 0; }
summary { cursor: pointer; padding: 8px 12px; }
details > :not(summary) { margin-left: 12px; margin-right: 12px; }
details[open] > summary { border-bottom: 1px solid var(--line); }
details ul { padding-left: 20px; }
.count { color: var(--muted); font-weight: 400; font-size: 12px; }
.pill { display: inline-block; border-radius: 10px; padding: 0 8px; font-size: 12px;
  background: var(--none-bg); color: var(--none); white-space: nowrap; }
.pill.passed, .pill.ready, .pill.pushed { background: var(--pass-bg); color: var(--pass); }
.pill.failed, .pill.error, .pill.deleted { background: var(--fail-bg); color: var(--fail); }
.pill.blocked, .pill.needswork, .pill.modified, .pill.changed { background: var(--block-bg);
  color: var(--block); }
.pill.skipped { background: var(--skip-bg); color: var(--skip); }
.pill.new, .pill.local { background: var(--info-bg); color: var(--info); }
.tag { font-size: 12px; color: var(--muted); margin-right: 6px; }
.time { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums;
  white-space: nowrap; }
.id { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted);
  margin-right: 6px; }
a.id { color: var(--accent); }
.case summary { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; }
.case .name { flex: 1 1 320px; }
.case .meta { display: flex; gap: 4px; align-items: baseline; flex-wrap: wrap; }
.case .body { padding: 4px 0 12px; }
table.steps td.text { width: 50%; }
.bar { display: flex; height: 8px; min-width: 120px; border-radius: 4px; overflow: hidden;
  background: var(--none-bg); }
.bar i { display: block; height: 100%; }
.bar .passed { background: var(--pass); } .bar .failed { background: var(--fail); }
.bar .blocked { background: var(--block); } .bar .skipped { background: var(--skip); }
.bar .notrun { background: var(--none-bg); }
.results { padding: 4px 0 12px; }
.result { padding: 8px 0; border-bottom: 1px solid var(--line); }
.result:last-child { border-bottom: 0; }
.result .text { margin: 4px 0; }
.files { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.files img { max-height: 140px; max-width: 240px; border: 1px solid var(--line);
  border-radius: 4px; display: block; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
#q { flex: 1 1 260px; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--surface); color: var(--text); font: inherit; }
.filters { display: flex; flex-wrap: wrap; gap: 4px; }
.filters button { border: 1px solid var(--line); background: var(--surface); color: var(--text);
  border-radius: 14px; padding: 3px 10px; font: inherit; font-size: 12px; cursor: pointer; }
.filters button.on { background: var(--text); color: var(--bg); border-color: var(--text); }
@media print { details { break-inside: avoid; } .toolbar, nav { display: none; } }
"""

_JS = """
(() => {
  const q = document.getElementById('q');
  const buttons = document.querySelectorAll('.filters button');
  const cases = document.querySelectorAll('details.case');
  let filter = '';
  function apply() {
    const words = q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
    let shown = 0;
    cases.forEach(c => {
      const ok = (!filter || c.dataset.flags.split(' ').includes(filter)) &&
        words.every(w => c.dataset.search.includes(w));
      c.hidden = !ok;
      if (ok) shown++;
    });
    document.querySelectorAll('#cases .group').forEach(g => {
      g.hidden = !g.querySelector('details.case:not([hidden])');
    });
    document.getElementById('empty').hidden = shown > 0;
  }
  q.addEventListener('input', apply);
  buttons.forEach(b => b.addEventListener('click', () => {
    filter = b.dataset.filter;
    buttons.forEach(x => x.classList.toggle('on', x === b));
    apply();
  }));
  // Opening a link to a section or run expands it.
  function reveal() {
    const el = location.hash && document.getElementById(location.hash.slice(1));
    if (el && el.tagName === 'DETAILS') el.open = true;
  }
  window.addEventListener('hashchange', reveal);
  reveal();
})();
"""
