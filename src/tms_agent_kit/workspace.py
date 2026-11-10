"""Local workspace: case files under the root and tool state under `.tms/`."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tms_agent_kit.casefile import CaseFormatError, parse
from tms_agent_kit.config import Config
from tms_agent_kit.model import CaseFile

STATE_DIR = ".tms"
STATE_VERSION = 1

_SLUG_MAX = 60
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True)
class SectionState:
    name: str
    parent_id: str | None
    path: str  # POSIX path relative to the case root; "" is the root itself


@dataclass
class State:
    project_id: str
    project_global_id: int | None
    project_name: str
    sections: dict[str, SectionState] = field(default_factory=dict)
    attribute_names: dict[str, str] = field(default_factory=dict)
    shared_steps: dict[str, int] = field(default_factory=dict)  # uuid -> global id
    last_pull: str | None = None

    def section_by_path(self, path: str) -> str | None:
        for section_id, section in self.sections.items():
            if section.path == path:
                return section_id
        return None

    def section_label(self, section_id: str) -> str:
        return section_label(self.sections, section_id)


@dataclass(frozen=True)
class LocalFile:
    """A parsed case file."""

    path: Path
    case_file: CaseFile


@dataclass(frozen=True)
class BadFile:
    """A file under the root that is not a valid case file."""

    path: Path
    error: str


@dataclass
class Scan:
    by_id: dict[str, LocalFile] = field(default_factory=dict)
    new: list[LocalFile] = field(default_factory=list)
    errors: list[BadFile] = field(default_factory=list)


class Workspace:
    def __init__(self, config: Config) -> None:
        self.root = config.root
        self.plans_root = config.plans_root
        self.runs_root = config.runs_root
        self.base_dir = config.workspace
        self.state_dir = config.workspace / STATE_DIR

    # State

    def load_state(self) -> State | None:
        path = self.state_dir / "state.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != STATE_VERSION:
            raise ValueError(f"{path}: unsupported state version {data.get('version')}")
        sections = {k: SectionState(**v) for k, v in data["sections"].items()}
        return State(
            project_id=data["project_id"],
            project_global_id=data.get("project_global_id"),
            project_name=data.get("project_name", ""),
            sections=sections,
            attribute_names=data.get("attribute_names", {}),
            shared_steps=data.get("shared_steps", {}),
            last_pull=data.get("last_pull"),
        )

    def save_state(self, state: State) -> None:
        data = {"version": STATE_VERSION, **asdict(state)}
        _write_json(self.state_dir / "state.json", data)

    # Base snapshots

    def base_path(self, case_id: str) -> Path:
        return self.state_dir / "base" / f"{case_id}.json"

    def load_base(self, case_id: str) -> dict[str, Any] | None:
        path = self.base_path(case_id)
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def require_base(self, case_id: str) -> dict[str, Any]:
        base = self.load_base(case_id)
        if base is None:
            raise FileNotFoundError(self.base_path(case_id))
        return base

    def save_base(self, case_id: str, raw: dict[str, Any]) -> None:
        _write_json(self.base_path(case_id), raw)

    def delete_base(self, case_id: str) -> None:
        self.base_path(case_id).unlink(missing_ok=True)

    def base_ids(self) -> list[str]:
        directory = self.state_dir / "base"
        if not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))

    # Plan snapshots: what the last plan push sent, in tool terms (not raw TMS JSON)

    def plan_snapshot_path(self, plan_id: str) -> Path:
        return self.state_dir / "plans" / f"{plan_id}.json"

    def load_plan_snapshot(self, plan_id: str) -> dict[str, Any] | None:
        path = self.plan_snapshot_path(plan_id)
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def save_plan_snapshot(self, plan_id: str, data: dict[str, Any]) -> None:
        _write_json(self.plan_snapshot_path(plan_id), data)

    def delete_plan_snapshot(self, plan_id: str) -> None:
        self.plan_snapshot_path(plan_id).unlink(missing_ok=True)

    def plan_snapshot_ids(self) -> list[str]:
        directory = self.state_dir / "plans"
        if not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.json"))

    # Run snapshots: what the last run push sent

    def run_snapshot_path(self, run_id: str) -> Path:
        return self.state_dir / "runs" / f"{run_id}.json"

    def load_run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        path = self.run_snapshot_path(run_id)
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def save_run_snapshot(self, run_id: str, data: dict[str, Any]) -> None:
        _write_json(self.run_snapshot_path(run_id), data)

    def plan_files(self) -> list[Path]:
        return _markdown_files(self.plans_root)

    # Case files

    def scan(self) -> Scan:
        """Parse every case file under the root.

        Files and folders whose names start with `_` or `.` are skipped.
        """
        scan = Scan()
        if not self.root.is_dir():
            return scan
        for path in _markdown_files(self.root):
            try:
                local = LocalFile(path, parse(path.read_text(encoding="utf-8")))
            except (CaseFormatError, UnicodeDecodeError) as e:
                scan.errors.append(BadFile(path, str(e)))
                continue
            case_id = local.case_file.meta.id
            if case_id is None:
                scan.new.append(local)
            elif case_id in scan.by_id:
                other = self.display(scan.by_id[case_id].path)
                scan.errors.append(BadFile(path, f"duplicate id {case_id}, also in {other}"))
            else:
                scan.by_id[case_id] = local
        return scan

    def section_path_of(self, path: Path) -> str:
        return path.parent.relative_to(self.root).as_posix() if path.parent != self.root else ""

    def case_path(self, section_path: str, global_id: int | None, name: str) -> Path:
        stem = slugify(name)
        if global_id is not None:
            stem = f"{global_id}-{stem}" if stem else str(global_id)
        return self.root / section_path / f"{stem or 'case'}.md"

    def write_file(self, path: Path, text: str) -> None:
        _write_text(path, text)

    def move_file(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        self.prune_empty_dirs(source.parent)

    def archive_file(self, path: Path) -> Path:
        target = self.state_dir / "archived" / path.name
        self.move_file(path, target)
        return target

    def remove_file(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self.prune_empty_dirs(path.parent)

    def sync_section_dirs(self, state: State, old_state: State | None = None) -> None:
        """Create a folder for every section, so empty sections are visible too.

        Empty folders of sections that no longer exist are removed.
        """
        current = {section.path for section in state.sections.values()}
        if old_state is not None:
            for section in old_state.sections.values():
                if section.path not in current:
                    self.prune_empty_dirs(self.root / section.path)
        for path in sorted(current):
            (self.root / path).mkdir(parents=True, exist_ok=True)

    def prune_empty_dirs(self, directory: Path) -> None:
        while directory != self.root and directory.is_relative_to(self.root):
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent

    def display(self, path: Path) -> str:
        try:
            return path.relative_to(self.base_dir).as_posix()
        except ValueError:
            return str(path)


def section_paths(sections: list[dict[str, Any]]) -> dict[str, SectionState]:
    """Map Test IT sections to folders. A single top-level section maps to the root itself."""
    by_id = {s["id"]: s for s in sections if not s.get("isDeleted")}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for section in by_id.values():
        parent = section.get("parentId")
        children.setdefault(parent if parent in by_id else None, []).append(section)
    result: dict[str, SectionState] = {}

    def visit_children(parent_id: str | None, parent_path: str) -> None:
        used: set[str] = set()
        for child in sorted(children.get(parent_id, []), key=lambda s: (s["name"], s["id"])):
            name = folder_name(child["name"])
            if name.casefold() in used:
                # Sibling sections with the same name need distinct folders.
                name = f"{name} ({child['id'][:8]})"
            used.add(name.casefold())
            visit(child, f"{parent_path}/{name}" if parent_path else name)

    def visit(section: dict[str, Any], path: str) -> None:
        result[section["id"]] = SectionState(section["name"], section.get("parentId"), path)
        visit_children(section["id"], path)

    roots = children.get(None, [])
    if len(roots) == 1:
        visit(roots[0], "")
    else:
        visit_children(None, "")
    return result


def _markdown_files(root: Path) -> list[Path]:
    """`.md` files under `root`, skipping files and folders whose names start with `_` or `.`."""
    if not root.is_dir():
        return []
    return [
        path
        for path in sorted(root.rglob("*.md"))
        if not any(part.startswith(("_", ".")) for part in path.relative_to(root).parts)
    ]


def section_label(sections: dict[str, SectionState], section_id: str) -> str:
    """Full section name as shown in the TMS tree, e.g. `Demo / AI`."""
    names: list[str] = []
    current: str | None = section_id
    while current in sections and len(names) < len(sections):
        names.append(sections[current].name)
        current = sections[current].parent_id
    return " / ".join(reversed(names)) or section_id


def folder_name(name: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", unicodedata.normalize("NFC", name)).strip().strip(".")
    return cleaned or "_unnamed"


def slugify(name: str) -> str:
    """File-name slug of a case name. Long names are cut at a word boundary."""
    text = unicodedata.normalize("NFC", name).casefold()
    text = re.sub(r"[^\w]+", "-", text).strip("-_")
    if len(text) > _SLUG_MAX:
        cut = text[: _SLUG_MAX + 1]
        boundary = cut.rfind("-")
        # A very long last word is cut hard rather than dropped with most of the name.
        text = cut[:boundary] if boundary > _SLUG_MAX // 2 else text[:_SLUG_MAX]
    return text.rstrip("-_")


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, text: str) -> None:
    """Write atomically so an interrupted run never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
