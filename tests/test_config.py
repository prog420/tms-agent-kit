from pathlib import Path

import pytest

from tms_agent_kit.config import CONFIG_FILE, ConfigError, find_config, load_config, load_token

REPO_ROOT = Path(__file__).resolve().parent.parent

VALID = """
tms = "testit"
base_url = "https://example.testit.software/"
project_id = "42"
"""


def write_config(directory: Path, text: str = VALID) -> Path:
    path = directory / CONFIG_FILE
    path.write_text(text, encoding="utf-8")
    return path


def test_load_valid_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    assert config.tms == "testit"
    assert config.base_url == "https://example.testit.software"
    assert config.project_id == "42"
    assert config.root == tmp_path.resolve() / "cases"
    assert config.plans_root == tmp_path.resolve() / "plans"
    assert config.runs_root == tmp_path.resolve() / "runs"
    assert config.workspace == tmp_path.resolve()


def test_custom_root(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, VALID + 'root = "tests/cases"\n'))
    assert config.root == tmp_path.resolve() / "tests" / "cases"


def test_project_id_as_number(tmp_path: Path) -> None:
    text = 'tms = "testit"\nbase_url = "https://x.example"\nproject_id = 42\n'
    assert load_config(write_config(tmp_path, text)).project_id == "42"


def test_http_allowed_for_localhost(tmp_path: Path) -> None:
    text = VALID.replace("https://example.testit.software/", "http://localhost:8080")
    assert load_config(write_config(tmp_path, text)).base_url == "http://localhost:8080"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('base_url = "https://x"\nproject_id = "1"\n', "tms is required"),
        ('tms = "jira"\nbase_url = "https://x"\nproject_id = "1"\n', "unsupported tms"),
        ('tms = "testit"\nbase_url = "x.com"\nproject_id = "1"\n', "absolute URL"),
        ('tms = "testit"\nbase_url = "ftp://x.com"\nproject_id = "1"\n', "absolute URL"),
        ('tms = "testit"\nbase_url = "http://x.com"\nproject_id = "1"\n', "must use https"),
        ('tms = "testit"\nbase_url = "https://x.com?a=1"\nproject_id = "1"\n', "query"),
        ('tms = "testit"\nbase_url = "https://x"\n', "project_id is required"),
        ('tms = "testit"\nbase_url = "https://x"\nproject_id = 0\n', "project_id is required"),
        ('tms = "testit"\nbase_url = "https://x"\nproject_id = true\n', "project_id is required"),
        (VALID + 'root = "../outside"\n', "root must be a subdirectory"),
        (VALID + 'root = "."\n', "root must be a subdirectory"),
        (VALID + 'plans_root = "../plans"\n', "plans_root must be a subdirectory"),
        (VALID + 'plans_root = "cases/plans"\n', "root and plans_root must not overlap"),
        (VALID + 'runs_root = "plans"\n', "plans_root and runs_root must not overlap"),
        (VALID + 'token = "secret"\n', "unknown keys: token"),
        ("tms = ", "Invalid"),
    ],
)
def test_invalid_config(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, text))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="file not found"):
        load_config(tmp_path / CONFIG_FILE)


def test_example_config_is_valid() -> None:
    config = load_config(REPO_ROOT / "tms-agent-kit.example.toml")
    assert config.tms == "testit"


def test_find_config_in_parent(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config(nested) == path.resolve()


def test_find_config_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        find_config(tmp_path)


def test_token_from_environment_wins(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    (tmp_path / ".env").write_text("TESTIT_TOKEN=from-file\n", encoding="utf-8")
    assert load_token(config, {"TESTIT_TOKEN": "from-env"}) == "from-env"


@pytest.mark.parametrize(
    "line",
    [
        "TESTIT_TOKEN=from-file",
        "export TESTIT_TOKEN=from-file",
        'TESTIT_TOKEN="from-file"',
        "TESTIT_TOKEN='from-file'",
        "TESTIT_TOKEN=from-file # comment",
    ],
)
def test_token_from_dotenv(tmp_path: Path, line: str) -> None:
    config = load_config(write_config(tmp_path))
    (tmp_path / ".env").write_text(f"# comment\n{line}\n", encoding="utf-8")
    assert load_token(config, {}) == "from-file"


@pytest.mark.parametrize("dotenv", [None, "", "TESTIT_TOKEN=\n", "TESTIT_TOKEN=   \n"])
def test_token_missing(tmp_path: Path, dotenv: str | None) -> None:
    config = load_config(write_config(tmp_path))
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    with pytest.raises(ConfigError, match="TESTIT_TOKEN is not set"):
        load_token(config, {})


def test_token_from_dotenv_at_the_repository_root(tmp_path: Path) -> None:
    """Projects in subfolders of one repository share the root `.env`."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".env").write_text("TESTIT_TOKEN=root\n", encoding="utf-8")
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    config = load_config(write_config(project))
    assert load_token(config, {}) == "root"
    # The nearest file that sets the token wins.
    (project / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert load_token(config, {}) == "root"
    (project / ".env").write_text("TESTIT_TOKEN=project\n", encoding="utf-8")
    assert load_token(config, {}) == "project"


def test_dotenv_outside_the_repository_is_not_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TESTIT_TOKEN=unrelated\n", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    project = repo / "projects" / "demo"
    project.mkdir(parents=True)
    with pytest.raises(ConfigError, match=r"\.env or .*repo/\.env"):
        load_token(load_config(write_config(project)), {})
    # Without a git repository, only the workspace `.env` counts.
    loose = tmp_path / "loose" / "demo"
    loose.mkdir(parents=True)
    with pytest.raises(ConfigError, match="is not set"):
        load_token(load_config(write_config(loose)), {})
