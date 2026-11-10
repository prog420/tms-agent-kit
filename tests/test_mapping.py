from typing import Any

import pytest

from tms_agent_kit.model import Case, Step
from tms_agent_kit.testit.mapping import (
    ProjectContext,
    changed_fields,
    create_body,
    to_case,
    to_meta,
    update_body,
    validate,
)

CTX = ProjectContext(
    project_id="p",
    attribute_names={"attr-1": "Component"},
    shared_step_global_ids={"shared-uuid": 1003},
)

RAW: dict[str, Any] = {
    "id": "w",
    "globalId": 7,
    "versionId": "v2",
    "versionNumber": 2,
    "createdDate": "2026-01-01T00:00:00Z",
    "modifiedDate": None,
    "isAutomated": True,
    "sectionId": "s",
    "name": " Name ",
    "entityTypeName": "TestCases",
    "state": "Ready",
    "priority": "High",
    "duration": 5000,
    "description": "line  \r\nnext\n\n",
    "attributes": {"attr-1": "Chat", "attr-unknown": 1},
    "tags": [{"name": "b"}, {"name": "a"}],
    "preconditionSteps": [],
    "steps": [
        {"id": "s1", "action": "Do", "expected": "Done", "testData": None, "comments": ""},
        {"id": "s2", "workItemId": "shared-uuid", "action": "", "expected": ""},
        {"id": "s3", "workItemId": "foreign-uuid"},
    ],
    "postconditionSteps": [],
    "links": [{"url": "https://x"}],
    "iterations": [],
    "createdById": "read-only",
}


def test_to_meta_falls_back_to_created_date() -> None:
    meta = to_meta(RAW)
    assert (meta.id, meta.global_id, meta.version_id, meta.version_number) == ("w", 7, "v2", 2)
    assert meta.modified_date == "2026-01-01T00:00:00Z"
    assert meta.automated is True


def test_to_case() -> None:
    case = to_case(RAW, CTX)
    assert case.name == "Name"
    assert case.description == "line\nnext"
    assert case.tags == ("a", "b")
    assert case.attributes == {"Component": "Chat", "attr-unknown": 1}
    assert case.steps == (
        Step(action="Do", expected="Done"),
        Step(shared_step="1003"),
        Step(shared_step="foreign-uuid"),
    )


def test_update_body_replaces_only_changed_fields() -> None:
    base = to_case(RAW, CTX)
    local = Case(**{**base.__dict__, "priority": "Low", "attributes": {"Component": "Voice"}})
    body = update_body(RAW, base, local, "new-section", CTX)
    assert body["priority"] == "Low"
    assert body["attributes"] == {"attr-1": "Voice"}
    assert body["sectionId"] == "new-section"
    assert body["description"] == RAW["description"]  # raw text kept
    assert body["steps"] == RAW["steps"]
    assert body["links"] == RAW["links"]
    assert "createdById" not in body
    assert "versionId" not in body


def test_update_body_maps_steps_and_keeps_ids_by_position() -> None:
    base = to_case(RAW, CTX)
    local = Case(**{**base.__dict__, "steps": (Step(action="New"), Step(shared_step="1003"))})
    body = update_body(RAW, base, local, "s", CTX)
    assert body["steps"] == [
        {"action": "New", "expected": "", "testData": "", "comments": "", "id": "s1"},
        {"workItemId": "shared-uuid", "id": "s2"},
    ]


def test_update_body_rejects_type_change() -> None:
    base = to_case(RAW, CTX)
    with pytest.raises(ValueError, match="type"):
        update_body(RAW, base, Case(**{**base.__dict__, "type": "CheckLists"}), "s", CTX)


def test_unknown_shared_step_global_id() -> None:
    base = to_case(RAW, CTX)
    local = Case(**{**base.__dict__, "steps": (Step(shared_step="999"),)})
    with pytest.raises(ValueError, match="unknown shared step 999"):
        update_body(RAW, base, local, "s", CTX)


def test_create_body() -> None:
    case = Case(name="N", duration_ms=1000, tags=("t",), steps=(Step(action="A"),))
    body = create_body(case, "s", CTX)
    assert body["projectId"] == "p"
    assert body["sectionId"] == "s"
    assert body["entityTypeName"] == "TestCases"
    assert body["duration"] == 1000
    assert body["tags"] == [{"name": "t"}]
    assert body["steps"] == [{"action": "A", "expected": "", "testData": "", "comments": ""}]


def test_changed_fields() -> None:
    base = Case(name="A")
    assert changed_fields(base, Case(name="B", priority="Low")) == ["name", "priority"]


def test_validate_duration() -> None:
    (problem,) = validate(Case(name="A", duration_ms=0))
    assert problem.startswith("duration_ms must be greater than 0 for TestCases")
    assert validate(Case(name="A", type="SharedSteps")) == []
