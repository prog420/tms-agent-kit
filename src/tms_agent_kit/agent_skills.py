"""Agent skills shipped with the package, and their installation into a repository.

Each installed skill folder gets a manifest with the hashes of the files it installed. An update
overwrites a file only if it still has the installed content, so local edits are never lost.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from tms_agent_kit import __version__

MANIFEST = ".tms-agent-kit.json"
DEFAULT_DIR = Path(".claude") / "skills"


@dataclass(frozen=True)
class Change:
    kind: str  # created | updated | removed | kept
    path: Path
    message: str = ""


def bundled_skills() -> dict[str, Traversable]:
    root = files("tms_agent_kit") / "skills"
    return {
        entry.name: entry
        for entry in sorted(root.iterdir(), key=lambda e: e.name)
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    }


def skill_description(skill: Traversable) -> str:
    for line in (skill / "SKILL.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("description:"):
            return line.removeprefix("description:").strip()
    return ""


def install(target: Path, *, force: bool = False) -> list[Change]:
    """Install or update every bundled skill under `target`."""
    changes: list[Change] = []
    for name, skill in bundled_skills().items():
        changes += _install_skill(skill, target / name, force=force)
    return changes


def _install_skill(skill: Traversable, folder: Path, *, force: bool) -> list[Change]:
    manifest_path = folder / MANIFEST
    installed: dict[str, str] = {}
    if manifest_path.is_file():
        installed = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})
    bundled = dict(_files(skill))
    changes: list[Change] = []
    recorded: dict[str, str] = {}
    for relative, content in bundled.items():
        path = folder / relative
        digest = _sha(content)
        if not path.exists():
            changes.append(Change("created", path))
        elif path.read_bytes() == content:
            recorded[relative] = digest
            continue
        elif _sha(path.read_bytes()) == installed.get(relative) or force:
            changes.append(Change("updated", path))
        else:
            changes.append(Change("kept", path, "changed locally; --force overwrites it"))
            if relative in installed:
                recorded[relative] = installed[relative]
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        recorded[relative] = digest
    # Files an earlier version installed and nobody changed since.
    for relative, digest in installed.items():
        path = folder / relative
        if relative in bundled or not path.is_file():
            continue
        if _sha(path.read_bytes()) == digest:
            path.unlink()
            changes.append(Change("removed", path, "no longer part of the skill"))
        else:
            changes.append(Change("kept", path, "no longer part of the skill; changed locally"))
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"version": __version__, "files": dict(sorted(recorded.items()))}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return changes


def _files(root: Traversable, prefix: str = "") -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        relative = f"{prefix}{entry.name}"
        if entry.is_dir():
            found += _files(entry, f"{relative}/")
        else:
            found.append((relative, entry.read_bytes()))
    return found


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def repository_root(start: Path) -> Path:
    """The nearest folder with `.git`, or `start` itself."""
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start
