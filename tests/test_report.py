from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fake_testit import FakeTestIt
from tms_agent_kit.casefile import render
from tms_agent_kit.cli import EXIT_OK, main
from tms_agent_kit.config import Config
from tms_agent_kit.model import Case, CaseFile, CaseMeta
from tms_agent_kit.plan_sync import check_plans_remote, plan_changes, push_plans
from tms_agent_kit.report import NOT_RUN, build_report, render_html
from tms_agent_kit.run_sync import check_run_remote, new_run, plan_run, push_run
from tms_agent_kit.sync import load_state, pull
from tms_agent_kit.testit import TestItClient
from tms_agent_kit.workspace import Workspace

Json = dict[str, Any]
STEPS = [{"action": "Open chat", "expected": "Chat opens"}, {"action": "Send", "expected": "Ok"}]


@pytest.fixture
def items(fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config) -> list[Json]:
    """Three pulled cases in a pushed plan, a pushed run and a local run."""
    cases = [fake.add_item(name=name, steps=STEPS) for name in ("Balance", "Quota", "Error")]
    pull(ws, client, config)
    ws.plans_root.mkdir()
    (ws.plans_root / "regress.md").write_text("# Regress\n## Cases\n- AI/\n", encoding="utf-8")
    changes = plan_changes(ws)
    check_plans_remote(changes, client, load_state(ws))
    push_plans(ws, load_state(ws), client, changes)

    balance, quota = cases[0]["globalId"], cases[1]["globalId"]
    first = new_run(ws, ws.plans_root / "regress.md", None)
    (first.parent / "shots").mkdir()
    (first.parent / "shots" / "quota.png").write_bytes(b"png")
    fill(
        first,
        {
            balance: "Result: passed",
            quota: "Result: failed\n\nNo card ![card](shots/quota.png)\n\n- Step 2: failed, empty",
        },
    )
    run_plan = plan_run(ws, first)
    check_run_remote(run_plan, client, load_state(ws), ws)
    push_run(ws, load_state(ws), client, run_plan)

    second = new_run(ws, ws.plans_root / "regress.md", None)  # a later run, not pushed
    fill(second, {quota: "Result: passed"})
    return cases


def fill(path: Path, results: dict[int, str]) -> None:
    text = path.read_text(encoding="utf-8")
    for global_id, block in results.items():
        heading = next(line for line in text.split("\n") if line.startswith(f"## {global_id} "))
        text = text.replace(f"{heading}\n\nResult:\n", f"{heading}\n\n{block}\n")
    path.write_text(text, encoding="utf-8")


def test_report_collects_cases_plans_and_runs(
    items: list[Json], ws: Workspace, config: Config
) -> None:
    balance, quota, error = (item["globalId"] for item in items)
    # A local edit, a new case and a broken plan file.
    quota_file = next(p for p in ws.root.rglob("*.md") if p.name.startswith(f"{quota}-"))
    quota_file.write_text(quota_file.read_text().replace("# Quota", "# Quota <b>v2</b>"))
    draft = CaseFile(CaseMeta(), Case(name="Draft", duration_ms=60000))
    (quota_file.parent / "draft.md").write_text(render(draft))
    (ws.plans_root / "broken.md").write_text("no heading\n")

    report = build_report(ws, config)
    by_id = {c.global_id: c for c in report.cases}
    assert [c.status for c in report.cases if c.global_id is None] == ["new"]
    assert (by_id[quota].status, by_id[balance].status) == ("modified", "pushed")
    # The latest result wins: the second run passed the quota case.
    assert [by_id[g].last_outcome for g in (balance, quota, error)] == ["Passed", "Passed", NOT_RUN]
    assert [r.outcome for _, r in by_id[quota].results] == ["Failed", "Passed"]

    broken, regress = report.plans  # sorted by file name
    assert (regress.name, regress.status, len(regress.cases), len(regress.runs)) == (
        "Regress",
        "pushed",
        3,
        2,
    )
    assert broken.status == "error"
    assert [p.target for p in report.problems] == ["plans/broken.md"]
    assert [r.status for r in report.runs] == ["pushed", "local"]
    assert report.runs[0].totals == {"Passed": 1, "Failed": 1, NOT_RUN: 1}

    html = render_html(report, ws.base_dir, datetime(2026, 1, 2, 3, 4))
    project = f"{config.base_url}/projects/42"
    assert f'href="{project}/tests/{balance}"' in html
    assert f'href="{project}/test-plans/{regress.global_id}/plan"' in html
    assert f"{project}/test-plans/{regress.global_id}/test-runs/" in html
    assert 'src="runs/shots/quota.png"' in html
    assert "Quota &lt;b&gt;v2&lt;/b&gt;" in html
    assert "<b>v2</b>" not in html
    assert "report 2026-01-02 03:04" in html


def test_runs_are_ordered_by_counter(items: list[Json], ws: Workspace, config: Config) -> None:
    names = [r.path.name for r in build_report(ws, config).runs]
    assert names[1] == names[0].removesuffix(".md") + "-2.md"


def test_cli_report(
    items: list[Json],
    ws: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(ws.base_dir)
    assert main(["report"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "written   report.html\n" in out
    assert "3 cases (2 passed, 1 not run), 1 plans, 2 runs" in out
    assert (ws.base_dir / "report.html").read_text().startswith("<!doctype html>")

    assert main(["report", "-o", "out/r.html"]) == EXIT_OK
    assert (ws.base_dir / "out" / "r.html").is_file()
