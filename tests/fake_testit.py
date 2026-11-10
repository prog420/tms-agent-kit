"""In-memory Test IT API for tests. Mirrors behavior observed on a real instance."""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any

import httpx

Json = dict[str, Any]

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
ROOT_SECTION = "10000000-0000-0000-0000-000000000000"
AI_SECTION = "20000000-0000-0000-0000-000000000000"
CONF_ANY = "40000000-0000-0000-0000-000000000001"
CONF_CHROME = "40000000-0000-0000-0000-000000000002"

_SHORT_FIELDS = (
    "id",
    "globalId",
    "versionId",
    "versionNumber",
    "name",
    "entityTypeName",
    "sectionId",
    "state",
    "priority",
    "duration",
    "isDeleted",
    "modifiedDate",
)


class FakeTestIt:
    def __init__(self) -> None:
        self.project: Json = {
            "id": PROJECT_ID,
            "globalId": 42,
            "name": "Demo",
            "attributesScheme": [],
        }
        self.sections: dict[str, Json] = {
            ROOT_SECTION: {"id": ROOT_SECTION, "name": "Demo", "parentId": None},
            AI_SECTION: {"id": AI_SECTION, "name": "AI", "parentId": ROOT_SECTION},
        }
        self.items: dict[str, Json] = {}
        self.configurations: list[Json] = [
            {"id": CONF_ANY, "name": "Any", "isDefault": True, "isDeleted": False},
            {"id": CONF_CHROME, "name": "Chrome", "isDefault": False, "isDeleted": False},
        ]
        self.plans: dict[str, Json] = {}
        self.suites: dict[str, Json] = {}
        self.autotests: dict[str, Json] = {}
        self.runs: dict[str, Json] = {}
        self.results: dict[str, Json] = {}
        self.attachments: dict[str, Json] = {}
        self.writes: list[tuple[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self._next_global_id = 1000

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # Seeding helpers

    def add_item(self, **fields: Any) -> Json:
        item_id = str(uuid.uuid4())
        item: Json = {
            "id": item_id,
            "globalId": self._take_global_id(),
            "versionId": item_id,
            "versionNumber": 1,
            "projectId": PROJECT_ID,
            "sectionId": AI_SECTION,
            "name": "Case",
            "entityTypeName": "TestCases",
            "state": "NotReady",
            "priority": "Medium",
            "duration": 60000,
            "description": "",
            "attributes": {},
            "tags": [],
            "preconditionSteps": [],
            "steps": [],
            "postconditionSteps": [],
            "iterations": [],
            "autoTests": [],
            "attachments": [],
            "links": [],
            "parameters": [],
            "isAutomated": False,
            "isDeleted": False,
            "createdDate": "2026-01-01T00:00:00Z",
            "modifiedDate": None,
        }
        item.update(fields)
        for key in ("preconditionSteps", "steps", "postconditionSteps"):
            item[key] = [self._step(s) for s in item[key]]
        self.items[item_id] = item
        return item

    def edit_item(self, item_id: str, **fields: Any) -> Json:
        """Simulate an edit made by someone else in the TMS."""
        item = self.items[item_id]
        item.update(fields)
        self._bump(item)
        return item

    # HTTP

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            body = None
            response = self._upload(path, request)
            if response is not None:
                return response
        else:
            body = json.loads(request.content) if request.content else None

        if method == "GET" and path in (f"/api/v2/projects/{PROJECT_ID}", "/api/v2/projects/42"):
            return httpx.Response(200, json=self.project)
        if method == "GET" and re.fullmatch(r"/api/v2/projects/[^/]+/sections", path):
            return self._paged(request, list(self.sections.values()))
        if method == "POST" and re.fullmatch(r"/api/v2/projects/[^/]+/workItems/search", path):
            flt = (body or {}).get("filter") or {}
            items = [
                i for i in self.items.values() if i["isDeleted"] == flt.get("isDeleted", False)
            ]
            return self._paged(request, [{k: i.get(k) for k in _SHORT_FIELDS} for i in items])
        match = re.fullmatch(r"/api/v2/workItems/([^/]+)", path)
        if match and method == "GET":
            item = self._find(match.group(1))
            if item is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=copy.deepcopy(item))
        if match and method == "DELETE":
            item = self._find(match.group(1))
            if item is None:
                return httpx.Response(404)
            item["isDeleted"] = True
            self.writes.append(("DELETE", item["id"]))
            return httpx.Response(204)
        response = self._handle_plans(method, path, body, request)
        if response is None:
            response = self._handle_runs(method, path, body, request)
        if response is not None:
            return response
        if path == "/api/v2/sections" and method == "POST":
            assert body is not None
            if body["parentId"] not in self.sections:
                return httpx.Response(400, json={"error": "parent section not found"})
            self.writes.append(("POST section", body))
            section = {"id": str(uuid.uuid4()), "name": body["name"], "parentId": body["parentId"]}
            self.sections[section["id"]] = section
            return httpx.Response(201, json=copy.deepcopy(section))
        if path == "/api/v2/workItems" and method == "POST":
            assert body is not None
            if body["entityTypeName"] == "TestCases" and body["duration"] <= 0:
                return httpx.Response(
                    400, json={"error": {"key": "Duration must be greater than 0"}}
                )
            self.writes.append(("POST", body))
            fields = {k: v for k, v in body.items() if k != "projectId"}
            item = self.add_item(**fields)
            return httpx.Response(201, json=copy.deepcopy(item))
        if path == "/api/v2/workItems" and method == "PUT":
            assert body is not None
            if body["name"] == "reject":
                return httpx.Response(400, json={"error": {"key": "Rejected by the fake"}})
            self.writes.append(("PUT", body))
            item = self.items[body["id"]]
            for key, value in body.items():
                if key in ("preconditionSteps", "steps", "postconditionSteps"):
                    value = [self._step(s) for s in value]
                item[key] = value
            self._bump(item)
            return httpx.Response(204)
        return httpx.Response(404, json={"error": f"no route {method} {path}"})

    def _handle_plans(
        self, method: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response | None:
        if method == "GET" and re.fullmatch(r"/api/v2/projects/[^/]+/configurations", path):
            return httpx.Response(200, json=self.configurations)
        if method == "GET" and re.fullmatch(r"/api/v2/projects/[^/]+/testPlans", path):
            plans = [p for p in self.plans.values() if not p["isDeleted"]]
            return httpx.Response(200, json=copy.deepcopy(plans))
        if path == "/api/v2/testPlans" and method == "POST":
            self.writes.append(("POST plan", body))
            plan = {
                **body,
                # Test IT stores plan tags in lower case.
                "tags": [{"name": t["name"].lower()} for t in body.get("tags") or []],
                "id": str(uuid.uuid4()),
                "globalId": self._take_global_id(),
                "status": "New",
                "isDeleted": False,
                "modifiedDate": None,
                "productName": None,
                "startDate": None,
                "endDate": None,
                "hasAutomaticDurationTimer": False,
            }
            self.plans[plan["id"]] = plan
            return httpx.Response(201, json=copy.deepcopy(plan))
        if path == "/api/v2/testPlans" and method == "PUT":
            self.writes.append(("PUT plan", body))
            tags = [{"name": t["name"].lower()} for t in body.get("tags") or []]
            self.plans[body["id"]].update({**body, "tags": tags})
            return httpx.Response(204)
        match = re.fullmatch(r"/api/v2/testPlans/([^/]+)", path)
        if match:
            found = self.plans.get(match.group(1))
            if found is None:
                return httpx.Response(404, json={"error": "not found"})
            if method == "DELETE":
                self.writes.append(("DELETE plan", found["id"]))
                found["isDeleted"] = True
                return httpx.Response(204)
            return httpx.Response(200, json=copy.deepcopy(found))
        if path == "/api/v2/testSuites" and method == "POST":
            self.writes.append(("POST suite", body))
            suite = {**body, "id": str(uuid.uuid4()), "workItems": [], "configurations": [CONF_ANY]}
            self.suites[suite["id"]] = suite
            return httpx.Response(201, json=copy.deepcopy(suite))
        match = re.fullmatch(r"/api/v2/testSuites/([^/]+)/(workItems|configurations)", path)
        if match and method == "POST":
            self.writes.append((f"POST suite {match.group(2)}", body))
            self.suites[match.group(1)][match.group(2)] = list(body)
            return httpx.Response(204)
        if path == "/api/v2/testPoints/search" and method == "POST":
            plan_ids = set(body.get("testPlanIds") or [])
            points = [
                {
                    "id": f"{suite['id']}:{work_item}:{conf}",
                    "workItemId": work_item,
                    "configurationId": conf,
                    "testSuiteId": suite["id"],
                    "status": "NoResults",
                    "isDeleted": False,
                }
                for suite in self.suites.values()
                if suite["testPlanId"] in plan_ids
                for work_item in suite["workItems"]
                for conf in suite["configurations"]
            ]
            return self._paged(request, points)
        return None

    def _handle_runs(
        self, method: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response | None:
        if path == "/api/v2/autoTests/search" and method == "POST":
            flt = body["filter"]
            matches = [
                a
                for a in self.autotests.values()
                if a["externalId"] in flt["externalIds"] and a["projectId"] in flt["projectIds"]
            ]
            return self._paged(request, copy.deepcopy(matches))
        if path == "/api/v2/autoTests" and method == "POST":
            self.writes.append(("POST autotest", body))
            links = body.pop("workItemIdsForLinkWithAutoTest", None) or []
            autotest = {**body, "id": str(uuid.uuid4()), "globalId": self._take_global_id()}
            self.autotests[autotest["id"]] = autotest
            for item_id in links:
                self._link(autotest, item_id)
            return httpx.Response(201, json=copy.deepcopy(autotest))
        if path == "/api/v2/autoTests" and method == "PUT":
            self.writes.append(("PUT autotest", body))
            self.autotests[body["id"]].update(body)
            return httpx.Response(204)
        match = re.fullmatch(r"/api/v2/autoTests/([^/]+)/workItems", path)
        if match and method == "POST":
            self.writes.append(("POST autotest link", body))
            self._link(self.autotests[match.group(1)], body["id"])
            return httpx.Response(204)
        if path == "/api/v2/testRuns/byConfigurations" and method == "POST":
            self.writes.append(("POST run", body))
            for selector in body["testPointSelectors"]:
                for item_id in selector["workItemIds"]:
                    if not self.items[item_id]["isAutomated"]:
                        message = "TestRun cannot be created with non-automated TestPoint"
                        return httpx.Response(400, json={"error": {"key": message}})
            self.plans[body["testPlanId"]]["status"] = "InProgress"
            run = {**body, "id": str(uuid.uuid4()), "stateName": "InProgress"}
            self.runs[run["id"]] = run
            return httpx.Response(201, json=copy.deepcopy(run))
        match = re.fullmatch(r"/api/v2/testRuns/([^/]+)/testResults", path)
        if match and method == "POST":
            self.writes.append(("POST results", body))
            known = {a["externalId"] for a in self.autotests.values()}
            if any(r["autoTestExternalId"] not in known for r in body):
                return httpx.Response(400, json={"error": "autotest not found"})
            ids = []
            for result in body:
                result_id = str(uuid.uuid4())
                self.results[result_id] = {**result, "id": result_id, "testRunId": match.group(1)}
                ids.append(result_id)
            return httpx.Response(200, json=ids)
        match = re.fullmatch(r"/api/v2/testResults/([^/]+)/attachments/([^/]+)", path)
        if match and method == "PUT":
            self.writes.append(("PUT result attachment", match.group(2)))
            attachment = self.attachments[match.group(2)]
            self.results[match.group(1)].setdefault("attachments", []).append(
                {"id": attachment["id"]}
            )
            return httpx.Response(200)
        match = re.fullmatch(r"/api/v2/testResults/([^/]+)", path)
        if match:
            result = self.results.get(match.group(1))
            if result is None:
                return httpx.Response(404, json={"error": "not found"})
            if method == "PUT":
                self.writes.append(("PUT result", body))
                result.update(body)
                return httpx.Response(204)
            return httpx.Response(200, json=copy.deepcopy(result))
        return None

    def _upload(self, path: str, request: httpx.Request) -> httpx.Response | None:
        if path != "/api/v2/attachments" or request.method != "POST":
            return None
        match = re.search(rb'filename="([^"]*)"', request.content)
        name = match.group(1).decode() if match else ""
        self.writes.append(("POST attachment", name))
        attachment_id = str(uuid.uuid4())
        attachment: Json = {"id": attachment_id, "name": name, "size": len(request.content)}
        self.attachments[attachment_id] = attachment
        return httpx.Response(201, json=attachment)

    def _link(self, autotest: Json, item_id: str) -> None:
        """Linking an autotest makes the case automated and bumps its version."""
        item = self.items[item_id]
        item["autoTests"].append(copy.deepcopy(autotest))
        item["isAutomated"] = True
        self._bump(item)

    def _find(self, key: str) -> Json | None:
        if key in self.items:
            return self.items[key]
        return next((i for i in self.items.values() if str(i["globalId"]) == key), None)

    def _paged(self, request: httpx.Request, items: list[Json]) -> httpx.Response:
        skip = int(request.url.params.get("Skip", 0))
        take = int(request.url.params.get("Take", 100))
        headers = {"Pagination-Total-Items": str(len(items))}
        return httpx.Response(200, json=items[skip : skip + take], headers=headers)

    def _step(self, step: Json) -> Json:
        full = {"action": "", "expected": "", "testData": "", "comments": "", "workItemId": None}
        full.update(step)
        full.setdefault("id", str(uuid.uuid4()))
        if not full["id"]:
            full["id"] = str(uuid.uuid4())
        return full

    def _bump(self, item: Json) -> None:
        item["versionId"] = str(uuid.uuid4())
        item["versionNumber"] += 1
        item["modifiedDate"] = f"2026-01-01T00:00:{item['versionNumber']:02d}Z"

    def _take_global_id(self) -> int:
        self._next_global_id += 1
        return self._next_global_id
