import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from tms_agent_kit import __version__
from tms_agent_kit.testit import TestItClient, TestItError

BASE_URL = "https://example.testit.software"
TOKEN = "secret-token"

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(
    handler: Handler, *, sleep: Callable[[float], None] | None = None, **kwargs: Any
) -> tuple[TestItClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = TestItClient(
        BASE_URL,
        TOKEN,
        transport=httpx.MockTransport(recording),
        sleep=sleep or (lambda _: None),
        **kwargs,
    )
    return client, requests


def paged(items: list[dict[str, Any]], total: int | None = None) -> Handler:
    """Serve `items` honoring Skip/Take, like the Test IT list endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["Skip"])
        take = int(request.url.params["Take"])
        headers = {} if total is None else {"Pagination-Total-Items": str(total)}
        return httpx.Response(200, json=items[skip : skip + take], headers=headers)

    return handler


def ok(body: object) -> Handler:
    return lambda _: httpx.Response(200, json=body)


def test_default_headers() -> None:
    client, requests = make_client(ok({"name": "P"}))
    client.get_project("42")
    headers = requests[0].headers
    assert headers["Authorization"] == f"PrivateToken {TOKEN}"
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == f"tms-agent-kit/{__version__}"
    assert requests[0].url == f"{BASE_URL}/api/v2/projects/42"


def test_ids_are_encoded_as_one_path_segment() -> None:
    client, requests = make_client(ok({"id": "w"}))
    client.get_work_item("../projects/1?x=1")
    assert requests[0].url.raw_path == b"/api/v2/workItems/..%2Fprojects%2F1%3Fx%3D1"


@pytest.mark.parametrize("total", [None, 5])
def test_pagination_collects_all_pages(total: int | None) -> None:
    items = [{"id": str(i)} for i in range(5)]
    client, requests = make_client(paged(items, total), page_size=2)
    assert list(client.iter_sections("42")) == items
    assert [r.url.params["Skip"] for r in requests] == ["0", "2", "4"]
    assert all(r.url.params["Take"] == "2" for r in requests)


def test_pagination_exact_multiple_without_total_header() -> None:
    items = [{"id": str(i)} for i in range(4)]
    client, requests = make_client(paged(items), page_size=2)
    assert list(client.iter_sections("42")) == items
    assert len(requests) == 3  # the last page is empty


def test_pagination_stops_on_total_header() -> None:
    items = [{"id": str(i)} for i in range(4)]
    client, requests = make_client(paged(items, total=4), page_size=2)
    assert list(client.iter_sections("42")) == items
    assert len(requests) == 2


def test_pagination_detects_ignored_skip() -> None:
    client, requests = make_client(ok([{"id": "a"}, {"id": "b"}]), page_size=2)
    with pytest.raises(TestItError, match="pagination does not advance"):
        list(client.iter_sections("42"))
    assert len(requests) == 2


def test_search_work_items_sends_filters() -> None:
    client, requests = make_client(paged([{"id": "a"}]))
    assert list(client.search_work_items("42", {"isDeleted": False})) == [{"id": "a"}]
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v2/projects/42/workItems/search"
    assert json.loads(request.content) == {"filter": {"isDeleted": False}}


def test_search_work_items_default_filter() -> None:
    client, requests = make_client(paged([]))
    assert list(client.search_work_items("42")) == []
    assert json.loads(requests[0].content) == {"filter": {}}


def test_get_work_item_with_version() -> None:
    client, requests = make_client(ok({"id": "w"}))
    assert client.get_work_item("1042", version_id="v1") == {"id": "w"}
    assert requests[0].url.path == "/api/v2/workItems/1042"
    assert requests[0].url.params["versionId"] == "v1"


def test_get_section() -> None:
    client, requests = make_client(ok({"id": "s"}))
    assert client.get_section("s") == {"id": "s"}
    assert requests[0].url.path == "/api/v2/sections/s"


def test_retry_after_seconds() -> None:
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    delays: list[float] = []
    client, requests = make_client(lambda _: next(responses), sleep=delays.append)
    assert client.get_project("42") == {}
    assert len(requests) == 2
    assert delays == [7.0]


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        (format_datetime(datetime.now(UTC) - timedelta(minutes=1), usegmt=True), 0.0),
        (format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True), 30.0),
        ("3600", 30.0),
    ],
)
def test_retry_after_is_bounded(retry_after: str, expected: float) -> None:
    responses = iter(
        [httpx.Response(503, headers={"Retry-After": retry_after}), httpx.Response(200, json={})]
    )
    delays: list[float] = []
    client, _ = make_client(lambda _: next(responses), sleep=delays.append)
    client.get_project("42")
    assert delays == [expected]


def test_backoff_is_jittered_and_capped() -> None:
    delays: list[float] = []
    client, requests = make_client(
        lambda _: httpx.Response(503), sleep=delays.append, max_retries=6
    )
    with pytest.raises(TestItError) as exc:
        client.get_project("42")
    assert exc.value.status_code == 503
    assert len(requests) == 7
    caps = [1, 2, 4, 8, 16, 30]
    assert all(0 <= d <= cap for d, cap in zip(delays, caps, strict=True))


def test_retries_on_transport_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"name": "P"})

    client, _ = make_client(handler)
    assert client.get_project("42") == {"name": "P"}
    assert calls == 2


def test_transport_error_after_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client, requests = make_client(handler, max_retries=1)
    with pytest.raises(TestItError, match="ConnectError: boom"):
        client.get_project("42")
    assert len(requests) == 2


def test_post_search_is_retried() -> None:
    responses = iter([httpx.Response(503), httpx.Response(200, json=[])])
    client, requests = make_client(lambda _: next(responses))
    assert list(client.search_work_items("42")) == []
    assert len(requests) == 2


def test_error_message_hides_token_and_hints_on_401() -> None:
    client, _ = make_client(lambda _: httpx.Response(401, text="Unauthorized"))
    with pytest.raises(TestItError) as exc:
        client.get_project("42")
    message = str(exc.value)
    assert "HTTP 401" in message
    assert "check the API token" in message
    assert TOKEN not in message
    assert exc.value.status_code == 401


def test_not_found_is_not_retried() -> None:
    client, requests = make_client(lambda _: httpx.Response(404, text="missing"))
    with pytest.raises(TestItError, match="HTTP 404: missing"):
        client.get_work_item("1")
    assert len(requests) == 1


def test_invalid_json() -> None:
    client, _ = make_client(lambda _: httpx.Response(200, text="<html>"))
    with pytest.raises(TestItError, match="not valid JSON"):
        client.get_project("42")


@pytest.mark.parametrize("body", [{"not": "a list"}, [1, 2]])
def test_unexpected_list_shape(body: object) -> None:
    client, _ = make_client(ok(body))
    with pytest.raises(TestItError, match="expected a JSON array of objects"):
        list(client.iter_sections("42"))


def test_unexpected_object_shape() -> None:
    client, _ = make_client(ok([]))
    with pytest.raises(TestItError, match="expected a JSON object"):
        client.get_project("42")


@pytest.mark.parametrize(("option", "value"), [("page_size", 0), ("max_retries", -1)])
def test_invalid_options(option: str, value: int) -> None:
    kwargs: dict[str, Any] = {option: value}
    with pytest.raises(ValueError, match=option):
        TestItClient(BASE_URL, TOKEN, **kwargs)


def test_create_work_item() -> None:
    client, requests = make_client(lambda _: httpx.Response(201, json={"id": "w"}))
    assert client.create_work_item({"name": "N"}) == {"id": "w"}
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v2/workItems"
    assert json.loads(requests[0].content) == {"name": "N"}


def test_update_work_item() -> None:
    client, requests = make_client(lambda _: httpx.Response(204))
    client.update_work_item({"id": "w"})
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/api/v2/workItems"


def test_archive_work_item() -> None:
    client, requests = make_client(lambda _: httpx.Response(204))
    client.archive_work_item("w")
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/api/v2/workItems/w"


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.create_work_item({}),
        lambda c: c.update_work_item({}),
        lambda c: c.archive_work_item("w"),
    ],
)
def test_writes_are_never_retried(call: Callable[[TestItClient], object]) -> None:
    client, requests = make_client(lambda _: httpx.Response(503))
    with pytest.raises(TestItError, match="HTTP 503"):
        call(client)
    assert len(requests) == 1
