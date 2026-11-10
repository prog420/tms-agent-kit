from pathlib import Path

import pytest

from tests.fake_testit import AI_SECTION, ROOT_SECTION, FakeTestIt
from tms_agent_kit.casefile import parse
from tms_agent_kit.config import Config
from tms_agent_kit.sync import (
    Create,
    Delete,
    SyncError,
    Update,
    check_remote,
    load_state,
    plan,
    pull,
    push,
)
from tms_agent_kit.testit import TestItClient
from tms_agent_kit.workspace import Workspace

STEPS = [{"action": "Open the chat.", "expected": "Input is empty."}]


def case_files(ws: Workspace) -> list[str]:
    return sorted(p.relative_to(ws.root).as_posix() for p in ws.root.rglob("*.md"))


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.fixture
def pulled(
    fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config
) -> tuple[dict[str, object], Path]:
    item = fake.add_item(name="Send a message", steps=STEPS, tags=[{"name": "chat"}])
    pull(ws, client, config)
    return item, ws.root / "AI" / f"{item['globalId']}-send-a-message.md"


# Pull


def test_pull_writes_files_and_state(pulled: tuple[dict[str, object], Path], ws: Workspace) -> None:
    item, path = pulled
    assert case_files(ws) == [path.relative_to(ws.root).as_posix()]
    case_file = parse(path.read_text(encoding="utf-8"))
    assert case_file.meta.id == item["id"]
    assert case_file.case.tags == ("chat",)
    state = load_state(ws)
    assert state.sections[ROOT_SECTION].path == ""
    assert state.sections[AI_SECTION].path == "AI"
    assert ws.load_base(str(item["id"])) is not None


def test_second_pull_fetches_nothing(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    fake.requests.clear()
    assert pull(ws, client, config) == []
    paths = [r.url.path for r in fake.requests]
    assert not any("/workItems/" in p and not p.endswith("search") for p in paths)


def test_pull_updates_unchanged_file(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    item, path = pulled
    fake.edit_item(str(item["id"]), priority="High")
    events = pull(ws, client, config)
    assert [e.kind for e in events] == ["updated"]
    assert parse(path.read_text(encoding="utf-8")).case.priority == "High"


def test_pull_keeps_local_edits_and_reports_conflict(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    item, path = pulled
    edit(path, "priority: Medium", "priority: Low")
    fake.edit_item(str(item["id"]), priority="High")
    events = pull(ws, client, config)
    assert [e.kind for e in events] == ["conflict"]
    assert "priority: Low" in path.read_text(encoding="utf-8")
    base = ws.load_base(str(item["id"]))
    assert base is not None
    assert base["priority"] == "Medium"  # the base stays at the version the edit started from


def test_pull_keeps_local_edits_when_tms_is_unchanged(
    pulled: tuple[dict[str, object], Path], ws: Workspace, client: TestItClient, config: Config
) -> None:
    _, path = pulled
    edit(path, "priority: Medium", "priority: Low")
    assert pull(ws, client, config) == []
    assert "priority: Low" in path.read_text(encoding="utf-8")


def test_pull_force_discards_local_edits(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    item, path = pulled
    edit(path, "priority: Medium", "priority: Low")
    fake.edit_item(str(item["id"]), priority="High")
    pull(ws, client, config, force=True)
    assert "priority: High" in path.read_text(encoding="utf-8")


def test_pull_moves_file_when_section_changes(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    item, _ = pulled
    fake.edit_item(str(item["id"]), sectionId=ROOT_SECTION, name="Renamed")
    events = pull(ws, client, config)
    assert [e.kind for e in events] == ["moved", "updated"]
    assert case_files(ws) == [f"{item['globalId']}-renamed.md"]
    assert list((ws.root / "AI").iterdir()) == []  # the section still exists


def test_pull_renames_file_without_update_event(
    pulled: tuple[dict[str, object], Path],
    ws: Workspace,
    client: TestItClient,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, path = pulled
    text = path.read_text(encoding="utf-8")
    monkeypatch.setattr("tms_agent_kit.workspace.slugify", lambda name: "new-slug")
    events = pull(ws, client, config)
    assert [e.kind for e in events] == ["moved"]
    assert (path.parent / f"{path.name.split('-')[0]}-new-slug.md").read_text(
        encoding="utf-8"
    ) == text


def test_pull_creates_folders_for_empty_sections(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    new_id = "30000000-0000-0000-0000-000000000000"
    fake.sections[new_id] = {"id": new_id, "name": "Limits", "parentId": AI_SECTION}
    pull(ws, client, config)
    assert (ws.root / "AI" / "Limits").is_dir()

    del fake.sections[new_id]
    pull(ws, client, config)
    assert not (ws.root / "AI" / "Limits").exists()
    assert (ws.root / "AI").is_dir()


def test_pull_archives_cases_deleted_in_tms(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    item, path = pulled
    fake.items[str(item["id"])]["isDeleted"] = True
    events = pull(ws, client, config)
    assert [e.kind for e in events] == ["archived"]
    assert not path.exists()
    assert (ws.state_dir / "archived" / path.name).exists()
    assert ws.load_base(str(item["id"])) is None


def test_pull_never_overwrites_another_file(
    fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config
) -> None:
    item = fake.add_item(name="Case")
    taken = ws.root / "AI" / f"{item['globalId']}-case.md"
    taken.parent.mkdir(parents=True)
    taken.write_text("my notes", encoding="utf-8")
    events = pull(ws, client, config, force=True)
    assert [e.kind for e in events] == ["error", "conflict"]
    assert taken.read_text(encoding="utf-8") == "my notes"


def test_pull_marks_lossy_cases(
    fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config
) -> None:
    fake.add_item(name="Case", description="```\nunclosed")
    events = pull(ws, client, config)
    assert "lossy" in [e.kind for e in events]


def test_pull_rejects_another_project(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    fake.project["id"] = "other"
    with pytest.raises(SyncError, match="use a separate root"):
        pull(ws, client, config)


# Plan


def test_plan_without_pull(ws: Workspace) -> None:
    with pytest.raises(SyncError, match="run `tms-agent-kit pull`"):
        load_state(ws)


def test_plan_no_changes(pulled: tuple[dict[str, object], Path], ws: Workspace) -> None:
    result = plan(ws, load_state(ws))
    assert result.changes == []
    assert result.errors == []


def test_plan_update(pulled: tuple[dict[str, object], Path], ws: Workspace) -> None:
    _, path = pulled
    edit(path, "Input is empty.", "Input is focused.")
    edit(path, "tags: [chat]", "tags: [chat, smoke]")
    result = plan(ws, load_state(ws))
    (change,) = result.changes
    assert isinstance(change, Update)
    assert change.fields == ["tags", "steps"]
    assert "+Input is focused." in change.diff


def test_plan_move_to_another_section(
    pulled: tuple[dict[str, object], Path], ws: Workspace
) -> None:
    _, path = pulled
    path.rename(ws.root / path.name)
    (change,) = plan(ws, load_state(ws)).changes
    assert isinstance(change, Update)
    assert change.fields == ["section"]
    assert change.section_id == ROOT_SECTION


def test_plan_create_and_delete(pulled: tuple[dict[str, object], Path], ws: Workspace) -> None:
    _, path = pulled
    path.unlink()
    (ws.root / "AI" / "new.md").write_text(
        "---\nduration_ms: 1000\n---\n\n# New\n", encoding="utf-8"
    )
    result = plan(ws, load_state(ws))
    assert sorted(type(c).__name__ for c in result.changes) == ["Create", "Delete"]


def test_plan_errors(pulled: tuple[dict[str, object], Path], ws: Workspace) -> None:
    _, path = pulled
    (ws.root / "AI" / "Bad.").mkdir()
    (ws.root / "AI" / "Bad." / "a.md").write_text("---\n---\n\n# A\n", encoding="utf-8")
    (ws.root / "AI" / "broken.md").write_text("no frontmatter", encoding="utf-8")
    (ws.root / "AI" / "zero.md").write_text("---\n---\n\n# Zero\n", encoding="utf-8")
    (ws.root / "AI" / "_notes.md").write_text("ignored", encoding="utf-8")
    edit(path, "type: TestCases", "type: CheckLists")
    messages = sorted(e.message for e in plan(ws, load_state(ws)).errors)
    assert len(messages) == 4
    assert "duration_ms must be greater than 0" in messages[0]
    assert "folder 'Bad.' cannot become a section; rename it to 'Bad'" in messages[1]
    assert "line 1: text before '# <name>'" in messages[2]
    assert "type cannot be changed" in messages[3]


def test_plan_new_file_without_frontmatter(
    pulled: tuple[dict[str, object], Path], ws: Workspace
) -> None:
    item, path = pulled
    # Parsed as a new case; Test IT still needs a duration for TestCases.
    draft = ws.root / "AI" / "draft.md"
    draft.write_text("# Draft\n", encoding="utf-8")
    (error,) = plan(ws, load_state(ws)).errors
    assert "add it to the frontmatter" in error.message
    draft.write_text("---\nduration_ms: 1000\n---\n# Draft\n", encoding="utf-8")
    (create,) = plan(ws, load_state(ws)).changes
    assert create.kind == "create"

    # A pulled file with its frontmatter stripped would be pushed as a duplicate.
    body = path.read_text(encoding="utf-8").split("---\n", 2)[2]
    path.write_text(body, encoding="utf-8")
    result = plan(ws, load_state(ws))
    (error,) = result.errors
    assert f"matches case #{item['globalId']}" in error.message
    assert [c.kind for c in result.changes] == ["create", "delete"]


def test_plan_duplicate_ids(pulled: tuple[dict[str, object], Path], ws: Workspace) -> None:
    _, path = pulled
    (ws.root / "copy.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (error,) = plan(ws, load_state(ws)).errors
    assert "duplicate id" in error.message


def test_plan_warns_on_read_only_edits(
    pulled: tuple[dict[str, object], Path], ws: Workspace
) -> None:
    _, path = pulled
    edit(path, "version_number: 1", "version_number: 7")
    result = plan(ws, load_state(ws))
    assert result.changes == []
    assert [w.message for w in result.warnings] == [
        "read-only field version_number was edited; ignored"
    ]


def test_untouched_lossy_case_is_not_a_change(
    fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config
) -> None:
    fake.add_item(name="Case", description="```\ntext")
    pull(ws, client, config)
    assert plan(ws, load_state(ws)).changes == []


def test_lossy_field_edit_is_blocked_but_others_are_allowed(
    fake: FakeTestIt, ws: Workspace, client: TestItClient, config: Config
) -> None:
    item = fake.add_item(name="Case", description="```\ntext", priority="Low")
    pull(ws, client, config)
    (path,) = ws.root.rglob("*.md")
    edit(path, "priority: Low", "priority: High")
    (change,) = plan(ws, load_state(ws)).changes
    assert isinstance(change, Update)
    assert change.fields == ["priority"]

    push(ws, load_state(ws), client, plan(ws, load_state(ws)))
    assert fake.items[str(item["id"])]["description"] == "```\ntext"  # untouched

    path = next(ws.root.rglob("*.md"))
    edit(path, "text", "changed")
    (error,) = plan(ws, load_state(ws)).errors
    assert "description cannot be represented exactly" in error.message


def test_check_remote_marks_conflicts(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace, client: TestItClient
) -> None:
    item, path = pulled
    edit(path, "priority: Medium", "priority: Low")
    fake.edit_item(str(item["id"]), name="Changed elsewhere")
    result = plan(ws, load_state(ws))
    check_remote(result, client)
    assert result.blocked
    assert "version 1 -> 2" in str(result.changes[0].conflict)


# Push


def test_push_update_sends_only_changed_fields(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace, client: TestItClient
) -> None:
    item, path = pulled
    fake.items[str(item["id"])]["links"] = [{"url": "https://x", "title": "kept"}]
    fake.items[str(item["id"])]["description"] = "  kept as is  "
    edit(path, "Input is empty.", "Input is focused.")
    result = plan(ws, load_state(ws))
    events = push(ws, load_state(ws), client, result)
    assert [e.kind for e in events] == ["pushed", "updated"]
    (method, body) = fake.writes[-1]
    assert method == "PUT"
    assert isinstance(body, dict)
    assert body["links"] == [{"url": "https://x", "title": "kept"}]
    assert body["description"] == "  kept as is  "
    assert body["steps"][0]["action"] == "Open the chat."
    assert body["steps"][0]["expected"] == "Input is focused."
    assert body["steps"][0]["id"] == fake.items[str(item["id"])]["steps"][0]["id"]
    assert plan(ws, load_state(ws)).changes == []
    assert "version_number: 2" in path.read_text(encoding="utf-8")


def test_push_new_step_has_no_id(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace, client: TestItClient
) -> None:
    _, path = pulled
    path.write_text(
        path.read_text(encoding="utf-8") + "\n### Step 2\n\n#### Action\n\nReload.\n",
        encoding="utf-8",
    )
    push(ws, load_state(ws), client, plan(ws, load_state(ws)))
    _, body = fake.writes[-1]
    assert isinstance(body, dict)
    assert "id" in body["steps"][0]
    assert "id" not in body["steps"][1]


def test_push_create_renames_file(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace, client: TestItClient
) -> None:
    new = ws.root / "AI" / "draft.md"
    new.write_text("---\nduration_ms: 1000\ntags: [x]\n---\n\n# Brand new\n", encoding="utf-8")
    result = plan(ws, load_state(ws))
    assert isinstance(result.changes[0], Create)
    events = push(ws, load_state(ws), client, result)
    assert [e.kind for e in events] == ["created", "created"]
    method, body = fake.writes[-1]
    assert method == "POST"
    assert isinstance(body, dict)
    assert body["sectionId"] == AI_SECTION
    assert body["tags"] == [{"name": "x"}]
    assert not new.exists()
    assert len(case_files(ws)) == 2
    assert plan(ws, load_state(ws)).changes == []


def test_push_skips_deletes_without_opt_in(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace, client: TestItClient
) -> None:
    _, path = pulled
    path.unlink()
    result = plan(ws, load_state(ws))
    assert isinstance(result.changes[0], Delete)
    assert push(ws, load_state(ws), client, result.without_deletes()) == []
    assert fake.writes == []


def test_push_delete_archives(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace, client: TestItClient
) -> None:
    item, path = pulled
    path.unlink()
    events = push(ws, load_state(ws), client, plan(ws, load_state(ws)))
    assert [e.kind for e in events] == ["archived"]
    assert fake.writes == [("DELETE", item["id"])]
    assert ws.load_base(str(item["id"])) is None


def test_push_rechecks_version_before_writing(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace, client: TestItClient
) -> None:
    item, path = pulled
    edit(path, "priority: Medium", "priority: Low")
    result = plan(ws, load_state(ws))
    check_remote(result, client)
    fake.edit_item(str(item["id"]), name="Changed meanwhile")
    events = push(ws, load_state(ws), client, result)
    assert [e.kind for e in events] == ["conflict"]
    assert fake.writes == []


def test_push_refuses_blocked_plan(
    pulled: tuple[dict[str, object], Path], ws: Workspace, client: TestItClient
) -> None:
    (ws.root / "AI" / "broken.md").write_text("broken", encoding="utf-8")
    with pytest.raises(SyncError, match="errors or conflicts"):
        push(ws, load_state(ws), client, plan(ws, load_state(ws)))


def test_push_reports_tms_errors(
    pulled: tuple[dict[str, object], Path], ws: Workspace, client: TestItClient
) -> None:
    _, path = pulled
    edit(path, "# Send a message", "# reject")
    events = push(ws, load_state(ws), client, plan(ws, load_state(ws)))
    assert [e.kind for e in events] == ["failed"]
    assert "HTTP 400" in events[0].message
    assert plan(ws, load_state(ws)).changes  # still pending


def test_token_changes_with_content(pulled: tuple[dict[str, object], Path], ws: Workspace) -> None:
    _, path = pulled
    edit(path, "priority: Medium", "priority: Low")
    first = plan(ws, load_state(ws)).token()
    assert plan(ws, load_state(ws)).token() == first
    edit(path, "priority: Low", "priority: High")
    assert plan(ws, load_state(ws)).token() != first


def test_push_creates_sections_for_new_folders(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
) -> None:
    item, path = pulled
    target = ws.root / "AI" / "Limits" / "Balance" / path.name
    target.parent.mkdir(parents=True)
    path.replace(target)
    (ws.root / "AI" / "Empty").mkdir()  # no cases: not planned
    state = load_state(ws)
    result = plan(ws, state)
    assert [(c.kind, c.target) for c in result.changes] == [
        ("section", "cases/AI/Limits/"),
        ("section", "cases/AI/Limits/Balance/"),
        ("update", "cases/AI/Limits/Balance/" + path.name),
    ]
    update = result.changes[2]
    assert isinstance(update, Update)
    assert update.fields == ["section"]
    assert update.section == "Demo / AI / Limits / Balance"
    assert update.from_section == "Demo / AI"
    assert result.warnings == []  # AI keeps a subsection, so it is not reported as empty

    events = push(ws, state, client, result)
    assert [e.kind for e in events] == ["created", "created", "pushed", "updated"]
    assert [w[0] for w in fake.writes] == ["POST section", "POST section", "PUT"]
    limits, balance = (w[1] for w in fake.writes[:2])
    assert isinstance(limits, dict)
    assert isinstance(balance, dict)
    assert (limits["name"], limits["parentId"]) == ("Limits", AI_SECTION)
    assert balance["name"] == "Balance"
    moved = fake.items[str(item["id"])]["sectionId"]
    assert fake.sections[moved]["name"] == "Balance"
    assert target.is_file()
    assert plan(ws, load_state(ws)).changes == []


def test_plan_rejects_files_in_root_without_section(
    pulled: tuple[dict[str, object], Path], ws: Workspace
) -> None:
    state = load_state(ws)
    state.sections.pop(ROOT_SECTION)
    (ws.root / "new.md").write_text("---\nduration_ms: 1\n---\n# New\n", encoding="utf-8")
    (error,) = plan(ws, state).errors
    assert "the case root is not a TMS section" in error.message


def test_plan_warns_about_emptied_section(
    pulled: tuple[dict[str, object], Path], fake: FakeTestIt, ws: Workspace
) -> None:
    _, path = pulled
    path.replace(ws.root / path.name)  # AI -> root section
    (warning,) = plan(ws, load_state(ws)).warnings
    assert warning.target == "cases/AI/"
    assert "section 'Demo / AI' will have no cases" in warning.message


def test_plan_does_not_warn_when_section_keeps_cases(
    pulled: tuple[dict[str, object], Path],
    fake: FakeTestIt,
    ws: Workspace,
    client: TestItClient,
    config: Config,
) -> None:
    _, path = pulled
    fake.add_item(name="Second")
    pull(ws, client, config)
    path.replace(ws.root / path.name)
    assert plan(ws, load_state(ws)).warnings == []
