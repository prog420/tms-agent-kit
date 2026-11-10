from pathlib import Path
from typing import Any

import pytest

from tests.fake_testit import CONF_ANY, CONF_CHROME, FakeTestIt
from tms_agent_kit.config import Config
from tms_agent_kit.plan_sync import check_plans_remote, plan_changes, push_plans
from tms_agent_kit.run_sync import (
    RunPlan,
    check_run_remote,
    describe_run,
    new_run,
    outcome_summary,
    plan_run,
    push_run,
)
from tms_agent_kit.runs import parse_run
from tms_agent_kit.sync import SyncError, load_state, pull
from tms_agent_kit.testit import TestItClient
from tms_agent_kit.workspace import Workspace

Json = dict[str, Any]

STEPS = [{"action": "Open chat", "expected": "Chat opens"}, {"action": "Send", "expected": "Ok"}]


@pytest.fixture
def items(fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config) -> list[Json]:
    """Three pulled cases with two steps each, in a pushed plan."""
    cases = [fake.add_item(name=name, steps=STEPS) for name in ("Balance", "Quota", "Error")]
    pull(ws, client, config)
    ws.plans_root.mkdir()
    (ws.plans_root / "regress.md").write_text("# Regress\n## Cases\n- AI/\n", encoding="utf-8")
    changes = plan_changes(ws)
    check_plans_remote(changes, client, load_state(ws))
    push_plans(ws, load_state(ws), client, changes)
    fake.writes.clear()
    return cases


def fill(path: Path, results: dict[int, str]) -> None:
    """Put a block of result lines under each listed case heading."""
    text = path.read_text(encoding="utf-8")
    for global_id, block in results.items():
        heading = next(line for line in text.split("\n") if line.startswith(f"## {global_id} "))
        text = text.replace(f"{heading}\n\nResult:\n", f"{heading}\n\n{block}\n")
    path.write_text(text, encoding="utf-8")


def checked(ws: Workspace, client: TestItClient, path: Path) -> RunPlan:
    run_plan = plan_run(ws, path)
    if not run_plan.blocked:
        check_run_remote(run_plan, client, load_state(ws), ws)
    return run_plan


def test_new_run_writes_a_template_per_plan(items: list[Json], ws: Workspace) -> None:
    first = new_run(ws, ws.plans_root / "regress.md", None)
    second = new_run(ws, ws.plans_root / "regress.md", "Chrome")
    assert first.parent == ws.runs_root
    assert second.name == first.name.replace(".md", "-2.md")
    text = second.read_text(encoding="utf-8")
    assert f"![screenshot]({second.stem}/<case id>-step-2.png)" in text
    run = parse_run(text)
    assert (run.plan, run.configuration) == ("plans/regress.md", "Chrome")
    assert [r.global_id for r in run.results] == [i["globalId"] for i in items]
    assert run.executed == []


def test_push_links_autotests_creates_the_run_and_uploads_results(
    items: list[Json], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    balance, quota, _ = items
    path = new_run(ws, ws.plans_root / "regress.md", None)
    fill(
        path,
        {
            balance["globalId"]: "Result: failed\n\nCard is empty.\n\n- Step 2: failed, no card",
            quota["globalId"]: "Result: passed",
        },
    )
    run_plan = checked(ws, client, path)
    assert not run_plan.blocked
    assert [c.autotest for c in run_plan.cases] == ["create", "create"]
    assert outcome_summary(run_plan) == "1 failed, 1 passed, 1 not executed"
    lines = describe_run(run_plan)
    plan_global_id = fake_plan(fake)["globalId"]
    assert lines[0].message == f"new run in plan #{plan_global_id}, configuration 'default'"
    assert lines[1].message == (
        f"1 step result; creates autotest tms-agent-kit-{balance['globalId']}; "
        "the case becomes automated"
    )
    assert fake.writes == []  # dry-run

    events = push_run(ws, load_state(ws), client, run_plan)
    assert [w[0] for w in fake.writes] == [
        "POST autotest",
        "POST autotest",
        "POST run",
        "POST results",
    ]
    assert [e.kind for e in events] == ["linked", "updated", "linked", "updated", "created"]
    run_body = fake.writes[2][1]
    assert run_body["testPointSelectors"] == [
        {"configurationId": CONF_ANY, "workItemIds": [balance["id"], quota["id"]]}
    ]
    failed = fake.writes[3][1][0]
    assert failed["autoTestExternalId"] == f"tms-agent-kit-{balance['globalId']}"
    assert (failed["outcome"], failed["message"]) == ("Failed", "Card is empty.")
    assert failed["stepResults"] == [
        {"title": "Step 2", "outcome": "Failed", "info": "no card", "attachments": []}
    ]
    assert fake_plan(fake)["status"] == "InProgress"

    # The case files now show the cases as automated and carry the new version.
    balance_file = next(ws.root.rglob(f"{balance['globalId']}-*.md")).read_text(encoding="utf-8")
    assert "automated: true" in balance_file
    run_id = parse_run(path.read_text(encoding="utf-8")).id
    assert run_id in fake.runs
    # Nothing left to push; the plan and the cases have no local changes.
    again = checked(ws, client, path)
    assert again.cases == []
    assert not again.blocked
    assert plan_changes(ws).changes == []


def fake_plan(fake: FakeTestIt) -> Json:
    (plan,) = fake.plans.values()
    return plan


def push(ws: Workspace, client: TestItClient, path: Path) -> list[str]:
    run_plan = checked(ws, client, path)
    assert not run_plan.blocked, run_plan.errors + run_plan.conflicts
    return [e.kind for e in push_run(ws, load_state(ws), client, run_plan)]


def test_repush_updates_changed_results_only(
    items: list[Json], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    balance, quota, error = items
    path = new_run(ws, ws.plans_root / "regress.md", None)
    fill(path, {balance["globalId"]: "Result: failed", quota["globalId"]: "Result: passed"})
    push(ws, client, path)
    fake.writes.clear()

    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("Result: failed", "Result: passed\n\nFixed by retry."))
    assert push(ws, client, path) == ["updated"]
    ((kind, body),) = fake.writes
    assert (kind, body) == ("PUT result", {"outcome": "Passed", "comment": "Fixed by retry."})

    # A result changed in the TMS blocks the update.
    (result,) = (r for r in fake.results.values() if r["outcome"] == "Passed" and r.get("comment"))
    result["outcome"] = "Blocked"
    path.write_text(path.read_text(encoding="utf-8").replace("Fixed by retry.", "Retry 2."))
    (conflict,) = checked(ws, client, path).conflicts
    assert conflict.message == "result changed in TMS to Blocked"

    # Results that cannot be updated in place.
    fill(path, {error["globalId"]: "Result: passed"})
    text = path.read_text(encoding="utf-8").replace("Result: passed\n\n## ", "Result:\n\n## ", 1)
    path.write_text(text)
    messages = sorted(e.message for e in plan_run(ws, path).errors)
    assert messages == [
        f"case #{quota['globalId']} was pushed; its result cannot be removed",
        "the case is not in the pushed run; start a new run",
    ]


def test_step_results_are_fixed_after_push(
    items: list[Json], ws: Workspace, client: TestItClient
) -> None:
    path = new_run(ws, ws.plans_root / "regress.md", None)
    fill(path, {items[0]["globalId"]: "Result: failed\n- Step 1: failed"})
    push(ws, client, path)
    path.write_text(path.read_text(encoding="utf-8").replace("Step 1", "Step 2"))
    (error,) = plan_run(ws, path).errors
    assert error.message == "step results cannot change after push; edit the comment"


def test_local_errors(items: list[Json], ws: Workspace, client: TestItClient) -> None:
    path = new_run(ws, ws.plans_root / "regress.md", None)
    fill(path, {items[0]["globalId"]: "Result: failed\n- Step 3: failed"})
    path.write_text(path.read_text(encoding="utf-8") + "\n## 999 Unknown\n\nResult: passed\n")
    messages = [e.message for e in plan_run(ws, path).errors]
    assert messages == ["the case has 2 steps, not 3", "case #999 is not in the plan"]

    plan = ws.plans_root / "regress.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("# Regress", "# Renamed"))
    with pytest.raises(SyncError, match="has local changes; run `plan push` first"):
        plan_run(ws, path)


def test_remote_checks(
    items: list[Json], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    balance, quota, error = items
    # Quota: an autotest from a previous workspace exists but is not linked.
    fake.autotests["a1"] = {
        "id": "a1",
        "projectId": fake.project["id"],
        "externalId": f"tms-agent-kit-{quota['globalId']}",
        "name": "Quota",
        "namespace": "tms-agent-kit",
        "steps": [{"title": "Step 1"}],
    }
    # Error: linked to a real autotest.
    fake.items[error["id"]]["autoTests"] = [{"id": "a2", "externalId": "ui.ErrorTest"}]
    path = new_run(ws, ws.plans_root / "regress.md", None)
    fill(path, {g["globalId"]: "Result: passed" for g in items})
    fake.edit_item(balance["id"], name="Balance v2")

    run_plan = checked(ws, client, path)
    assert [e.message for e in run_plan.conflicts] == [
        "changed in TMS since the last pull; run pull"
    ]
    assert [e.message for e in run_plan.errors] == ["linked to other autotests: ui.ErrorTest"]
    assert [c.autotest for c in run_plan.cases] == ["ready", "link", "ready"]

    chrome = new_run(ws, ws.plans_root / "regress.md", "Chrome")
    fill(chrome, {balance["globalId"]: "Result: passed"})
    (error_event,) = checked(ws, client, chrome).errors
    assert error_event.message == "not in the TMS plan for this configuration"
    assert CONF_CHROME not in next(iter(fake.suites.values()))["configurations"]


def test_existing_autotest_is_linked_and_its_steps_updated(
    items: list[Json], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    quota = items[1]
    fake.autotests["a1"] = {
        "id": "a1",
        "projectId": fake.project["id"],
        "externalId": f"tms-agent-kit-{quota['globalId']}",
        "name": "Quota",
        "namespace": "tms-agent-kit",
        "steps": [{"title": "Step 1"}],
    }
    path = new_run(ws, ws.plans_root / "regress.md", None)
    fill(path, {quota["globalId"]: "Result: blocked\n\nNo stand."})
    assert push(ws, client, path) == ["linked", "updated", "created"]
    assert [w[0] for w in fake.writes] == [
        "POST autotest link",
        "PUT autotest",
        "POST run",
        "POST results",
    ]
    assert fake.autotests["a1"]["steps"] == [{"title": "Step 1"}, {"title": "Step 2"}]
    assert fake.items[quota["id"]]["isAutomated"] is True


def test_attachments_are_uploaded_with_the_results(
    items: list[Json], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    balance = items[0]
    path = new_run(ws, ws.plans_root / "regress.md", None)
    shots = path.parent / "shots"
    shots.mkdir()
    (shots / "page.png").write_bytes(b"x" * 10)
    (shots / "step.png").write_bytes(b"y" * 5)
    fill(
        path,
        {
            balance["globalId"]: "Result: failed\n\nNo card ![page](shots/page.png)\n\n"
            "- Step 2: failed, empty ![step](shots/step.png)"
        },
    )
    run_plan = checked(ws, client, path)
    assert run_plan.errors == []
    lines = describe_run(run_plan)
    assert "2 attachments" in lines[1].message
    assert (lines[-1].kind, lines[-1].message) == (
        "upload",
        "2 files, 15 B; each file is uploaded once",
    )

    push_run(ws, load_state(ws), client, run_plan)
    kinds = [w[0] for w in fake.writes]
    assert kinds == [
        "POST autotest",
        "POST attachment",
        "POST attachment",
        "POST run",
        "POST results",
    ]
    ids = {a["name"]: a["id"] for a in fake.attachments.values()}
    (body,) = fake.writes[4][1]
    assert body["message"] == "No card"
    assert body["attachments"] == [{"id": ids["page.png"]}]
    assert body["stepResults"][0]["attachments"] == [{"id": ids["step.png"]}]

    # A file linked from the result and from a step is uploaded once.
    other = new_run(ws, ws.plans_root / "regress.md", None)
    both = "Result: failed\n![p](shots/page.png)\n- Step 1: failed ![p](shots/page.png)"
    fill(other, {balance["globalId"]: both})
    fake.writes.clear()
    push(ws, client, other)
    assert [w[0] for w in fake.writes].count("POST attachment") == 1
    (body,) = fake.writes[-1][1]
    assert body["attachments"] == body["stepResults"][0]["attachments"]

    # Adding a file later attaches it to the pushed result; nothing else is sent.
    fake.writes.clear()
    (shots / "log.txt").write_text("log")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "No card ![page](shots/page.png)",
            "No card ![page](shots/page.png)\n[log](shots/log.txt)",
        )
    )
    assert push(ws, client, path) == ["updated"]
    assert [w[0] for w in fake.writes] == ["POST attachment", "PUT result attachment"]
    assert checked(ws, client, path).cases == []

    # Pushed files cannot be removed, and step attachments are fixed.
    path.write_text(path.read_text(encoding="utf-8").replace("[log](shots/log.txt)", ""))
    (error,) = plan_run(ws, path).errors
    assert error.message == "attachment runs/shots/log.txt was pushed; it cannot be removed"
    path.write_text(text.replace("![step](shots/step.png)", ""))
    assert "step results cannot change" in plan_run(ws, path).errors[0].message


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("[x](../../outside.txt)", "attachment '../../outside.txt' is outside the workspace"),
        ("[env](../.env)", "attachment '../.env' is a hidden file or in a hidden folder"),
        ("[s](.tms/state.json)", "attachment '.tms/state.json' is a hidden file or in a hidden"),
        ("![s](missing.png)", "attachment 'missing.png' not found"),
    ],
)
def test_attachment_paths_are_checked(
    items: list[Json], ws: Workspace, link: str, message: str
) -> None:
    (ws.base_dir / ".env").write_text("TESTIT_TOKEN=secret")
    path = new_run(ws, ws.plans_root / "regress.md", None)
    fill(path, {items[0]["globalId"]: f"Result: passed\n\n{link}"})
    (error,) = plan_run(ws, path).errors
    assert message in error.message
