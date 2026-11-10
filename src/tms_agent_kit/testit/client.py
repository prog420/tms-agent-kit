"""HTTP client for the Test IT REST API v2.

Write methods are called only by `push`, after a dry-run and explicit confirmation.
Writes are never retried: a retried write could be applied twice.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeAlias
from urllib.parse import quote

import httpx

from tms_agent_kit import __version__

DEFAULT_TIMEOUT = 30.0
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_RETRIES = 3

_RETRY_STATUSES = {429, 502, 503, 504}
_BASE_RETRY_DELAY = 1.0
_MAX_RETRY_DELAY = 30.0
_ERROR_BODY_LIMIT = 500

Json: TypeAlias = dict[str, Any]

logger = logging.getLogger(__name__)


class TestItError(Exception):
    """A failed Test IT API call. Never contains the token."""

    __test__ = False  # not a pytest test class

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TestItClient:
    __test__ = False  # not a pytest test class

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._page_size = page_size
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"PrivateToken {token}",
                "Accept": "application/json",
                "User-Agent": f"tms-agent-kit/{__version__}",
            },
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TestItClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Projects

    def get_project(self, project_id: str) -> Json:
        """`project_id` is a UUID or a global integer id."""
        return self._get_json(f"/api/v2/projects/{_segment(project_id)}")

    # Sections

    def iter_sections(self, project_id: str) -> Iterator[Json]:
        """All sections of a project. Items have no pre/postcondition steps."""
        return self._paginate("GET", f"/api/v2/projects/{_segment(project_id)}/sections")

    def get_section(self, section_id: str) -> Json:
        """One section with its precondition and postcondition steps."""
        return self._get_json(f"/api/v2/sections/{_segment(section_id)}")

    def create_section(self, body: Json) -> Json:
        """POST /api/v2/sections. Returns the created section."""
        return self._send_json("POST", "/api/v2/sections", body)

    # Work items

    def search_work_items(self, project_id: str, filters: Json | None = None) -> Iterator[Json]:
        """Short work item models matching `filters` (the `filter` of WorkItemSelectApiModel)."""
        return self._paginate(
            "POST",
            f"/api/v2/projects/{_segment(project_id)}/workItems/search",
            json={"filter": filters or {}},
        )

    def get_work_item(self, work_item_id: str, *, version_id: str | None = None) -> Json:
        """Full work item. `work_item_id` is a UUID or a global integer id."""
        params = {"versionId": version_id} if version_id else None
        return self._get_json(f"/api/v2/workItems/{_segment(work_item_id)}", params=params)

    def create_work_item(self, body: Json) -> Json:
        """POST /api/v2/workItems. Returns the created work item."""
        response = self._request("POST", "/api/v2/workItems", json=body)
        data = _decode(response)
        if not isinstance(data, dict):
            raise TestItError("POST /api/v2/workItems: expected a JSON object")
        return data

    def update_work_item(self, body: Json) -> None:
        """PUT /api/v2/workItems. Replaces the whole work item with `body`."""
        self._request("PUT", "/api/v2/workItems", json=body)

    def archive_work_item(self, work_item_id: str) -> None:
        """DELETE /api/v2/workItems/{id}. Moves the work item to the archive (soft delete)."""
        self._request("DELETE", f"/api/v2/workItems/{_segment(work_item_id)}")

    # Configurations

    def list_configurations(self, project_id: str) -> list[Json]:
        """Configurations of a project. `project_id` is the project UUID."""
        return self._get_list(f"/api/v2/projects/{_segment(project_id)}/configurations")

    # Test plans

    def list_test_plans(self, project_id: str) -> list[Json]:
        """Test plans of a project that are not archived. `project_id` is the project UUID."""
        path = f"/api/v2/projects/{_segment(project_id)}/testPlans"
        return self._get_list(path, params={"isDeleted": "false"})

    def get_test_plan(self, plan_id: str) -> Json:
        return self._get_json(f"/api/v2/testPlans/{_segment(plan_id)}")

    def create_test_plan(self, body: Json) -> Json:
        return self._send_json("POST", "/api/v2/testPlans", body)

    def update_test_plan(self, body: Json) -> None:
        """PUT /api/v2/testPlans. Replaces the editable plan fields with `body`."""
        self._request("PUT", "/api/v2/testPlans", json=body)

    def archive_test_plan(self, plan_id: str) -> None:
        """DELETE /api/v2/testPlans/{id}. Moves the plan to the archive (soft delete)."""
        self._request("DELETE", f"/api/v2/testPlans/{_segment(plan_id)}")

    def create_test_suite(self, body: Json) -> Json:
        return self._send_json("POST", "/api/v2/testSuites", body)

    def set_suite_work_items(self, suite_id: str, work_item_ids: list[str]) -> None:
        """Replace the work items of a static suite."""
        self._request(
            "POST", f"/api/v2/testSuites/{_segment(suite_id)}/workItems", json=work_item_ids
        )

    def set_suite_configurations(self, suite_id: str, configuration_ids: list[str]) -> None:
        path = f"/api/v2/testSuites/{_segment(suite_id)}/configurations"
        self._request("POST", path, json=configuration_ids)

    def search_test_points(self, filters: Json) -> Iterator[Json]:
        """Short test point models matching `filters` (TestPointFilterRequestModel)."""
        return self._paginate("POST", "/api/v2/testPoints/search", json=filters)

    # Autotests (used to record results of manual cases, see docs/test-plans.md)

    def search_autotests(self, project_id: str, external_ids: list[str]) -> list[Json]:
        body = {
            "filter": {"projectIds": [project_id], "externalIds": external_ids, "isDeleted": False}
        }
        return list(self._paginate("POST", "/api/v2/autoTests/search", json=body))

    def create_autotest(self, body: Json) -> Json:
        return self._send_json("POST", "/api/v2/autoTests", body)

    def update_autotest(self, body: Json) -> None:
        self._request("PUT", "/api/v2/autoTests", json=body)

    def link_autotest(self, autotest_id: str, work_item_id: str) -> None:
        path = f"/api/v2/autoTests/{_segment(autotest_id)}/workItems"
        self._request("POST", path, json={"id": work_item_id})

    # Test runs and results

    def create_test_run(self, body: Json) -> Json:
        """POST /api/v2/testRuns/byConfigurations: a run over the selected test points."""
        return self._send_json("POST", "/api/v2/testRuns/byConfigurations", body)

    def get_test_run(self, run_id: str) -> Json:
        return self._get_json(f"/api/v2/testRuns/{_segment(run_id)}")

    def send_test_results(self, run_id: str, results: list[Json]) -> list[str]:
        """POST /api/v2/testRuns/{id}/testResults. Returns the result ids in request order."""
        path = f"/api/v2/testRuns/{_segment(run_id)}/testResults"
        data = _decode(self._request("POST", path, json=results))
        if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
            raise TestItError(f"POST {path}: expected a JSON array of ids")
        return data

    def get_test_result(self, result_id: str) -> Json:
        return self._get_json(f"/api/v2/testResults/{_segment(result_id)}")

    def update_test_result(self, result_id: str, body: Json) -> None:
        self._request("PUT", f"/api/v2/testResults/{_segment(result_id)}", json=body)

    def get_test_result_attachments(self, result_id: str) -> list[Json]:
        return self._get_list(f"/api/v2/testResults/{_segment(result_id)}/attachments/info")

    def attach_to_test_result(self, result_id: str, attachment_id: str) -> None:
        """Link an uploaded file to a result that was already sent."""
        path = f"/api/v2/testResults/{_segment(result_id)}/attachments/{_segment(attachment_id)}"
        self._request("PUT", path)

    # Attachments

    def upload_attachment(self, name: str, content: bytes, content_type: str) -> Json:
        """POST /api/v2/attachments: store a file; its id can then be linked to results."""
        files = {"file": (name, content, content_type)}
        data = _decode(self._request("POST", "/api/v2/attachments", files=files))
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise TestItError("POST /api/v2/attachments: expected an object with an id")
        return data

    # Transport

    def _send_json(self, method: str, path: str, body: Json | list[Any]) -> Json:
        data = _decode(self._request(method, path, json=body))
        if not isinstance(data, dict):
            raise TestItError(f"{method} {path}: expected a JSON object")
        return data

    def _get_list(self, path: str, *, params: dict[str, Any] | None = None) -> list[Json]:
        data = _decode(self._request("GET", path, params=params))
        if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
            raise TestItError(f"GET {path}: expected a JSON array of objects")
        return data

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Json:
        data = _decode(self._request("GET", path, params=params))
        if not isinstance(data, dict):
            raise TestItError(f"GET {path}: expected a JSON object, got {type(data).__name__}")
        return data

    def _paginate(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Json | None = None,
    ) -> Iterator[Json]:
        skip = 0
        seen_ids: set[str] = set()
        while True:
            page_params = {**(params or {}), "Skip": skip, "Take": self._page_size}
            response = self._request(method, path, params=page_params, json=json, idempotent=True)
            items = _decode(response)
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise TestItError(f"{method} {path}: expected a JSON array of objects")
            # A server that ignores Skip would return the same page forever.
            page_ids = {i["id"] for i in items if isinstance(i.get("id"), str)}
            if page_ids and page_ids <= seen_ids:
                raise TestItError(f"{method} {path}: pagination does not advance (Skip={skip})")
            seen_ids |= page_ids
            yield from items
            skip += len(items)
            total = _int_header(response, "Pagination-Total-Items")
            if len(items) < self._page_size or (total is not None and skip >= total):
                return

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Json | list[Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        idempotent: bool = False,
    ) -> httpx.Response:
        # Only safe requests are retried: a retried write could be applied twice.
        retries = self._max_retries if method == "GET" or idempotent else 0
        attempt = 0
        while True:
            try:
                response = self._http.request(method, path, params=params, json=json, files=files)
            except httpx.TransportError as e:
                if attempt >= retries:
                    raise TestItError(f"{method} {path}: {type(e).__name__}: {e}") from e
                delay, reason = _backoff(attempt), type(e).__name__
            else:
                if response.status_code not in _RETRY_STATUSES or attempt >= retries:
                    if response.is_error:
                        raise _error_from_response(method, path, response)
                    return response
                delay, reason = _retry_delay(response, attempt), f"HTTP {response.status_code}"
            attempt += 1
            logger.debug(
                "Retrying %s %s in %.1fs (%s, attempt %d of %d)",
                method,
                path,
                delay,
                reason,
                attempt,
                retries,
            )
            self._sleep(delay)


def _segment(value: str) -> str:
    """Encode one URL path segment so an id cannot change the request path."""
    return quote(str(value), safe="")


def _decode(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as e:
        request = response.request
        raise TestItError(
            f"{request.method} {request.url.path}: response is not valid JSON",
            response.status_code,
        ) from e


def _error_from_response(method: str, path: str, response: httpx.Response) -> TestItError:
    message = f"{method} {path}: HTTP {response.status_code}"
    body = response.text.strip()
    if body:
        message += f": {body[:_ERROR_BODY_LIMIT]}"
    if response.status_code == 401:
        message += " (check the API token)"
    elif response.status_code == 403:
        message += " (the token has no access to this resource)"
    return TestItError(message, response.status_code)


def _int_header(response: httpx.Response, name: str) -> int | None:
    value = response.headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter."""
    cap = min(_BASE_RETRY_DELAY * 2**attempt, _MAX_RETRY_DELAY)
    return random.uniform(0, cap)  # noqa: S311 - jitter, not cryptography


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honor `Retry-After` (seconds or HTTP date), capped; otherwise back off."""
    value = response.headers.get("Retry-After")
    if value is None:
        return _backoff(attempt)
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return _backoff(attempt)
    return min(max(seconds, 0.0), _MAX_RETRY_DELAY)
