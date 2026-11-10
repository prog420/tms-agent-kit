"""`init`: lay out a test repository and add TMS projects to it.

Existing files are never overwritten: `init` can run again to add a project or new template files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from tms_agent_kit.config import CONFIG_FILE, ConfigError, load_config

PROJECTS_DIR = "projects"
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Written:
    kind: str  # created | exists
    path: Path


def init_repository(root: Path) -> list[Written]:
    """Write the template files that are missing in `root`."""
    template = files("tms_agent_kit") / "templates" / "test-repo"
    results: list[Written] = []
    for entry in sorted(template.iterdir(), key=lambda e: e.name):
        if not entry.is_file():
            continue
        name = entry.name
        text = entry.read_text(encoding="utf-8")
        if name.startswith("dot-"):
            name = "." + name.removeprefix("dot-")
        if name.endswith(".tmpl"):
            name = name.removesuffix(".tmpl")
            text = text.format(name=_package_name(root.resolve().name))
        results.append(_write_new(root / name, text))
    projects = root / PROJECTS_DIR
    if not projects.is_dir():
        projects.mkdir(parents=True)
        results.append(Written("created", projects))
    return results


def add_project(root: Path, name: str, base_url: str, project_id: str) -> Written:
    """Write `projects/<name>/tms-agent-kit.toml` and check it loads."""
    if not _PROJECT_NAME_RE.match(name):
        raise ConfigError(
            f"project name {name!r}: letters, digits, '.', '_', '-'; starts with a letter or digit"
        )
    folder = root / PROJECTS_DIR / name
    path = folder / CONFIG_FILE
    if path.exists():
        return Written("exists", path)
    value = project_id if project_id.isdigit() else f'"{project_id}"'
    text = f'tms = "testit"\nbase_url = "{base_url}"\nproject_id = {value}  # UUID or global id\n'
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        load_config(path)
    except ConfigError:
        path.unlink()
        if not any(folder.iterdir()):
            folder.rmdir()
        raise
    return Written("created", path)


def _write_new(path: Path, text: str) -> Written:
    if path.exists():
        return Written("exists", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return Written("created", path)


def _package_name(folder: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", folder.lower()).strip("-")
    return name or "tests"
