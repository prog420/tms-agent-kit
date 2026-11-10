"""Conversion between Test IT work item JSON and the neutral case model."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tms_agent_kit.model import Case, CaseMeta, Step, normalize_text

Json = dict[str, Any]

# Fields of ApiV2WorkItemsPutRequest. Everything else in the GET response is read-only.
_PUT_FIELDS = (
    "id",
    "sectionId",
    "name",
    "duration",
    "state",
    "priority",
    "description",
    "attributes",
    "tags",
    "preconditionSteps",
    "steps",
    "postconditionSteps",
    "iterations",
    "autoTests",
    "attachments",
    "links",
    "parameters",
)

_STEP_LISTS = {
    "preconditions": "preconditionSteps",
    "steps": "steps",
    "postconditions": "postconditionSteps",
}


@dataclass(frozen=True)
class ProjectContext:
    """Project data needed to map ids to readable names and back."""

    project_id: str
    attribute_names: Mapping[str, str] = field(default_factory=dict)  # attribute id -> name
    shared_step_global_ids: Mapping[str, int] = field(default_factory=dict)  # uuid -> global id

    def attribute_id(self, name: str) -> str:
        for attr_id, attr_name in self.attribute_names.items():
            if attr_name == name:
                return attr_id
        return name  # unknown names are kept as raw ids

    def shared_step_uuid(self, ref: str) -> str:
        if ref.isdigit():
            for uuid, global_id in self.shared_step_global_ids.items():
                if global_id == int(ref):
                    return uuid
            raise ValueError(f"unknown shared step {ref}")
        return ref


# TMS -> model


def to_meta(raw: Json) -> CaseMeta:
    return CaseMeta(
        id=raw["id"],
        global_id=raw.get("globalId"),
        version_id=raw.get("versionId"),
        version_number=raw.get("versionNumber"),
        modified_date=raw.get("modifiedDate") or raw.get("createdDate"),
        automated=bool(raw.get("isAutomated")),
    )


def to_case(raw: Json, ctx: ProjectContext) -> Case:
    attributes = {
        ctx.attribute_names.get(attr_id, attr_id): value
        for attr_id, value in (raw.get("attributes") or {}).items()
    }
    return Case(
        name=(raw.get("name") or "").strip(),
        type=raw.get("entityTypeName") or "TestCases",
        state=raw.get("state") or "NotReady",
        priority=raw.get("priority") or "Medium",
        duration_ms=int(raw.get("duration") or 0),
        tags=tuple(sorted({t["name"].strip() for t in raw.get("tags") or [] if t.get("name")})),
        attributes=attributes,
        description=normalize_text(raw.get("description")),
        preconditions=_to_steps(raw.get("preconditionSteps"), ctx),
        steps=_to_steps(raw.get("steps"), ctx),
        postconditions=_to_steps(raw.get("postconditionSteps"), ctx),
    )


def _to_steps(raw_steps: list[Json] | None, ctx: ProjectContext) -> tuple[Step, ...]:
    steps = []
    for raw in raw_steps or []:
        shared = raw.get("workItemId")
        if shared:
            global_id = ctx.shared_step_global_ids.get(shared)
            steps.append(Step(shared_step=str(global_id) if global_id is not None else shared))
            continue
        steps.append(
            Step(
                action=normalize_text(raw.get("action")),
                expected=normalize_text(raw.get("expected")),
                test_data=normalize_text(raw.get("testData")),
                comments=normalize_text(raw.get("comments")),
            )
        )
    return tuple(steps)


# model -> TMS


def validate(case: Case) -> list[str]:
    """Test IT rules that the Markdown format does not enforce. Checked before push."""
    problems = []
    if case.type == "TestCases" and case.duration_ms <= 0:
        problems.append(
            "duration_ms must be greater than 0 for TestCases; "
            "add it to the frontmatter, e.g. 'duration_ms: 300000' (5 minutes)"
        )
    return problems


def changed_fields(base: Case, local: Case) -> list[str]:
    """Names of model fields that differ."""
    return [
        name for name in Case.__dataclass_fields__ if getattr(base, name) != getattr(local, name)
    ]


def update_body(
    remote: Json,
    base: Case,
    local: Case,
    section_id: str,
    ctx: ProjectContext,
) -> Json:
    """PUT body: the remote work item with only the locally changed fields replaced.

    Unchanged fields keep their raw TMS values, so formatting the Markdown format does not
    model is preserved.
    """
    body = {key: copy.deepcopy(remote[key]) for key in _PUT_FIELDS if key in remote}
    body["sectionId"] = section_id
    for name in changed_fields(base, local):
        if name == "type":
            raise ValueError("the case type cannot be changed")
        _apply_field(body, name, local, ctx)
    return body


def create_body(case: Case, section_id: str, ctx: ProjectContext) -> Json:
    body: Json = {
        "projectId": ctx.project_id,
        "sectionId": section_id,
        "entityTypeName": case.type,
        "attributes": {},
        "tags": [],
        "links": [],
    }
    for name in Case.__dataclass_fields__:
        if name != "type":
            _apply_field(body, name, case, ctx)
    return body


def _apply_field(body: Json, name: str, case: Case, ctx: ProjectContext) -> None:
    value = getattr(case, name)
    if name == "duration_ms":
        body["duration"] = value
    elif name == "tags":
        body["tags"] = [{"name": tag} for tag in value]
    elif name == "attributes":
        body["attributes"] = {ctx.attribute_id(key): v for key, v in value.items()}
    elif name in _STEP_LISTS:
        key = _STEP_LISTS[name]
        body[key] = _from_steps(value, body.get(key) or [], ctx)
    else:
        body[name] = value


def _from_steps(steps: tuple[Step, ...], old: list[Json], ctx: ProjectContext) -> list[Json]:
    result = []
    for index, step in enumerate(steps):
        raw: Json = {
            "action": step.action,
            "expected": step.expected,
            "testData": step.test_data,
            "comments": step.comments,
        }
        if step.shared_step is not None:
            raw = {"workItemId": ctx.shared_step_uuid(step.shared_step)}
        # Keep step ids by position so unchanged steps stay the same entities in the TMS.
        if index < len(old) and old[index].get("id"):
            raw["id"] = old[index]["id"]
        result.append(raw)
    return result
