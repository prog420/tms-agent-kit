import tomllib
from pathlib import Path

import pytest

from tms_agent_kit.cli import EXIT_OK, main
from tms_agent_kit.config import CONFIG_FILE, ConfigError, load_config
from tms_agent_kit.scaffold import add_project, init_repository


def test_init_writes_the_template_once(tmp_path: Path) -> None:
    root = tmp_path / "My Tests"
    root.mkdir()
    created = {p.path.name for p in init_repository(root) if p.kind == "created"}
    assert created == {
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        ".env.example",
        ".gitignore",
        ".mcp.json",
        "pyproject.toml",
        "projects",
    }
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert pyproject["project"]["name"] == "my-tests"
    assert pyproject["tool"]["uv"]["sources"]["tms-agent-kit"]["git"].startswith("https://")
    gitignore = (root / ".gitignore").read_text()
    assert ".env\n" in gitignore
    assert "projects/*/runs/\n" in gitignore
    assert "projects/*/reports/\n" in gitignore

    (root / "AGENTS.md").write_text("mine\n")
    assert {p.kind for p in init_repository(root)} == {"exists"}
    assert (root / "AGENTS.md").read_text() == "mine\n"


def test_add_project(tmp_path: Path) -> None:
    item = add_project(tmp_path, "shop", "https://x.testit.software", "42")
    config = load_config(item.path)
    assert (config.project_id, config.base_url) == ("42", "https://x.testit.software")
    assert add_project(tmp_path, "shop", "https://other", "1").kind == "exists"

    uuid = "6f1c2e3a-0000-4000-8000-000000000001"
    assert load_config(add_project(tmp_path, "uuid", "https://x", uuid).path).project_id == uuid


@pytest.mark.parametrize(
    ("name", "url", "message"),
    [
        ("../escape", "https://x", "project name '../escape'"),
        ("bad", "http://example.com", "base_url"),
    ],
)
def test_add_project_rejects_bad_input(tmp_path: Path, name: str, url: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        add_project(tmp_path, name, url, "1")
    assert not (tmp_path / "projects" / "bad").exists()


def test_cli_init_and_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    args = ["init", "--project", "demo", "--base-url", "https://x", "--project-id", "7"]
    assert main(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "created   projects/demo/tms-agent-kit.toml\n" in out
    assert "created   .claude/skills/test/SKILL.md\n" in out
    assert "cd projects/demo && uv run tms-agent-kit check" in out
    assert (tmp_path / "projects" / "demo" / CONFIG_FILE).is_file()

    assert main(["init", "--project", "x"]) == 2
    assert "go together" in capsys.readouterr().err

    monkeypatch.chdir(tmp_path / "projects" / "demo")
    assert main(["skills", "install"]) == EXIT_OK
    assert "are up to date" in capsys.readouterr().out
    assert main(["skills", "list"]) == EXIT_OK
    assert "tms-test-run " in capsys.readouterr().out
