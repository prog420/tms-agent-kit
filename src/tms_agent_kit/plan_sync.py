"""Test plans: resolve plan files against local cases and compare them with the last push."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from tms_agent_kit.frontmatter import FormatError
from tms_agent_kit.model import normalize_text
from tms_agent_kit.plans import CaseRef, Plan, PlanFile, PlanMeta, parse_plan, set_plan_meta
from tms_agent_kit.sync import Event, SyncError
from tms_agent_kit.testit import TestItClient, TestItError
from tms_agent_kit.workspace import LocalFile, Scan, State, Workspace

Json = dict[str, Any]

_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?\s*\)")
_CODE_RE = re.compile(r"`([^`]+)`")
_ID_RE = re.compile(r"^#?(\d+)\b")
# Where a free comment may start after a path: " — note", " - note", " (note)".
_COMMENT_RE = re.compile(r"\s+(?:\u2014|\u2013|--|-|#)\s+|\s+\(")


@dataclass(frozen=True)
class PlannedCase:
    case_id: str
    global_id: int | None
    path: Path
    name: str


@dataclass
class PlanChange:
    kind: Literal["create", "update", "delete"]
    target: str
    path: Path | None = None
    plan_file: PlanFile | None = None
    cases: list[PlannedCase] = field(default_factory=list)
    snapshot: Json | None = None
    fields: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    conflict: str | None = None
    configuration_ids: list[str] = field(default_factory=list)  # set by check_plans_remote

    @property
    def plan(self) -> Plan | None:
        return self.plan_file.plan if self.plan_file else None


@dataclass
class PlanChanges:
    changes: list[PlanChange] = field(default_factory=list)
    errors: list[Event] = field(default_factory=list)
    warnings: list[Event] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.errors or any(c.conflict for c in self.changes))

    def without_deletes(self) -> PlanChanges:
        kept = [c for c in self.changes if c.kind != "delete"]
        return PlanChanges(changes=kept, errors=self.errors, warnings=self.warnings)

    def token(self) -> str:
        """Short hash of exactly what `plan push` would do."""
        entries = []
        for c in self.changes:
            entry: Json = {
                "kind": c.kind,
                "target": c.target,
                "cases": [p.case_id for p in c.cases],
            }
            if c.path is not None and c.path.is_file():
                entry["content"] = hashlib.sha256(c.path.read_bytes()).hexdigest()
            if c.snapshot is not None:
                entry["base"] = c.snapshot.get("modified_date")
            entries.append(entry)
        data = json.dumps(entries, sort_keys=True).encode()
        return hashlib.sha256(data).hexdigest()[:12]


def snapshot_of(plan: Plan, cases: list[PlannedCase]) -> Json:
    """The comparable part of a plan, as stored in `.tms/plans/` after a push."""
    return {
        "name": plan.name,
        "description": plan.description,
        "build": plan.build,
        "tags": list(plan.tags),
        "configurations": list(plan.configurations),
        "cases": sorted(c.case_id for c in cases),
    }


def plan_changes(ws: Workspace, scan: Scan | None = None) -> PlanChanges:
    """Plan files against the snapshots of the last plan push. Makes no network calls."""
    scan = scan or ws.scan()
    result = PlanChanges()
    seen_ids: dict[str, Path] = {}
    seen_names: dict[str, Path] = {}
    for path in ws.plan_files():
        target = ws.display(path)
        try:
            plan_file = parse_plan(path.read_text(encoding="utf-8"))
        except (FormatError, UnicodeDecodeError) as e:
            result.errors.append(Event("error", target, str(e)))
            continue
        cases, problems = resolve_cases(ws, scan, path, plan_file.plan.refs)
        result.errors += [Event("error", target, problem) for problem in problems]
        if problems:
            continue
        name_key = plan_file.plan.name.casefold()
        if name_key in seen_names:
            other = ws.display(seen_names[name_key])
            result.errors.append(Event("error", target, f"plan name is also used in {other}"))
            continue
        seen_names[name_key] = path
        plan_id = plan_file.meta.id
        if plan_id is None:
            result.changes.append(
                PlanChange("create", target, path, plan_file, cases, added=len(cases))
            )
            continue
        if plan_id in seen_ids:
            other = ws.display(seen_ids[plan_id])
            result.errors.append(Event("error", target, f"duplicate plan id, also in {other}"))
            continue
        seen_ids[plan_id] = path
        snapshot = ws.load_plan_snapshot(plan_id)
        if snapshot is None:
            result.errors.append(
                Event("error", target, "unknown plan id: it was not pushed from this workspace")
            )
            continue
        current = snapshot_of(plan_file.plan, cases)
        fields = [key for key in current if key != "cases" and current[key] != snapshot.get(key)]
        before, after = set(snapshot.get("cases", [])), set(current["cases"])
        if before != after:
            fields.append("cases")
        if fields:
            result.changes.append(
                PlanChange(
                    "update",
                    target,
                    path,
                    plan_file,
                    cases,
                    snapshot,
                    fields,
                    added=len(after - before),
                    removed=len(before - after),
                )
            )
            if before - after:
                result.warnings.append(
                    Event(
                        "warning",
                        target,
                        f"{len(before - after)} case(s) leave the plan; their test points "
                        "and results are removed from the plan",
                    )
                )

    for plan_id in ws.plan_snapshot_ids():
        if plan_id not in seen_ids:
            snapshot = ws.load_plan_snapshot(plan_id) or {}
            label = f"plan #{snapshot.get('global_id')} {snapshot.get('name', '')!r}"
            result.changes.append(PlanChange("delete", label, snapshot={**snapshot, "id": plan_id}))
    return result


def resolve_cases(
    ws: Workspace, scan: Scan, plan_path: Path, refs: tuple[CaseRef, ...]
) -> tuple[list[PlannedCase], list[str]]:
    """Cases selected by the references, in file order without duplicates, and problems."""
    by_global_id = {f.case_file.meta.global_id: f for f in scan.by_id.values()}
    by_path = {f.path.resolve(): f for f in [*scan.by_id.values(), *scan.new]}
    cases: dict[str, PlannedCase] = {}
    problems: list[str] = []
    for ref in refs:
        selected, problem = _resolve_ref(ws, plan_path, ref, by_global_id, by_path, scan)
        if problem:
            problems.append(f"line {ref.line}: {problem}")
            continue
        for local in selected:
            meta = local.case_file.meta
            if meta.id is not None and meta.id not in cases:
                cases[meta.id] = PlannedCase(
                    meta.id, meta.global_id, local.path, local.case_file.case.name
                )
    return list(cases.values()), problems


def _resolve_ref(
    ws: Workspace,
    plan_path: Path,
    ref: CaseRef,
    by_global_id: dict[int | None, LocalFile],
    by_path: dict[Path, LocalFile],
    scan: Scan,
) -> tuple[list[LocalFile], str | None]:
    link = _LINK_RE.search(ref.text)
    code = _CODE_RE.search(ref.text)
    id_match = _ID_RE.match(ref.text) if link is None and code is None else None
    if id_match and int(id_match.group(1)) in by_global_id:
        return _single(by_global_id[int(id_match.group(1))])
    if link is not None:
        candidates = [unquote(link.group(1))]
    elif code is not None:
        candidates = [code.group(1)]
    else:
        candidates = [ref.text, _COMMENT_RE.split(ref.text, maxsplit=1)[0]]
    for candidate in candidates:
        for full in _paths(ws, plan_path, candidate.strip().strip("\"'")):
            if full.is_file():
                local = by_path.get(full)
                if local is None:
                    return [], f"{candidate!r} is not a valid case file"
                return _single(local)
            if full.is_dir():
                selected = [
                    f
                    for f in scan.by_id.values()
                    if f.path.resolve().is_relative_to(full)
                    and f.case_file.case.type != "SharedSteps"
                ]
                if not selected:
                    return [], f"folder {candidate!r} has no pushed cases"
                return sorted(selected, key=lambda f: f.path), None
    if id_match:
        return [], f"case #{id_match.group(1)} is not in the local case files; run pull"
    return [], (
        f"cannot resolve {ref.text!r}: expected a case global id, "
        "a case file or a section folder under the case root"
    )


def _single(local: LocalFile) -> tuple[list[LocalFile], str | None]:
    if local.case_file.meta.id is None:
        return [], f"{local.path.name} is not in the TMS yet; push it first"
    if local.case_file.case.type == "SharedSteps":
        return [], f"{local.path.name} is a shared step, not a case"
    return [local], None


def _paths(ws: Workspace, plan_path: Path, candidate: str) -> list[Path]:
    """Existing paths under the case root that `candidate` may mean."""
    if not candidate:
        return []
    result = []
    for base in (plan_path.parent, ws.root, ws.base_dir):
        full = (base / candidate).resolve()
        if full.exists() and full.is_relative_to(ws.root.resolve()) and full not in result:
            result.append(full)
    return result


def describe(change: PlanChange, *, allow_delete: bool) -> str:
    """Plan line detail: what changes and the resulting size."""
    if change.kind == "delete":
        return "" if allow_delete else "deleted locally; ignored without --allow-delete"
    if change.plan is None:
        return ""
    configurations = change.plan.configurations
    size = f"{len(change.cases)} case{'s' if len(change.cases) != 1 else ''} x " + (
        f"{len(configurations)} configuration{'s' if len(configurations) != 1 else ''}"
        if configurations
        else "default configuration"
    )
    if change.kind == "create":
        return size
    parts = [f for f in change.fields if f != "cases"]
    if "cases" in change.fields:
        parts.append(f"cases +{change.added} -{change.removed}")
    return f"{', '.join(parts)}; {size}"


# Remote check and push

# Plan fields that PUT /api/v2/testPlans accepts; the rest of the plan is server-managed.
_PLAN_PUT_FIELDS = (
    "id",
    "name",
    "startDate",
    "endDate",
    "description",
    "build",
    "projectId",
    "productName",
    "hasAutomaticDurationTimer",
    "attributes",
    "tags",
)


def check_plans_remote(changes: PlanChanges, client: TestItClient, state: State) -> None:
    """Resolve configuration names and mark plans that changed in the TMS since the last push."""
    configurations = client.list_configurations(state.project_id)
    for change in changes.changes:
        if change.plan is not None:
            ids, problem = configuration_ids(change.plan.configurations, configurations)
            if problem:
                changes.errors.append(Event("error", change.target, problem))
            change.configuration_ids = ids
        if change.snapshot is None:
            continue
        plan_id = str(change.snapshot["id"]) if change.kind == "delete" else _plan_id(change)
        change.conflict = _remote_conflict(client, plan_id, change.snapshot)


def _snapshot(change: PlanChange) -> Json:
    if change.snapshot is None:
        raise SyncError(f"{change.target}: no snapshot of the last plan push")
    return change.snapshot


def _plan_id(change: PlanChange) -> str:
    if change.plan_file is None or change.plan_file.meta.id is None:
        raise SyncError(f"{change.target}: plan has no id")
    return change.plan_file.meta.id


def configuration_ids(
    names: tuple[str, ...], configurations: list[Json]
) -> tuple[list[str], str | None]:
    """Configuration ids by name; no names means the project default."""
    live = [c for c in configurations if not c.get("isDeleted")]
    if not names:
        default = [c["id"] for c in live if c.get("isDefault")]
        return default[:1], None if default else "the project has no default configuration"
    by_name = {str(c.get("name", "")).casefold(): c["id"] for c in live}
    missing = [n for n in names if n.casefold() not in by_name]
    if missing:
        known = ", ".join(sorted(str(c.get("name")) for c in live))
        return [], f"unknown configuration(s): {', '.join(missing)}; known: {known}"
    return [by_name[n.casefold()] for n in names], None


def _remote_conflict(client: TestItClient, plan_id: str, snapshot: Json) -> str | None:
    try:
        remote = client.get_test_plan(plan_id)
    except TestItError as e:
        if e.status_code == 404:
            return "not found in TMS"
        raise
    if remote.get("isDeleted"):
        return "archived in TMS"
    points = list(client.search_test_points({"testPlanIds": [plan_id]}))
    current = _remote_snapshot(remote, points)
    # Test IT stores plan tags in lower case.
    expected = {**snapshot, "tags": sorted(t.casefold() for t in snapshot.get("tags", []))}
    changed = [key for key, value in current.items() if value != expected.get(key)]
    if changed:
        return f"changed in TMS since the last plan push: {', '.join(changed)}"
    return None


def _remote_snapshot(remote: Json, points: list[Json]) -> Json:
    """The managed part of a TMS plan, comparable with a push snapshot."""
    live = [p for p in points if not p.get("isDeleted")]
    return {
        "name": remote.get("name") or "",
        "description": normalize_text(remote.get("description")),
        "build": remote.get("build") or "",
        "tags": sorted(t["name"].casefold() for t in remote.get("tags") or []),
        "cases": sorted({p["workItemId"] for p in live}),
        "configuration_ids": sorted({p["configurationId"] for p in live}),
    }


def push_plans(
    ws: Workspace, state: State, client: TestItClient, changes: PlanChanges
) -> list[Event]:
    """Apply checked plan changes. Each update re-checks the TMS right before writing."""
    if changes.blocked:
        raise SyncError("the plan changes have errors or conflicts")
    events: list[Event] = []
    for change in changes.changes:
        try:
            events.append(_apply_plan(ws, state, client, change))
        except (TestItError, ValueError) as e:
            events.append(Event("failed", change.target, str(e)))
    return events


def _apply_plan(ws: Workspace, state: State, client: TestItClient, change: PlanChange) -> Event:
    if change.kind == "delete":
        plan_id = str(_snapshot(change)["id"])
        conflict = _remote_conflict(client, plan_id, _snapshot(change))
        if conflict:
            return Event("conflict", change.target, conflict)
        client.archive_test_plan(plan_id)
        ws.delete_plan_snapshot(plan_id)
        return Event("archived", change.target, "moved to the TMS archive")

    plan, path = change.plan, change.path
    if plan is None or path is None:
        raise SyncError(f"{change.target}: nothing to push")
    case_ids = [c.case_id for c in change.cases]
    if change.kind == "create":
        created = client.create_test_plan(
            {
                "name": plan.name,
                "description": plan.description,
                "build": plan.build or None,
                "tags": [{"name": t} for t in plan.tags],
                "projectId": state.project_id,
                "attributes": {},
            }
        )
        plan_id = created["id"]
        suite = client.create_test_suite(
            {"name": plan.name, "testPlanId": plan_id, "type": "Custom"}
        )
        suite_id = suite["id"]
        client.set_suite_configurations(suite_id, change.configuration_ids)
        client.set_suite_work_items(suite_id, case_ids)
        meta = PlanMeta(id=plan_id, global_id=created.get("globalId"))
        ws.write_file(path, set_plan_meta(path.read_text(encoding="utf-8"), meta))
        message = f"plan #{meta.global_id} in TMS"
    else:
        snapshot = _snapshot(change)
        plan_id, suite_id = _plan_id(change), str(snapshot["suite_id"])
        conflict = _remote_conflict(client, plan_id, snapshot)
        if conflict:
            return Event("conflict", change.target, conflict)
        if {"name", "description", "build", "tags"} & set(change.fields):
            remote = client.get_test_plan(plan_id)
            body = {key: remote.get(key) for key in _PLAN_PUT_FIELDS}
            body |= {
                "name": plan.name,
                "description": plan.description,
                "build": plan.build or None,
                "tags": [{"name": t} for t in plan.tags],
            }
            client.update_test_plan(body)
        if "configurations" in change.fields:
            client.set_suite_configurations(suite_id, change.configuration_ids)
        if "cases" in change.fields:
            client.set_suite_work_items(suite_id, case_ids)
        meta = PlanMeta(id=plan_id, global_id=snapshot.get("global_id"))
        message = ", ".join(change.fields)

    ws.save_plan_snapshot(
        plan_id,
        {
            **snapshot_of(plan, change.cases),
            "global_id": meta.global_id,
            "suite_id": suite_id,
            "configuration_ids": sorted(change.configuration_ids),
        },
    )
    return Event("created" if change.kind == "create" else "pushed", change.target, message)


def list_plans(ws: Workspace, state: State, client: TestItClient) -> list[tuple[Json, str | None]]:
    """TMS plans of the project with the local plan file that manages each, if any."""
    files: dict[str, str] = {}
    for path in ws.plan_files():
        try:
            plan_id = parse_plan(path.read_text(encoding="utf-8")).meta.id
        except (FormatError, UnicodeDecodeError):
            continue
        if plan_id is not None:
            files[plan_id] = ws.display(path)
    plans = sorted(client.list_test_plans(state.project_id), key=lambda p: p.get("globalId") or 0)
    return [(plan, files.get(plan["id"])) for plan in plans]
