import json
import random
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from energy_curves.ingestion.eia import (
    EiaAuthError,
    EiaClient,
    EiaPaginationError,
    EiaRequestError,
    EiaTransientError,
    RetryPolicy,
    redact,
)

KEY = "k" * 40
URL = "https://api.eia.test/v2/petroleum/pri/spt/data/"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/eia_spot_page.json").read_text())


def body(rows: list[dict[str, object]], total: int, echo_key: bool = False) -> dict[str, object]:
    params: dict[str, object] = {"frequency": "daily"}
    if echo_key:
        params["api_key"] = KEY
    return {"response": {"total": str(total), "data": rows}, "request": {"params": params}}


def rows(n: int, start: int = 0) -> list[dict[str, object]]:
    return [
        {"period": f"2026-01-{i + 1:02d}", "series": "RWTC", "value": "1", "units": "$/BBL"}
        for i in range(start, start + n)
    ]


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> EiaClient:
    return EiaClient(
        KEY,
        "https://api.eia.test/v2",
        sleep=sleeps.append,
        rng=random.Random(0),
        clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
    )


def fetch(client: EiaClient) -> list:
    return list(
        client.fetch("petroleum/pri/spt", ["RWTC", "RBRTE"], date(2026, 1, 1), date(2026, 1, 31))
    )


@respx.mock
def test_recorded_structure_parses(client: EiaClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=FIXTURE))
    pages = fetch(client)
    assert [r["value"] for r in pages[0].rows] == ["61.25", "65.5", "60.875"]


@respx.mock
def test_pagination_is_deterministic_and_complete(client: EiaClient) -> None:
    route = respx.get(URL)
    route.side_effect = [
        httpx.Response(200, json=body(rows(2), 3)),
        httpx.Response(200, json=body(rows(1, start=2), 3)),
    ]
    pages = fetch(client)
    assert sum(len(p.rows) for p in pages) == 3
    first, second = (dict(c.request.url.params.multi_items()) for c in route.calls)
    assert (first["offset"], second["offset"]) == ("0", "2")
    sent = route.calls[0].request.url.params
    assert sent.get_list("facets[series][]") == ["RBRTE", "RWTC"]  # sorted
    assert (sent["sort[0][column]"], sent["sort[1][column]"]) == ("period", "series")


@respx.mock
def test_overlapping_pages_rejected(client: EiaClient) -> None:
    respx.get(URL).side_effect = [
        httpx.Response(200, json=body(rows(2), 4)),
        httpx.Response(200, json=body(rows(2, start=1), 4)),
    ]
    with pytest.raises(EiaPaginationError, match="more than one page"):
        fetch(client)


@respx.mock
def test_short_result_rejected(client: EiaClient) -> None:
    respx.get(URL).side_effect = [
        httpx.Response(200, json=body(rows(2), 3)),
        httpx.Response(200, json=body([], 3)),
    ]
    with pytest.raises(EiaPaginationError, match="received 2 rows"):
        fetch(client)


@respx.mock
def test_total_changing_mid_fetch_rejected(client: EiaClient) -> None:
    respx.get(URL).side_effect = [
        httpx.Response(200, json=body(rows(2), 3)),
        httpx.Response(200, json=body(rows(1, start=2), 4)),
    ]
    with pytest.raises(EiaPaginationError, match="total changed"):
        fetch(client)


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_errors_retried_with_jitter(
    client: EiaClient, sleeps: list[float], status: int
) -> None:
    respx.get(URL).side_effect = [
        httpx.Response(status),
        httpx.Response(status),
        httpx.Response(200, json=body(rows(1), 1)),
    ]
    assert len(fetch(client)) == 1
    assert len(sleeps) == 2
    assert all(0 <= s <= RetryPolicy().max_delay for s in sleeps)


@respx.mock
def test_retry_after_header_respected(client: EiaClient, sleeps: list[float]) -> None:
    respx.get(URL).side_effect = [
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json=body(rows(1), 1)),
    ]
    fetch(client)
    assert sleeps == [3.0]


@respx.mock
def test_transport_errors_retried(client: EiaClient) -> None:
    respx.get(URL).side_effect = [
        httpx.ConnectTimeout("slow"),
        httpx.Response(200, json=body(rows(1), 1)),
    ]
    assert len(fetch(client)) == 1


@respx.mock
def test_retry_budget_bounded(client: EiaClient, sleeps: list[float]) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(EiaTransientError):
        fetch(client)
    assert route.call_count == RetryPolicy().max_attempts
    assert len(sleeps) == RetryPolicy().max_attempts - 1


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_fail_fast(client: EiaClient, sleeps: list[float], status: int) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(EiaAuthError):
        fetch(client)
    assert route.call_count == 1 and sleeps == []


@respx.mock
def test_other_client_errors_fail_fast(client: EiaClient) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(400))
    with pytest.raises(EiaRequestError):
        fetch(client)
    assert route.call_count == 1


@respx.mock
def test_unexpected_shape_rejected(client: EiaClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"error": "nope"}))
    with pytest.raises(EiaRequestError, match="shape"):
        fetch(client)


def test_missing_key_rejected() -> None:
    with pytest.raises(EiaAuthError):
        EiaClient("")


@respx.mock
def test_echoed_key_redacted_from_page_body(client: EiaClient) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=body(rows(1), 1, echo_key=True)))
    page = fetch(client)[0]
    assert KEY.encode() not in page.body
    assert json.loads(page.body)["request"]["params"]["api_key"] == "REDACTED"
    assert "api_key" not in page.params


def test_redact_removes_stray_copies_and_non_json() -> None:
    assert KEY.encode() not in redact(f'{{"note": "{KEY}"}}'.encode(), KEY)
    assert KEY.encode() not in redact(f"plain {KEY} text".encode(), KEY)
