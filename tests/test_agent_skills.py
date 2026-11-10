import hashlib
import json
from pathlib import Path

import pytest

from tms_agent_kit.agent_skills import (
    MANIFEST,
    Change,
    bundled_skills,
    install,
    repository_root,
)


def kinds(changes: list[Change]) -> list[tuple[str, str]]:
    return sorted((c.kind, c.path.name) for c in changes)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_bundled_skills() -> None:
    skills = bundled_skills()
    assert set(skills) == {"test", "tms-test-cases", "tms-test-run"}
    for name, skill in skills.items():
        head = (skill / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        assert f"name: {name}\n" in head  # the Agent Skills format requires a matching name


def test_install_update_and_local_edits(tmp_path: Path) -> None:
    target = tmp_path / ".claude" / "skills"
    first = install(target)
    assert {c.kind for c in first} == {"created"}
    skill_md = target / "tms-test-run" / "SKILL.md"
    assert skill_md.is_file()
    manifest = json.loads((target / "tms-test-run" / MANIFEST).read_text())
    assert set(manifest["files"]) == {"SKILL.md", "plan-and-run-files.md"}
    assert install(target) == []

    # A file from an older version that nobody changed is updated.
    old = "old version\n"
    skill_md.write_text(old)
    manifest["files"]["SKILL.md"] = sha(old)
    (target / "tms-test-run" / MANIFEST).write_text(json.dumps(manifest))
    assert kinds(install(target)) == [("updated", "SKILL.md")]

    # A local edit is kept, and kept again on the next install.
    skill_md.write_text("my edit\n")
    (change,) = install(target)
    assert (change.kind, change.message) == ("kept", "changed locally; --force overwrites it")
    assert skill_md.read_text() == "my edit\n"
    assert kinds(install(target)) == [("kept", "SKILL.md")]
    assert kinds(install(target, force=True)) == [("updated", "SKILL.md")]
    assert install(target) == []


def test_files_dropped_from_a_skill(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    install(target)
    folder = target / "test"
    manifest = json.loads((folder / MANIFEST).read_text())
    (folder / "old.md").write_text("old\n")
    (folder / "edited.md").write_text("edited\n")
    manifest["files"] |= {"old.md": sha("old\n"), "edited.md": sha("original\n")}
    (folder / MANIFEST).write_text(json.dumps(manifest))
    (folder / "mine.md").write_text("not installed by the tool\n")

    assert kinds(install(target)) == [("kept", "edited.md"), ("removed", "old.md")]
    assert not (folder / "old.md").exists()
    assert (folder / "mine.md").exists()


@pytest.mark.parametrize("with_git", [True, False])
def test_repository_root(tmp_path: Path, with_git: bool) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    if with_git:
        (tmp_path / ".git").mkdir()
    assert repository_root(nested) == (tmp_path if with_git else nested).resolve()
