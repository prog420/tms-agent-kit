from pathlib import Path
from typing import Any

import pytest

from tests.fake_testit import AI_SECTION, CONF_ANY, CONF_CHROME, FakeTestIt
from tms_agent_kit.config import Config
from tms_agent_kit.plan_sync import (
    check_plans_remote,
    describe,
    list_plans,
    plan_changes,
    push_plans,
    snapshot_of,
)
from tms_agent_kit.sync import load_state, pull
from tms_agent_kit.testit import TestItClient
from tms_agent_kit.workspace import Workspace


@pytest.fixture
def cases(
    fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config
) -> dict[str, dict[str, object]]:
    """Three pulled cases: two in AI/Limits, one in AI; plus a shared step in AI/Limits."""
    limits = "30000000-0000-0000-0000-000000000000"
    fake.sections[limits] = {"id": limits, "name": "Limits", "parentId": AI_SECTION}
    items = {
        "balance": fake.add_item(name="Balance", sectionId=limits),
        "quota": fake.add_item(name="Quota", sectionId=limits),
        "error": fake.add_item(name="Error"),
        "shared": fake.add_item(name="Login", sectionId=limits, entityTypeName="SharedSteps"),
    }
    pull(ws, client, config)
    return items


def write_plan(ws: Workspace, text: str, name: str = "regress.md") -> Path:
    path = ws.plans_root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_new_plan_resolves_every_reference_kind(
    cases: dict[str, dict[str, object]], ws: Workspace
) -> None:
    balance = next((ws.root / "AI" / "Limits").glob(f"{cases['balance']['globalId']}-*.md"))
    write_plan(
        ws,
        "# Regress\n\n## Cases\n\n"
        "- AI/Limits/ — all limit cards\n"
        f"- #{cases['error']['globalId']} plain error\n"
        f"- [Balance](../cases/AI/Limits/{balance.name}) duplicate, dropped\n",
    )
    result = plan_changes(ws)
    assert result.errors == []
    (change,) = result.changes
    assert change.kind == "create"
    assert [c.global_id for c in change.cases] == [
        cases["balance"]["globalId"],
        cases["quota"]["globalId"],
        cases["error"]["globalId"],
    ]
    assert describe(change, allow_delete=False) == "3 cases x default configuration"


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ("- 999999", "line 3: case #999999 is not in the local case files"),
        ("- Nowhere/", "line 3: cannot resolve 'Nowhere/'"),
        ("- `AI/Empty/`", "line 3: folder 'AI/Empty/' has no pushed cases"),
        ("- ../tms-agent-kit.toml", "line 3: cannot resolve"),
    ],
)
def test_unresolved_references(
    cases: dict[str, dict[str, object]], ws: Workspace, item: str, message: str
) -> None:
    (ws.root / "AI" / "Empty").mkdir()
    write_plan(ws, f"# Plan\n## Cases\n{item}\n")
    (error,) = plan_changes(ws).errors
    assert message in error.message


def test_shared_step_and_unpushed_case_are_rejected(
    cases: dict[str, dict[str, object]], ws: Workspace
) -> None:
    (ws.root / "AI" / "draft.md").write_text("# Draft\n", encoding="utf-8")
    write_plan(ws, f"# Plan\n## Cases\n- {cases['shared']['globalId']}\n- AI/draft.md\n")
    messages = [e.message for e in plan_changes(ws).errors]
    assert "is a shared step" in messages[0]
    assert "is not in the TMS yet" in messages[1]


def test_update_and_delete_against_snapshots(
    cases: dict[str, dict[str, object]], ws: Workspace
) -> None:
    path = write_plan(ws, "---\nid: p1\nglobal_id: 7\n---\n# Plan\n## Cases\n- AI/Limits/\n")
    # Simulate a previous push of this plan with one case and an older build.
    pushed = plan_changes_snapshot(ws, path)
    ws.save_plan_snapshot("p1", {**pushed, "build": "1.0", "cases": pushed["cases"][:1]})
    ws.save_plan_snapshot("gone", {"name": "Old", "global_id": 5, "cases": []})

    result = plan_changes(ws)
    update, delete = result.changes
    assert update.kind == "update"
    assert update.fields == ["build", "cases"]
    assert (
        describe(update, allow_delete=False)
        == "build, cases +1 -0; 2 cases x default configuration"
    )
    assert delete.kind == "delete"
    assert delete.target == "plan #5 'Old'"
    assert result.without_deletes().changes == [update]

    path.write_text(path.read_text().replace("- AI/Limits/", f"- {cases['error']['globalId']}"))
    ws.save_plan_snapshot("p1", pushed)
    (update, _) = plan_changes(ws).changes
    assert describe(update, allow_delete=False) == "cases +1 -2; 1 case x default configuration"
    (warning,) = plan_changes(ws).warnings
    assert "2 case(s) leave the plan" in warning.message


def plan_changes_snapshot(ws: Workspace, path: Path) -> dict[str, Any]:
    """Snapshot of a plan file as if it had just been pushed."""
    from tms_agent_kit.plan_sync import resolve_cases
    from tms_agent_kit.plans import parse_plan

    plan_file = parse_plan(path.read_text(encoding="utf-8"))
    resolved, problems = resolve_cases(ws, ws.scan(), path, plan_file.plan.refs)
    assert problems == []
    return snapshot_of(plan_file.plan, resolved)


def test_unknown_id_duplicate_names_and_token(
    cases: dict[str, dict[str, object]], ws: Workspace
) -> None:
    write_plan(ws, "---\nid: nope\n---\n# A\n## Cases\n- AI/\n", "a.md")
    write_plan(ws, "# B\n## Cases\n- AI/\n", "b.md")
    write_plan(ws, "# b\n## Cases\n- AI/\n", "c.md")
    result = plan_changes(ws)
    messages = sorted(e.message for e in result.errors)
    assert "plan name is also used in plans/b.md" in messages[0]
    assert "unknown plan id" in messages[1]
    token = result.token()
    assert token == plan_changes(ws).token()
    write_plan(ws, "# B\n\nnew description\n\n## Cases\n- AI/\n", "b.md")
    assert plan_changes(ws).token() != token


# Push


def push_all(ws: Workspace, client: TestItClient, *, allow_delete: bool = False) -> list[str]:
    state = load_state(ws)
    changes = plan_changes(ws)
    changes = changes if allow_delete else changes.without_deletes()
    check_plans_remote(changes, client, state)
    assert not changes.blocked, (changes.errors, [c.conflict for c in changes.changes])
    return [e.kind for e in push_plans(ws, state, client, changes)]


def test_push_creates_plan_suite_and_points(
    cases: dict[str, dict[str, object]], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    path = write_plan(
        ws, "---\nbuild: '1.0'\ntags: [smoke]\n---\n# Regress\n## Cases\n- AI/Limits/\n"
    )
    assert push_all(ws, client) == ["created"]
    assert [w[0] for w in fake.writes] == [
        "POST plan",
        "POST suite",
        "POST suite configurations",
        "POST suite workItems",
    ]
    (plan,) = fake.plans.values()
    assert (plan["name"], plan["build"], plan["tags"]) == ("Regress", "1.0", [{"name": "smoke"}])
    # A later push is not blocked by the lower-cased tags stored in the TMS.
    path.write_text(path.read_text(encoding="utf-8").replace("[smoke]", "[Smoke]"))
    assert push_all(ws, client) == ["pushed"]
    (suite,) = fake.suites.values()
    assert suite["configurations"] == [CONF_ANY]
    assert sorted(suite["workItems"]) == sorted(
        [str(cases["balance"]["id"]), str(cases["quota"]["id"])]
    )
    text = path.read_text(encoding="utf-8")
    assert text.startswith(f"---\nid: {plan['id']}\nglobal_id: {plan['globalId']}\nbuild: '1.0'\n")
    assert plan_changes(ws).changes == []


def test_push_updates_only_what_changed(
    cases: dict[str, dict[str, object]], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    path = write_plan(ws, "# Regress\n## Cases\n- AI/Limits/\n")
    push_all(ws, client)
    fake.writes.clear()

    text = path.read_text(encoding="utf-8").replace("- AI/Limits/", "- AI/")
    path.write_text(text.replace("---\n\n#", "configurations: [Any, chrome]\n---\n\n#"))
    assert push_all(ws, client) == ["pushed"]
    assert [w[0] for w in fake.writes] == ["POST suite configurations", "POST suite workItems"]
    (suite,) = fake.suites.values()
    assert suite["configurations"] == [CONF_ANY, CONF_CHROME]
    assert len(suite["workItems"]) == 3  # the shared step is skipped

    fake.writes.clear()
    path.write_text(path.read_text(encoding="utf-8").replace("# Regress", "# Regress v2"))
    assert push_all(ws, client) == ["pushed"]
    (put,) = fake.writes
    assert put[0] == "PUT plan"
    assert isinstance(put[1], dict)
    assert put[1]["name"] == "Regress v2"
    assert put[1]["hasAutomaticDurationTimer"] is False  # unmanaged fields are sent back
    assert plan_changes(ws).changes == []


def test_remote_edits_and_unknown_configurations_block_the_push(
    cases: dict[str, dict[str, object]], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    path = write_plan(ws, "# Regress\n## Cases\n- AI/Limits/\n")
    push_all(ws, client)
    (plan,) = fake.plans.values()
    plan["build"] = "edited in TMS"
    path.write_text(path.read_text(encoding="utf-8").replace("# Regress", "# Renamed"))
    changes = plan_changes(ws)
    check_plans_remote(changes, client, load_state(ws))
    assert changes.changes[0].conflict == "changed in TMS since the last plan push: build"

    write_plan(ws, "---\nconfigurations: [Safari]\n---\n# Other\n## Cases\n- AI/\n", "other.md")
    changes = plan_changes(ws)
    check_plans_remote(changes, client, load_state(ws))
    assert "unknown configuration(s): Safari; known: Any, Chrome" in changes.errors[0].message


def test_deleted_plan_file_archives_only_with_opt_in(
    cases: dict[str, dict[str, object]], ws: Workspace, fake: FakeTestIt, client: TestItClient
) -> None:
    path = write_plan(ws, "# Regress\n## Cases\n- AI/Limits/\n")
    push_all(ws, client)
    rows = list_plans(ws, load_state(ws), client)
    assert [(p["name"], local) for p, local in rows] == [("Regress", "plans/regress.md")]

    path.unlink()
    fake.writes.clear()
    assert push_all(ws, client) == []
    assert push_all(ws, client, allow_delete=True) == ["archived"]
    assert fake.writes[0][0] == "DELETE plan"
    assert list_plans(ws, load_state(ws), client) == []
    assert plan_changes(ws).changes == []
