"""Pull, diff and push between the workspace and Test IT."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tms_agent_kit.casefile import CaseFormatError, is_representable, parse, render
from tms_agent_kit.config import Config
from tms_agent_kit.model import Case, CaseFile
from tms_agent_kit.testit import TestItClient, TestItError
from tms_agent_kit.testit.mapping import (
    ProjectContext,
    changed_fields,
    create_body,
    to_case,
    to_meta,
    update_body,
    validate,
)
from tms_agent_kit.workspace import (
    LocalFile,
    SectionState,
    State,
    Workspace,
    folder_name,
    section_paths,
)

Json = dict[str, Any]

_GLOBAL_ID_PREFIX = re.compile(r"^(\d+)-")
_READ_ONLY_META = ("id", "global_id", "version_id", "version_number", "modified_date", "automated")


class SyncError(Exception):
    """The operation cannot start: missing state, wrong project, blocked plan."""


@dataclass(frozen=True)
class Event:
    kind: str
    target: str
    message: str = ""


def context(state: State) -> ProjectContext:
    return ProjectContext(state.project_id, state.attribute_names, state.shared_steps)


def case_file_from_raw(raw: Json, ctx: ProjectContext) -> CaseFile:
    case_file = CaseFile(meta=to_meta(raw), case=to_case(raw, ctx))
    if not is_representable(case_file):
        case_file = replace(case_file, meta=replace(case_file.meta, lossy=True))
    return case_file


def reference_case(raw: Json, ctx: ProjectContext) -> tuple[Case, list[str]]:
    """The case as its pulled file reads, and the fields Markdown cannot hold exactly.

    Local edits are compared with this reference, so a lossy field that nobody touched
    is not reported as a change.
    """
    case_file = case_file_from_raw(raw, ctx)
    if not case_file.meta.lossy:
        return case_file.case, []
    try:
        as_read = parse(render(case_file)).case
    except CaseFormatError:
        return case_file.case, list(Case.__dataclass_fields__)
    return as_read, changed_fields(case_file.case, as_read)


def load_state(ws: Workspace) -> State:
    state = ws.load_state()
    if state is None:
        raise SyncError("the workspace has not been pulled yet; run `tms-agent-kit pull`")
    return state


# Pull


def pull(
    ws: Workspace, client: TestItClient, config: Config, *, force: bool = False
) -> list[Event]:
    """Bring TMS changes into the workspace.

    Local edits are kept, and reported as conflicts when the TMS changed too.
    `force` discards local edits of cases. It never overwrites files of other cases.
    """
    project = client.get_project(config.project_id)
    old_state = ws.load_state()
    if old_state is not None and old_state.project_id != project["id"]:
        raise SyncError(
            f"the workspace was pulled from project {old_state.project_name!r}, "
            f"not {project.get('name')!r}; use a separate root"
        )
    sections = section_paths(list(client.iter_sections(config.project_id)))
    remote_items = list(client.search_work_items(config.project_id, {"isDeleted": False}))
    state = State(
        project_id=project["id"],
        project_global_id=project.get("globalId"),
        project_name=project.get("name", ""),
        sections=sections,
        attribute_names={
            a["id"]: a["name"] for a in project.get("attributesScheme") or [] if "id" in a
        },
        shared_steps={
            i["id"]: i["globalId"] for i in remote_items if i.get("entityTypeName") == "SharedSteps"
        },
        last_pull=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    # Local files were written with the previous state; compare them in that context.
    old = old_state or state
    old_ctx, ctx = context(old), context(state)
    scan = ws.scan()
    events = [Event("error", ws.display(f.path), f.error) for f in scan.errors]

    for item in remote_items:
        case_id = item["id"]
        base = ws.load_base(case_id)
        local = scan.by_id.get(case_id)
        remote_changed = (
            base is None
            or item.get("versionId") in (None, "")
            or (item["versionId"] != base.get("versionId"))
        )
        reuse_base = base is not None and not (remote_changed or force)
        raw = base if base is not None and reuse_base else client.get_work_item(case_id)
        if base is None and local is None:
            status = "unchanged"  # new in the TMS
        else:
            status = _local_status(ws, old, old_ctx, base or raw, local)
        if status != "unchanged" and not force:
            if remote_changed:
                events.append(
                    Event(
                        "conflict",
                        ws.display(local.path) if local else _label(raw),
                        f"changed locally ({status}) and in TMS; merge by hand "
                        "or run `pull --force` to discard local changes",
                    )
                )
            continue
        events += _write_case(ws, state, ctx, raw, local)

    remote_ids = {item["id"] for item in remote_items}
    for case_id in ws.base_ids():
        if case_id in remote_ids:
            continue
        base = ws.require_base(case_id)
        local = scan.by_id.get(case_id)
        status = _local_status(ws, old, old_ctx, base, local)
        if status == "modified" and not force:
            target = ws.display(local.path) if local else _label(base)
            events.append(Event("conflict", target, "deleted in TMS but changed locally"))
            continue
        if local is not None:
            archived = ws.archive_file(local.path)
            events.append(
                Event("archived", ws.display(local.path), f"moved to {ws.display(archived)}")
            )
        ws.delete_base(case_id)

    ws.sync_section_dirs(state, old_state)
    ws.save_state(state)
    return events


def _write_case(
    ws: Workspace, state: State, ctx: ProjectContext, raw: Json, local: LocalFile | None
) -> list[Event]:
    """Write the case file and its base snapshot from fresh TMS data."""
    case_file = case_file_from_raw(raw, ctx)
    section = state.sections.get(raw.get("sectionId") or "")
    if section is None:
        return [
            Event("error", _label(raw), f"section {raw.get('sectionId')} is not in the project")
        ]
    target = ws.case_path(section.path, case_file.meta.global_id, case_file.case.name)
    # Never overwrite a file that belongs to another case or is not a case file at all.
    if target.exists() and (local is None or local.path != target):
        return [Event("conflict", ws.display(target), f"path is taken; cannot write {_label(raw)}")]

    events: list[Event] = []
    current = target.read_text(encoding="utf-8") if target.exists() else None
    if local is not None and local.path != target:
        current = local.path.read_text(encoding="utf-8")
        ws.remove_file(local.path)
        events.append(Event("moved", ws.display(local.path), f"to {ws.display(target)}"))
    text = render(case_file)
    if current != text or not target.exists():
        ws.write_file(target, text)
    if current != text:
        kind = "created" if current is None and local is None else "updated"
        events.append(Event(kind, ws.display(target)))
    if case_file.meta.lossy:
        events.append(
            Event("lossy", ws.display(target), "some fields cannot be represented exactly")
        )
    ws.save_base(raw["id"], raw)
    return events


def _local_status(
    ws: Workspace, state: State, ctx: ProjectContext, base: Json, local: LocalFile | None
) -> Literal["unchanged", "modified", "missing"]:
    if local is None:
        return "missing"
    if state.section_by_path(ws.section_path_of(local.path)) != base.get("sectionId"):
        return "modified"
    if local.case_file.case != reference_case(base, ctx)[0]:
        return "modified"
    return "unchanged"


def _label(raw: Json) -> str:
    return f"#{raw.get('globalId')} {raw.get('name', '')!r}"


# Plan (diff)


@dataclass
class CreateSection:
    """A folder that holds cases but is not a TMS section yet."""

    target: str
    folder: str  # POSIX path relative to the case root
    name: str
    parent_folder: str
    section: str  # full section name, e.g. `Demo / AI / Limits`
    conflict: str | None = None
    kind: Literal["section"] = "section"


@dataclass(frozen=True)
class _SectionRef:
    folder: str
    section_id: str | None  # None until the section is created by push
    label: str


@dataclass
class Update:
    target: str
    path: Path
    case_id: str
    base: Json
    local: CaseFile
    section_id: str | None
    folder: str
    section: str
    fields: list[str]
    diff: str
    from_section: str | None = None  # full name of the old section when the case moves
    conflict: str | None = None
    kind: Literal["update"] = "update"


@dataclass
class Create:
    target: str
    path: Path
    local: CaseFile
    section_id: str | None
    folder: str
    section: str
    diff: str
    conflict: str | None = None
    kind: Literal["create"] = "create"


@dataclass
class Delete:
    target: str
    case_id: str
    base: Json
    conflict: str | None = None
    kind: Literal["delete"] = "delete"


Change = CreateSection | Update | Create | Delete


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    errors: list[Event] = field(default_factory=list)
    warnings: list[Event] = field(default_factory=list)

    @property
    def conflicts(self) -> list[Change]:
        return [c for c in self.changes if c.conflict]

    @property
    def blocked(self) -> bool:
        return bool(self.errors or self.conflicts)

    def without_deletes(self) -> Plan:
        changes: list[Change] = [c for c in self.changes if not isinstance(c, Delete)]
        return Plan(changes=changes, errors=self.errors, warnings=self.warnings)

    def token(self) -> str:
        """Short hash of exactly what push would do. Any change to the plan changes it."""
        entries = []
        for c in self.changes:
            entry: dict[str, Any] = {"kind": c.kind, "target": c.target}
            if isinstance(c, Update | Delete):
                entry |= {"id": c.case_id, "base_version": c.base.get("versionId")}
            if isinstance(c, Update | Create):
                entry["content"] = hashlib.sha256(c.path.read_bytes()).hexdigest()
                entry["section"] = c.section_id or c.folder
            if isinstance(c, CreateSection):
                entry["folder"] = c.folder
            entries.append(entry)
        data = json.dumps(entries, sort_keys=True).encode()
        return hashlib.sha256(data).hexdigest()[:12]


def plan(ws: Workspace, state: State) -> Plan:
    """Local changes against the base snapshots. Makes no network calls."""
    ctx = context(state)
    scan = ws.scan()
    result = Plan(errors=[Event("error", ws.display(f.path), f.error) for f in scan.errors])
    new_sections: dict[str, CreateSection] = {}

    for case_id, local in scan.by_id.items():
        target = ws.display(local.path)
        base = ws.load_base(case_id)
        if base is None:
            result.errors.append(Event("error", target, "unknown id: no base snapshot; run pull"))
            continue
        ref = _section_of(ws, state, local, result, new_sections)
        if ref is None:
            continue
        base_file = case_file_from_raw(base, ctx)
        reference, lossy_fields = reference_case(base, ctx)
        fields = changed_fields(reference, local.case_file.case)
        if ref.section_id != base.get("sectionId"):
            fields.append("section")
        for name in _READ_ONLY_META:
            if getattr(local.case_file.meta, name) != getattr(base_file.meta, name):
                result.warnings.append(
                    Event("warning", target, f"read-only field {name} was edited; ignored")
                )
        if not fields:
            continue
        if "type" in fields:
            result.errors.append(Event("error", target, "type cannot be changed"))
            continue
        if _invalid(local.case_file, target, result):
            continue
        blocked = sorted(set(fields) & set(lossy_fields))
        if blocked:
            result.errors.append(
                Event(
                    "error",
                    target,
                    f"{', '.join(blocked)} cannot be represented exactly in Markdown; "
                    "edit in the TMS",
                )
            )
            continue
        result.changes.append(
            Update(
                target=target,
                path=local.path,
                case_id=case_id,
                base=base,
                local=local.case_file,
                section_id=ref.section_id,
                folder=ref.folder,
                section=ref.label,
                fields=fields,
                diff=_unified(render(base_file), local.path.read_text(encoding="utf-8"), target),
                from_section=(
                    state.section_label(base["sectionId"]) if "section" in fields else None
                ),
            )
        )

    pulled_global_ids = {base.get("globalId") for base in map(ws.require_base, ws.base_ids())}
    for local in scan.new:
        target = ws.display(local.path)
        match = _GLOBAL_ID_PREFIX.match(local.path.name)
        if match and int(match.group(1)) in pulled_global_ids:
            # Most likely a pulled case whose frontmatter was removed: pushing would duplicate it.
            result.errors.append(
                Event(
                    "error",
                    target,
                    f"file name matches case #{match.group(1)} but the file has no id; "
                    "restore the frontmatter, or rename the file to create a new case",
                )
            )
            continue
        ref = _section_of(ws, state, local, result, new_sections)
        if ref is None or _invalid(local.case_file, target, result):
            continue
        result.changes.append(
            Create(
                target=target,
                path=local.path,
                local=local.case_file,
                section_id=ref.section_id,
                folder=ref.folder,
                section=ref.label,
                diff=_unified("", local.path.read_text(encoding="utf-8"), target),
            )
        )

    for case_id in ws.base_ids():
        if case_id not in scan.by_id:
            base = ws.require_base(case_id)
            result.changes.append(Delete(target=_label(base), case_id=case_id, base=base))
    # Sections first, parents before children: cases are written into them.
    used = {c.folder for c in result.changes if isinstance(c, Update | Create)}
    sections = [s for s in new_sections.values() if any(_within(f, s.folder) for f in used)]
    result.changes[:0] = sections
    result.warnings += _emptied_sections(ws, state, result)
    return result


def _emptied_sections(ws: Workspace, state: State, result: Plan) -> list[Event]:
    """Warn about sections that lose their last case and have no subsections.

    The tool never deletes sections, so a human has to decide whether to remove them.
    """
    cases = Counter(ws.require_base(i).get("sectionId") for i in ws.base_ids())
    for change in result.changes:
        if isinstance(change, Update) and "section" in change.fields:
            cases[change.base.get("sectionId")] -= 1
        if isinstance(change, Update | Create) and change.section_id is not None:
            cases[change.section_id] += 1
    parents = {s.parent_id for s in state.sections.values()}
    parents |= {
        state.section_by_path(c.parent_folder)
        for c in result.changes
        if isinstance(c, CreateSection)
    }
    moved_from = {
        c.base.get("sectionId")
        for c in result.changes
        if isinstance(c, Update) and "section" in c.fields
    }
    return [
        Event(
            "warning",
            ws.display(ws.root / state.sections[section_id].path) + "/",
            f"section {state.section_label(section_id)!r} will have no cases; "
            "the tool does not delete sections, remove it in the TMS if it is not needed",
        )
        for section_id in sorted(moved_from, key=str)
        if section_id in state.sections and cases[section_id] <= 0 and section_id not in parents
    ]


def _within(folder: str, ancestor: str) -> bool:
    return folder == ancestor or folder.startswith(ancestor + "/")


def check_remote(change_plan: Plan, client: TestItClient) -> None:
    """Mark updates and deletes whose TMS version moved since the last pull."""
    for change in change_plan.changes:
        if isinstance(change, Create | CreateSection):
            continue
        try:
            remote = client.get_work_item(change.case_id)
        except TestItError as e:
            if e.status_code == 404:
                change.conflict = "not found in TMS; run pull"
                continue
            raise
        change.conflict = _version_conflict(change.base, remote)


def _version_conflict(base: Json, remote: Json) -> str | None:
    if remote.get("isDeleted"):
        return "archived in TMS; run pull"
    if remote.get("versionId") != base.get("versionId"):
        return (
            f"changed in TMS since last pull (version {base.get('versionNumber')} -> "
            f"{remote.get('versionNumber')}); run pull"
        )
    return None


def _invalid(case_file: CaseFile, target: str, result: Plan) -> bool:
    problems = validate(case_file.case)
    result.errors += [Event("error", target, problem) for problem in problems]
    return bool(problems)


def _section_of(
    ws: Workspace,
    state: State,
    local: LocalFile,
    result: Plan,
    new_sections: dict[str, CreateSection],
) -> _SectionRef | None:
    """The section of the file's folder. A folder that is not a section yet is planned as one."""
    folder = ws.section_path_of(local.path)
    section_id = state.section_by_path(folder)
    if section_id is not None:
        return _SectionRef(folder, section_id, state.section_label(section_id))
    problem = _plan_sections(ws, state, folder, new_sections)
    if problem:
        result.errors.append(Event("error", ws.display(local.path), problem))
        return None
    return _SectionRef(folder, None, new_sections[folder].section)


def _plan_sections(
    ws: Workspace, state: State, folder: str, new_sections: dict[str, CreateSection]
) -> str | None:
    """Plan sections for `folder` and its missing parents. Returns a problem, if any."""
    if not folder:
        return "the case root is not a TMS section; put the file into a section folder"
    parts = folder.split("/")
    for depth in range(1, len(parts) + 1):
        path = "/".join(parts[:depth])
        if state.section_by_path(path) is not None or path in new_sections:
            continue
        name, parent = parts[depth - 1], "/".join(parts[: depth - 1])
        if folder_name(name) != name:
            return f"folder {name!r} cannot become a section; rename it to {folder_name(name)!r}"
        parent_id = state.section_by_path(parent)
        if parent_id is None and parent not in new_sections:
            return f"folder {parent or '.'!r} is not a TMS section"
        if parent_id is not None:
            siblings = {
                s.name.casefold() for s in state.sections.values() if s.parent_id == parent_id
            }
            if name.casefold() in siblings:
                return f"section {name!r} already exists in a folder with another name"
            parent_label = state.section_label(parent_id)
        else:
            parent_label = new_sections[parent].section
        new_sections[path] = CreateSection(
            target=ws.display(ws.root / path) + "/",
            folder=path,
            name=name,
            parent_folder=parent,
            section=f"{parent_label} / {name}",
        )
    return None


def _unified(before: str, after: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"tms/{name}",
            tofile=f"local/{name}",
        )
    )


# Push


def push(ws: Workspace, state: State, client: TestItClient, change_plan: Plan) -> list[Event]:
    """Apply a confirmed plan. Each change re-checks the TMS version right before writing."""
    if change_plan.blocked:
        raise SyncError("the plan has errors or conflicts")
    events: list[Event] = []
    for change in change_plan.changes:
        try:
            events += _apply(ws, state, client, change)
        except (TestItError, ValueError) as e:
            events.append(Event("failed", change.target, str(e)))
    ws.sync_section_dirs(state)
    ws.save_state(state)
    return events


def _apply(ws: Workspace, state: State, client: TestItClient, change: Change) -> list[Event]:
    ctx = context(state)
    if isinstance(change, CreateSection):
        parent_id = state.section_by_path(change.parent_folder)
        if parent_id is None:
            raise ValueError(f"parent section {change.parent_folder!r} was not created")
        body = {
            "name": change.name,
            "projectId": state.project_id,
            "parentId": parent_id,
            "attachments": [],
        }
        created = client.create_section(body)
        state.sections[created["id"]] = SectionState(change.name, parent_id, change.folder)
        return [Event("created", change.target, f"section {change.section!r}")]

    if isinstance(change, Create):
        section_id = _resolve_section(state, change)
        created = client.create_work_item(create_body(change.local.case, section_id, ctx))
        fresh = client.get_work_item(created["id"])
        if fresh.get("entityTypeName") == "SharedSteps":
            state.shared_steps[fresh["id"]] = fresh["globalId"]
        ws.remove_file(change.path)
        return [
            Event("created", change.target, f"#{fresh.get('globalId')} in TMS"),
            *_write_case(ws, state, context(state), fresh, None),
        ]

    remote = client.get_work_item(change.case_id)
    conflict = _version_conflict(change.base, remote)
    if conflict:
        return [Event("conflict", change.target, conflict)]

    if isinstance(change, Delete):
        client.archive_work_item(change.case_id)
        ws.delete_base(change.case_id)
        return [Event("archived", change.target, "moved to the TMS archive")]

    reference = reference_case(change.base, ctx)[0]
    section_id = _resolve_section(state, change)
    client.update_work_item(update_body(remote, reference, change.local.case, section_id, ctx))
    fresh = client.get_work_item(change.case_id)
    return [
        Event("pushed", change.target, ", ".join(change.fields)),
        *_write_case(ws, state, ctx, fresh, LocalFile(change.path, change.local)),
    ]


def _resolve_section(state: State, change: Update | Create) -> str:
    section_id = change.section_id or state.section_by_path(change.folder)
    if section_id is None:
        raise ValueError(f"section for folder {change.folder!r} was not created")
    return section_id
