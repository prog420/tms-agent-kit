"""Test runs: create a run file from a plan, upload its results to the TMS.

The public Test IT API records results only for automated test points, so each executed case
gets its own autotest (`tms-agent-kit-<globalId>`) linked to it. See docs/test-plans.md.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from tms_agent_kit.frontmatter import FormatError
from tms_agent_kit.plan_sync import PlannedCase, configuration_ids, plan_changes, resolve_cases
from tms_agent_kit.plans import parse_plan
from tms_agent_kit.runs import (
    CaseResult,
    FileRef,
    RunFile,
    format_duration,
    parse_run,
    render_template,
    set_run_id,
)
from tms_agent_kit.sync import Event, SyncError, _write_case, context
from tms_agent_kit.testit import TestItClient, TestItError
from tms_agent_kit.workspace import State, Workspace

Json = dict[str, Any]

AUTOTEST_PREFIX = "tms-agent-kit-"
AUTOTEST_NAMESPACE = "tms-agent-kit"

AutotestAction = Literal["ready", "create", "link", "update"]


def autotest_external_id(global_id: int | None) -> str:
    return f"{AUTOTEST_PREFIX}{global_id}"


# run new


def new_run(ws: Workspace, plan_path: Path, configuration: str | None) -> Path:
    """Write a run file template for the plan. Makes no network calls."""
    plan_path = plan_path.resolve()
    if not plan_path.is_file():
        raise SyncError(f"{plan_path}: plan file not found")
    shown = ws.display(plan_path)
    try:
        plan = parse_plan(plan_path.read_text(encoding="utf-8")).plan
    except FormatError as e:
        raise SyncError(f"{shown}: {e}") from e
    cases, problems = resolve_cases(ws, ws.scan(), plan_path, plan.refs)
    if problems:
        raise SyncError(f"{shown}: {'; '.join(problems)}")
    today = date.today().isoformat()
    path = ws.runs_root / f"{today}-{plan_path.stem}.md"
    counter = 2
    while path.exists():
        path = ws.runs_root / f"{today}-{plan_path.stem}-{counter}.md"
        counter += 1
    text = render_template(
        plan=shown,
        configuration=configuration,
        name=f"{plan.name} {today}",
        cases=[(c.global_id, c.name) for c in cases],
        evidence=path.stem,
    )
    ws.write_file(path, text)
    return path


# run push: local check


@dataclass(frozen=True)
class Attachment:
    path: Path
    shown: str  # path relative to the workspace; identifies the file in run snapshots

    @property
    def size(self) -> int:
        return self.path.stat().st_size


@dataclass
class RunCase:
    result: CaseResult
    case: PlannedCase
    step_count: int
    attachments: list[Attachment] = field(default_factory=list)
    step_attachments: dict[int, list[Attachment]] = field(default_factory=dict)
    # Result attachments to upload: all of them for a new run, the added ones on update.
    new_attachments: list[Attachment] = field(default_factory=list)
    result_changed: bool = True  # outcome, comment or time differ from the pushed result
    # What the autotest needs before results can be sent; set by check_run_remote.
    autotest: AutotestAction = "ready"
    autotest_model: Json | None = None
    # Set when the run was pushed before: the result to update and what was sent.
    result_id: str | None = None
    pushed_outcome: str | None = None

    @property
    def label(self) -> str:
        return f"#{self.case.global_id} {self.case.name}"

    @property
    def external_id(self) -> str:
        return autotest_external_id(self.case.global_id)


@dataclass
class RunPlan:
    path: Path
    target: str
    run_file: RunFile
    plan_id: str
    plan_global_id: int | None
    plan_configurations: tuple[str, ...]
    cases: list[RunCase] = field(default_factory=list)
    not_executed: int = 0
    unchanged: int = 0
    errors: list[Event] = field(default_factory=list)
    conflicts: list[Event] = field(default_factory=list)
    configuration_id: str | None = None

    @property
    def mode(self) -> Literal["create", "update"]:
        return "update" if self.run_file.id else "create"

    @property
    def blocked(self) -> bool:
        return bool(self.errors or self.conflicts)

    @property
    def configuration(self) -> str | None:
        return self.run_file.configuration or next(iter(self.plan_configurations), None)

    def token(self) -> str:
        files = {
            a.shown: hashlib.sha256(a.path.read_bytes()).hexdigest()
            for c in self.cases
            for a in _uploads(c)
        }
        data = {
            "content": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            "plan": self.plan_id,
            "cases": [(c.case.case_id, c.autotest) for c in self.cases],
            "files": files,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]

    def error(self, where: str, message: str) -> None:
        self.errors.append(Event("error", where, message))

    def conflict(self, where: str, message: str) -> None:
        self.conflicts.append(Event("conflict", where, message))


def plan_run(ws: Workspace, run_path: Path) -> RunPlan:
    """Check a run file against the local plan and cases. Makes no network calls."""
    run_path = run_path.resolve()
    target = ws.display(run_path)
    try:
        run_file = parse_run(run_path.read_text(encoding="utf-8"))
    except (FormatError, UnicodeDecodeError, FileNotFoundError) as e:
        raise SyncError(f"{target}: {e}") from e
    plan_path = (ws.base_dir / run_file.plan).resolve()
    if not plan_path.is_file():
        raise SyncError(f"{target}: plan file {run_file.plan!r} not found")
    shown = ws.display(plan_path)
    try:
        plan_file = parse_plan(plan_path.read_text(encoding="utf-8"))
    except FormatError as e:
        raise SyncError(f"{shown}: {e}") from e
    if plan_file.meta.id is None:
        raise SyncError(f"{shown} is not in the TMS yet; run `plan push` first")
    scan = ws.scan()
    if any(c.path == plan_path for c in plan_changes(ws, scan).changes):
        raise SyncError(f"{shown} has local changes; run `plan push` first")
    cases, problems = resolve_cases(ws, scan, plan_path, plan_file.plan.refs)
    if problems:
        raise SyncError(f"{shown}: {'; '.join(problems)}")
    by_global_id = {c.global_id: c for c in cases}

    result = RunPlan(
        path=run_path,
        target=target,
        run_file=run_file,
        plan_id=plan_file.meta.id,
        plan_global_id=plan_file.meta.global_id,
        plan_configurations=plan_file.plan.configurations,
    )
    snapshot = ws.load_run_snapshot(run_file.id) if run_file.id else None
    if run_file.id and snapshot is None:
        raise SyncError(f"{target}: run {run_file.id} was not pushed from this workspace")
    if snapshot is not None and snapshot.get("plan_id") != result.plan_id:
        raise SyncError(f"{target}: the run was pushed for another plan; start a new run")

    for case_result in run_file.results:
        where = f"{target}:{case_result.line}"
        planned = by_global_id.get(case_result.global_id)
        if planned is None:
            result.error(where, f"case #{case_result.global_id} is not in the plan")
            continue
        if case_result.outcome is None:
            result.not_executed += 1
            continue
        local = scan.by_id.get(planned.case_id)
        step_count = len(local.case_file.case.steps) if local else 0
        too_far = [s.number for s in case_result.steps if s.number > step_count]
        if too_far:
            result.error(where, f"the case has {step_count} steps, not {too_far[0]}")
            continue
        run_case = RunCase(case_result, planned, step_count)
        file_errors = _resolve_attachments(ws, run_path, run_case)
        if file_errors:
            result.errors += file_errors
            continue
        run_case.new_attachments = list(run_case.attachments)
        if snapshot is not None:
            pushed = snapshot["results"].get(str(planned.global_id))
            if pushed is None:
                result.error(where, "the case is not in the pushed run; start a new run")
                continue
            if pushed["steps"] != _steps_json(run_case):
                result.error(where, "step results cannot change after push; edit the comment")
                continue
            pushed_files = pushed.get("attachments", [])
            local_files = [a.shown for a in run_case.attachments]
            removed = [f for f in pushed_files if f not in local_files]
            if removed:
                result.error(where, f"attachment {removed[0]} was pushed; it cannot be removed")
                continue
            run_case.new_attachments = [
                a for a in run_case.attachments if a.shown not in pushed_files
            ]
            sent = (pushed["outcome"], pushed["comment"], pushed.get("seconds"))
            current = (case_result.outcome, case_result.comment, case_result.seconds)
            run_case.result_changed = sent != current
            if not (run_case.result_changed or run_case.new_attachments):
                result.unchanged += 1
                continue
            run_case.result_id = pushed["result_id"]
            run_case.pushed_outcome = pushed["outcome"]
        result.cases.append(run_case)
    if snapshot is not None:
        executed = {str(r.global_id) for r in run_file.executed}
        for global_id in sorted(set(snapshot["results"]) - executed):
            result.error(target, f"case #{global_id} was pushed; its result cannot be removed")
    return result


def _resolve_attachments(ws: Workspace, run_path: Path, run_case: RunCase) -> list[Event]:
    errors: list[Event] = []

    def resolve(refs: tuple[FileRef, ...]) -> list[Attachment]:
        found: dict[str, Attachment] = {}
        for ref in refs:
            attachment, problem = _attachment(ws, run_path, ref)
            if problem:
                errors.append(Event("error", f"{ws.display(run_path)}:{ref.line}", problem))
            elif attachment is not None:
                found.setdefault(attachment.shown, attachment)
        return list(found.values())

    run_case.attachments = resolve(run_case.result.attachments)
    for step in run_case.result.steps:
        if step.attachments:
            run_case.step_attachments[step.number] = resolve(step.attachments)
    return errors


def _attachment(ws: Workspace, run_path: Path, ref: FileRef) -> tuple[Attachment | None, str]:
    """A file under the workspace, outside hidden folders: never `.env`, `.git` or `.tms`."""
    base = ws.base_dir.resolve()
    path = (run_path.parent / ref.path).resolve()
    if not path.is_relative_to(base):
        return None, f"attachment {ref.path!r} is outside the workspace"
    relative = path.relative_to(base)
    if any(part.startswith(".") for part in relative.parts):
        return None, f"attachment {ref.path!r} is a hidden file or in a hidden folder"
    if not path.is_file():
        return None, f"attachment {ref.path!r} not found"
    return Attachment(path, relative.as_posix()), ""


def _uploads(run_case: RunCase) -> list[Attachment]:
    """Files the push uploads for this case."""
    step_files = run_case.step_attachments.values() if run_case.result_id is None else []
    files = run_case.new_attachments + [a for files in step_files for a in files]
    return list({a.shown: a for a in files}.values())  # one upload per file


def _steps_json(run_case: RunCase) -> list[list[Any]]:
    steps: list[list[Any]] = []
    for s in run_case.result.steps:
        entry: list[Any] = [s.number, s.outcome, s.comment]
        files = run_case.step_attachments.get(s.number)
        if files:
            entry.append([a.shown for a in files])
        steps.append(entry)
    return steps


# run push: remote check


def check_run_remote(run_plan: RunPlan, client: TestItClient, state: State, ws: Workspace) -> None:
    """Resolve the configuration; check test points, autotests, case versions and results."""
    names = (run_plan.configuration,) if run_plan.configuration else ()
    ids, problem = configuration_ids(names, client.list_configurations(state.project_id))
    if problem:
        run_plan.error(run_plan.target, problem)
        return
    run_plan.configuration_id = ids[0]
    if run_plan.mode == "update":
        for run_case in run_plan.cases:
            _check_result(run_plan, run_case, client)
        return

    points = {
        (p["workItemId"], p["configurationId"])
        for p in client.search_test_points({"testPlanIds": [run_plan.plan_id]})
        if not p.get("isDeleted")
    }
    for run_case in run_plan.cases:
        if (run_case.case.case_id, run_plan.configuration_id) not in points:
            run_plan.error(run_case.label, "not in the TMS plan for this configuration")
            continue
        _check_case(run_plan, run_case, client, state, ws)


def _check_case(
    run_plan: RunPlan, run_case: RunCase, client: TestItClient, state: State, ws: Workspace
) -> None:
    remote = client.get_work_item(run_case.case.case_id)
    base = ws.load_base(run_case.case.case_id) or {}
    if remote.get("versionId") != base.get("versionId"):
        run_plan.conflict(run_case.label, "changed in TMS since the last pull; run pull")
        return
    linked = remote.get("autoTests") or []
    others = [a for a in linked if a.get("externalId") != run_case.external_id]
    if others:
        names = ", ".join(str(a.get("externalId")) for a in others)
        run_plan.error(run_case.label, f"linked to other autotests: {names}")
        return
    if linked:
        model = linked[0]
        run_case.autotest_model = model
        if len(model.get("steps") or []) != run_case.step_count:
            run_case.autotest = "update"
        return
    existing = client.search_autotests(state.project_id, [run_case.external_id])
    if existing:
        run_case.autotest, run_case.autotest_model = "link", existing[0]
    else:
        run_case.autotest = "create"


def _check_result(run_plan: RunPlan, run_case: RunCase, client: TestItClient) -> None:
    if run_case.result_id is None:
        return
    try:
        remote = client.get_test_result(run_case.result_id)
    except TestItError as e:
        if e.status_code == 404:
            run_plan.conflict(run_case.label, "the result is not in TMS anymore")
            return
        raise
    if remote.get("outcome") != run_case.pushed_outcome:
        run_plan.conflict(run_case.label, f"result changed in TMS to {remote.get('outcome')}")


# run push: apply


def push_run(ws: Workspace, state: State, client: TestItClient, run_plan: RunPlan) -> list[Event]:
    if run_plan.blocked:
        raise SyncError("the run has errors or conflicts")
    if run_plan.mode == "update":
        return _update_results(ws, client, run_plan)
    configuration_id = run_plan.configuration_id
    if configuration_id is None:
        raise SyncError("the configuration is not resolved; call check_run_remote first")
    if not run_plan.cases:
        raise SyncError(f"{run_plan.target}: no executed cases; fill in 'Result:' lines")

    events: list[Event] = []
    ctx = context(state)
    for run_case in run_plan.cases:
        if run_case.autotest == "ready":
            continue
        _prepare_autotest(client, state, run_case)
        events.append(Event("linked", run_case.label, run_case.external_id))
        # Linking bumps the case version: refresh the case file and its base.
        fresh = client.get_work_item(run_case.case.case_id)
        events += _write_case(ws, state, ctx, fresh, ws.scan().by_id.get(run_case.case.case_id))

    # Upload files before the run exists: a failed upload leaves no empty run behind.
    uploaded: dict[str, str] = {}
    for run_case in run_plan.cases:
        for attachment in _uploads(run_case):
            if attachment.shown not in uploaded:
                uploaded[attachment.shown] = _upload(client, attachment)
    run = client.create_test_run(
        {
            "projectId": state.project_id,
            "testPlanId": run_plan.plan_id,
            "name": run_plan.run_file.name,
            "testPointSelectors": [
                {
                    "configurationId": configuration_id,
                    "workItemIds": [c.case.case_id for c in run_plan.cases],
                }
            ],
        }
    )
    run_id = str(run["id"])
    # Keep the run id even if sending results fails: the file then points at the run.
    ws.write_file(run_plan.path, set_run_id(run_plan.path.read_text(encoding="utf-8"), run_id))
    bodies = [_result_body(c, configuration_id, uploaded) for c in run_plan.cases]
    result_ids = client.send_test_results(run_id, bodies)
    ws.save_run_snapshot(
        run_id,
        {
            "plan_id": run_plan.plan_id,
            "configuration_id": configuration_id,
            "results": {
                str(c.case.global_id): {
                    "outcome": c.result.outcome,
                    "comment": c.result.comment,
                    "seconds": c.result.seconds,
                    "steps": _steps_json(c),
                    "attachments": [a.shown for a in c.attachments],
                    "result_id": result_id,
                }
                for c, result_id in zip(run_plan.cases, result_ids, strict=True)
            },
        },
    )
    where = f"run in plan #{run_plan.plan_global_id}: {outcome_summary(run_plan)}"
    events.append(Event("created", run_plan.target, where))
    return events


def _prepare_autotest(client: TestItClient, state: State, run_case: RunCase) -> None:
    steps = [{"title": f"Step {n}"} for n in range(1, run_case.step_count + 1)]
    if run_case.autotest == "create":
        client.create_autotest(
            {
                "projectId": state.project_id,
                "externalId": run_case.external_id,
                "name": run_case.case.name,
                "namespace": AUTOTEST_NAMESPACE,
                "steps": steps,
                "workItemIdsForLinkWithAutoTest": [run_case.case.case_id],
            }
        )
        return
    model = run_case.autotest_model or {}
    if run_case.autotest == "link":
        client.link_autotest(str(model["id"]), run_case.case.case_id)
    if len(model.get("steps") or []) != run_case.step_count:
        client.update_autotest(
            {
                "id": model["id"],
                "projectId": state.project_id,
                "externalId": run_case.external_id,
                "name": model.get("name") or run_case.case.name,
                "namespace": model.get("namespace") or AUTOTEST_NAMESPACE,
                "steps": steps,
            }
        )


def _upload(client: TestItClient, attachment: Attachment) -> str:
    content_type = mimetypes.guess_type(attachment.path.name)[0] or "application/octet-stream"
    content = attachment.path.read_bytes()
    return str(client.upload_attachment(attachment.path.name, content, content_type)["id"])


def _result_body(run_case: RunCase, configuration_id: str, uploaded: dict[str, str]) -> Json:
    result = run_case.result

    def ids(files: list[Attachment]) -> list[Json]:
        return [{"id": uploaded[a.shown]} for a in files]

    body: Json = {
        "configurationId": configuration_id,
        "autoTestExternalId": run_case.external_id,
        "outcome": result.outcome,
        "message": result.comment or None,
        "attachments": ids(run_case.attachments),
        "stepResults": [
            {
                "title": f"Step {s.number}",
                "outcome": s.outcome,
                "info": s.comment or None,
                "attachments": ids(run_case.step_attachments.get(s.number, [])),
            }
            for s in result.steps
        ],
    }
    if result.seconds is not None:
        body["duration"] = result.seconds * 1000  # milliseconds
    return body


def _update_results(ws: Workspace, client: TestItClient, run_plan: RunPlan) -> list[Event]:
    run_id = run_plan.run_file.id or ""
    snapshot = ws.load_run_snapshot(run_id) or {"results": {}}
    events = []
    uploaded: dict[str, str] = {}  # one upload per file, also when cases share it
    for run_case in run_plan.cases:
        if run_case.result_id is None:
            continue
        result = run_case.result
        entry = snapshot["results"][str(run_case.case.global_id)]
        try:
            if run_case.result_changed:
                body: Json = {"outcome": result.outcome, "comment": result.comment or None}
                if result.seconds is not None:
                    body["durationInMs"] = result.seconds * 1000
                client.update_test_result(run_case.result_id, body)
                entry |= {
                    "outcome": result.outcome,
                    "comment": result.comment,
                    "seconds": result.seconds,
                }
            for attachment in run_case.new_attachments:
                if attachment.shown not in uploaded:
                    uploaded[attachment.shown] = _upload(client, attachment)
                client.attach_to_test_result(run_case.result_id, uploaded[attachment.shown])
                entry.setdefault("attachments", []).append(attachment.shown)
        except TestItError as e:
            events.append(Event("failed", run_case.label, str(e)))
            continue
        finally:
            ws.save_run_snapshot(run_id, snapshot)
        details = [str(result.outcome).lower()]
        if result.seconds is not None:
            details.append(format_duration(result.seconds))
        if run_case.new_attachments:
            details.append(f"+{_plural(len(run_case.new_attachments), 'attachment')}")
        events.append(Event("updated", run_case.label, ", ".join(details)))
    return events


# Output


def outcome_summary(run_plan: RunPlan) -> str:
    counts: dict[str, int] = {}
    for c in run_plan.cases:
        key = str(c.result.outcome).lower()
        counts[key] = counts.get(key, 0) + 1
    parts = [f"{n} {k}" for k, n in sorted(counts.items())]
    if run_plan.unchanged:
        parts.append(f"{run_plan.unchanged} unchanged")
    if run_plan.not_executed:
        parts.append(f"{run_plan.not_executed} not executed")
    summary = ", ".join(parts) or "nothing to upload"
    seconds = run_plan.run_file.seconds
    return f"{summary}; time {format_duration(seconds)}" if seconds is not None else summary


def describe_run(run_plan: RunPlan) -> list[Event]:
    """Lines for the dry-run output."""
    configuration = run_plan.configuration or "default"
    if run_plan.mode == "create":
        head = f"new run in plan #{run_plan.plan_global_id}, configuration {configuration!r}"
    else:
        head = f"update results of run {run_plan.run_file.id}"
    lines = [Event("run", run_plan.target, head)]
    for c in run_plan.cases:
        details = []
        if c.result.seconds is not None:
            details.append(format_duration(c.result.seconds))
        if c.result.steps and run_plan.mode == "create":
            details.append(_plural(len(c.result.steps), "step result"))
        files = _uploads(c)
        if files:
            verb = "" if run_plan.mode == "create" else "adds "
            details.append(f"{verb}{_plural(len(files), 'attachment')}")
        if run_plan.mode == "update" and not c.result_changed:
            details.insert(0, "result unchanged")
        if c.autotest in ("create", "link"):
            verb = "creates" if c.autotest == "create" else "links"
            details.append(f"{verb} autotest {c.external_id}; the case becomes automated")
        elif c.autotest == "update":
            details.append("updates the autotest steps")
        lines.append(Event(str(c.result.outcome).lower(), c.label, "; ".join(details)))
    unique = {a.shown: a for c in run_plan.cases for a in _uploads(c)}
    if unique:
        size = _size(sum(a.size for a in unique.values()))
        message = f"{_plural(len(unique), 'file')}, {size}; each file is uploaded once"
        lines.append(Event("upload", run_plan.target, message))
    return lines


def _size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"
