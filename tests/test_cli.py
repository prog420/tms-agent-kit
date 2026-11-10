import logging
from pathlib import Path

import httpx
import pytest

from tests.fake_testit import FakeTestIt
from tms_agent_kit.cli import (
    EXIT_CHANGES,
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    ClientFactory,
    main,
)
from tms_agent_kit.config import CONFIG_FILE, Config
from tms_agent_kit.testit import TestItClient

CONFIG = """
tms = "testit"
base_url = "https://example.testit.software"
project_id = "42"
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / CONFIG_FILE).write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TESTIT_TOKEN", "secret-token")
    return tmp_path


def fake_factory(handler: httpx.MockTransport | None = None) -> ClientFactory:
    transport = handler or httpx.MockTransport(api)

    def factory(config: Config, token: str) -> TestItClient:
        return TestItClient(config.base_url, token, transport=transport, sleep=lambda _: None)

    return factory


def api(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v2/projects/42":
        return httpx.Response(200, json={"name": "Demo", "globalId": 42})
    if request.url.path == "/api/v2/projects/42/sections":
        sections = [
            {"id": "a", "name": "Demo", "parentId": None},
            {"id": "b", "name": "Login/Logout", "parentId": "a"},
        ]
        return httpx.Response(200, json=sections)
    return httpx.Response(404)


def test_check_success(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"], client_factory=fake_factory()) == EXIT_OK
    out = capsys.readouterr().out
    assert "Project: Demo (global id 42)" in out
    assert "Sections: 2" in out
    assert "  cases/               Demo\n" in out
    assert "  cases/Login_Logout/  Demo / Login/Logout\n" in out


def test_check_api_error(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(401))
    assert main(["check"], client_factory=fake_factory(transport)) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "HTTP 401" in err
    assert "secret-token" not in err


def test_interrupt(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def interrupt(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    transport = httpx.MockTransport(interrupt)
    assert main(["check"], client_factory=fake_factory(transport)) == EXIT_INTERRUPTED
    assert "interrupted" in capsys.readouterr().err


def test_verbose_logs_requests_without_token(
    workspace: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # basicConfig is a no-op under pytest's handler; caplog still sees the records.
    monkeypatch.setattr(logging, "basicConfig", lambda **_: None)
    with caplog.at_level(logging.DEBUG):
        assert main(["-v", "check"], client_factory=fake_factory()) == EXIT_OK
    assert "/api/v2/projects/42" in caplog.text
    assert "secret-token" not in caplog.text


def test_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["check"]) == EXIT_ERROR
    assert "tms-agent-kit.toml not found" in capsys.readouterr().err


def test_missing_token(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TESTIT_TOKEN")
    assert main(["check"]) == EXIT_ERROR
    assert "TESTIT_TOKEN is not set" in capsys.readouterr().err


def test_explicit_config_path(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = workspace / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    args = ["--config", str(workspace / CONFIG_FILE), "check"]
    assert main(args, client_factory=fake_factory()) == EXIT_OK


def test_command_required(workspace: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == EXIT_ERROR


# pull / diff / push


@pytest.fixture
def synced(
    workspace: Path, fake: FakeTestIt, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, ClientFactory]:
    fake.add_item(name="Case", steps=[{"action": "A"}])
    factory = fake_factory(fake.transport)
    assert main(["pull"], client_factory=factory) == EXIT_OK
    capsys.readouterr()
    (path,) = (workspace / "cases").rglob("*.md")
    return path, factory


def test_pull_prints_created_files(
    workspace: Path, fake: FakeTestIt, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.add_item(name="Case")
    assert main(["pull"], client_factory=fake_factory(fake.transport)) == EXIT_OK
    assert "created   cases/AI/" in capsys.readouterr().out
    assert main(["pull"], client_factory=fake_factory(fake.transport)) == EXIT_OK
    assert "Up to date." in capsys.readouterr().out


def test_diff_exit_codes(
    synced: tuple[Path, ClientFactory], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = synced
    assert main(["diff"]) == EXIT_OK  # offline: no client needed
    path.write_text(path.read_text().replace("priority: Medium", "priority: Low"))
    assert main(["diff"]) == EXIT_CHANGES
    out = capsys.readouterr().out
    assert "update    cases/AI/" in out
    assert "-priority: Medium\n+priority: Low" in out
    path.write_text("broken")
    assert main(["diff"]) == EXIT_ERROR


def test_push_requires_matching_token(
    synced: tuple[Path, ClientFactory], fake: FakeTestIt, capsys: pytest.CaptureFixture[str]
) -> None:
    path, factory = synced
    path.write_text(path.read_text().replace("priority: Medium", "priority: Low"))

    assert main(["diff"]) == EXIT_CHANGES
    assert "+++ local/" in capsys.readouterr().out

    assert main(["push"], client_factory=factory) == EXIT_CHANGES
    out = capsys.readouterr().out
    assert "Nothing was written" in out
    assert "(priority; section 'Demo / AI', 1 step)" in out
    assert "Total: 1 update\n" in out
    assert "+++ local/" not in out  # push prints a summary; diff shows the full changes
    token = out.rsplit("--confirm ", 1)[1].strip()
    assert fake.writes == []

    assert main(["push", "--confirm", "wrong"], client_factory=factory) == EXIT_ERROR
    assert "plan changed" in capsys.readouterr().err
    assert fake.writes == []

    assert main(["push", "--confirm", token], client_factory=factory) == EXIT_OK
    assert "pushed    cases/AI/" in capsys.readouterr().out
    assert [method for method, _ in fake.writes] == ["PUT"]
    assert main(["push"], client_factory=factory) == EXIT_OK
    assert "Nothing to push." in capsys.readouterr().out


def test_push_token_is_invalidated_by_edits(
    synced: tuple[Path, ClientFactory], fake: FakeTestIt, capsys: pytest.CaptureFixture[str]
) -> None:
    path, factory = synced
    path.write_text(path.read_text().replace("priority: Medium", "priority: Low"))
    main(["push"], client_factory=factory)
    token = capsys.readouterr().out.rsplit("--confirm ", 1)[1].strip()
    path.write_text(path.read_text().replace("priority: Low", "priority: High"))
    assert main(["push", "--confirm", token], client_factory=factory) == EXIT_ERROR
    assert fake.writes == []


def test_push_delete_needs_flag(
    synced: tuple[Path, ClientFactory], fake: FakeTestIt, capsys: pytest.CaptureFixture[str]
) -> None:
    path, factory = synced
    path.unlink()
    assert main(["push"], client_factory=factory) == EXIT_OK
    out = capsys.readouterr().out
    assert "ignored without --allow-delete" in out
    assert "Nothing to push." in out

    assert main(["push", "--allow-delete"], client_factory=factory) == EXIT_CHANGES
    out = capsys.readouterr().out
    assert "push --allow-delete --confirm" in out
    token = out.rsplit("--confirm ", 1)[1].strip()
    args = ["push", "--allow-delete", "--confirm", token]
    assert main(args, client_factory=factory) == EXIT_OK
    assert [method for method, _ in fake.writes] == ["DELETE"]


def test_push_blocked_by_conflict(
    synced: tuple[Path, ClientFactory], fake: FakeTestIt, capsys: pytest.CaptureFixture[str]
) -> None:
    path, factory = synced
    path.write_text(path.read_text().replace("priority: Medium", "priority: Low"))
    (item,) = fake.items.values()
    fake.edit_item(item["id"], name="Changed in TMS")
    assert main(["push"], client_factory=factory) == EXIT_ERROR
    out = capsys.readouterr().out
    assert "conflict" in out
    assert "Push is blocked" in out
    assert main(["diff", "--remote"], client_factory=factory) == EXIT_ERROR


def test_diff_before_pull(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["diff"]) == EXIT_ERROR
    assert "run `tms-agent-kit pull`" in capsys.readouterr().err


def test_plan_shows_moves_new_sections_and_totals(
    synced: tuple[Path, ClientFactory], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = synced
    target = path.parent / "Limits" / path.name
    target.parent.mkdir()
    path.replace(target)
    assert main(["diff"]) == EXIT_CHANGES
    out = capsys.readouterr().out
    assert "section   cases/AI/Limits/  (new section 'Demo / AI / Limits')\n" in out
    assert "move      cases/AI/Limits/" in out
    assert "(from 'Demo / AI' to 'Demo / AI / Limits', 1 step)\n" in out
    assert "Total: 1 new section, 1 move\n" in out


def test_check_lists_pending_folders(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workspace / "cases" / "New" / "Deeper").mkdir(parents=True)
    (workspace / "cases" / "New" / "Deeper" / "case.md").write_text("# Case\n")
    (workspace / "cases" / "Empty").mkdir()
    (workspace / "cases" / "_drafts").mkdir()
    assert main(["check"], client_factory=fake_factory()) == EXIT_OK
    out = capsys.readouterr().out
    assert "  cases/Empty/         (not a section; empty)\n" in out
    assert "  cases/New/           (new: push creates this section)\n" in out
    assert "  cases/New/Deeper/    (new: push creates this section)\n" in out
    assert "_drafts" not in out


def test_plan_diff_push_and_list(
    synced: tuple[Path, ClientFactory],
    workspace: Path,
    fake: FakeTestIt,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, factory = synced
    (workspace / "plans").mkdir()
    (workspace / "plans" / "smoke.md").write_text("# Smoke\n\n## Cases\n\n- AI/\n")
    assert main(["plan", "diff"]) == EXIT_CHANGES
    out = capsys.readouterr().out
    assert "create    plans/smoke.md  (1 case x default configuration)\n" in out
    assert "Total: 1 new plan\n" in out

    assert main(["plan", "push"], client_factory=factory) == EXIT_CHANGES
    token = capsys.readouterr().out.rsplit("--confirm ", 1)[1].strip()
    assert main(["plan", "push", "--confirm", "wrong"], client_factory=factory) == EXIT_ERROR
    assert fake.plans == {}
    capsys.readouterr()
    assert main(["plan", "push", "--confirm", token], client_factory=factory) == EXIT_OK
    assert "created   plans/smoke.md  (plan #" in capsys.readouterr().out

    assert main(["plan", "diff"]) == EXIT_OK
    assert "No plan changes." in capsys.readouterr().out
    assert main(["plan", "list"], client_factory=factory) == EXIT_OK
    assert "New        Smoke  <- plans/smoke.md\n" in capsys.readouterr().out


def test_run_new_and_push(
    synced: tuple[Path, ClientFactory],
    workspace: Path,
    fake: FakeTestIt,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, factory = synced
    (workspace / "plans").mkdir()
    (workspace / "plans" / "smoke.md").write_text("# Smoke\n\n## Cases\n\n- AI/\n")
    main(["plan", "push"], client_factory=factory)
    token = capsys.readouterr().out.rsplit("--confirm ", 1)[1].strip()
    main(["plan", "push", "--confirm", token], client_factory=factory)
    capsys.readouterr()

    assert main(["run", "new", "plans/smoke.md"]) == EXIT_OK
    out = capsys.readouterr().out
    run_file = out.split()[1]
    assert run_file.startswith("runs/")
    assert f"run push {run_file}" in out
    path = workspace / run_file
    path.write_text(path.read_text().replace("Result:\n", "Result: passed\n", 1))

    assert main(["run", "push", run_file], client_factory=factory) == EXIT_CHANGES
    out = capsys.readouterr().out
    assert "passed    #" in out
    assert "creates autotest tms-agent-kit-" in out
    assert "Total: 1 passed\n" in out
    token = out.rsplit("--confirm ", 1)[1].strip()
    assert fake.runs == {}
    assert main(["run", "push", run_file, "--confirm", token], client_factory=factory) == EXIT_OK
    out = capsys.readouterr().out
    assert "linked    #" in out
    assert f"created   {run_file}  (run in plan #" in out
    assert len(fake.runs) == 1

    assert main(["run", "push", run_file], client_factory=factory) == EXIT_OK
    assert "Nothing to push." in capsys.readouterr().out
