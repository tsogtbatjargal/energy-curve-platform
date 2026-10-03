"""EIA API v2 client: deterministic pagination, bounded retries, and key redaction.

The API key must never reach Bronze, logs, or hashes. Responses from current API versions do
not echo it, but older ones did (`request.params.api_key`), so redaction stays as a guarantee.
"""

from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

PAGE_SIZE = 5000  # EIA's maximum rows per JSON response
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class EiaError(Exception):
    """Base class for EIA client failures."""


class EiaAuthError(EiaError):
    """Missing or rejected API key; retrying cannot help."""


class EiaRequestError(EiaError):
    """Non-retryable client or response error."""


class EiaTransientError(EiaError):
    """Retryable failure that persisted past the retry budget."""


class EiaPaginationError(EiaError):
    """Pages overlapped or did not add up to the reported total."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 8.0

    def delay(self, attempt: int, rng: random.Random) -> float:
        """Full jitter: uniform in [0, min(max_delay, base * 2**attempt)]."""
        return rng.uniform(0, min(self.max_delay, self.base_delay * 2**attempt))


@dataclass(frozen=True)
class Page:
    """One API response, already redacted, plus the request parameters without the key."""

    body: bytes
    params: dict[str, Any]
    retrieved_at: datetime
    rows: list[dict[str, Any]]
    total: int


def redact(raw: bytes, api_key: str) -> bytes:
    """Remove the API key from a response body: echoed params first, then any stray copy."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        doc = None
    if isinstance(doc, dict):
        params = doc.get("request", {}).get("params")
        if isinstance(params, dict) and "api_key" in params:
            params["api_key"] = "REDACTED"
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    if api_key:
        raw = raw.replace(api_key.encode(), b"REDACTED")
    return raw


class EiaClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.eia.gov/v2",
        *,
        http: httpx.Client | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not api_key:
            raise EiaAuthError("EIA_API_KEY is not set")
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self._clock = clock

    def fetch(self, route: str, series_ids: list[str], start: date, end: date) -> Iterator[Page]:
        """Yield every page for the series in [start, end], sorted by period then series."""
        seen: set[tuple[str, str]] = set()
        offset, total, received = 0, None, 0
        while total is None or offset < total:
            params = self._params(series_ids, start, end, offset)
            page = self._get_with_retry(route, params)
            if total is None:
                total = page.total
            elif page.total != total:
                raise EiaPaginationError(f"total changed mid-fetch: {total} -> {page.total}")
            for row in page.rows:
                key = (str(row.get("series")), str(row.get("period")))
                if key in seen:
                    raise EiaPaginationError(f"row {key} returned on more than one page")
                seen.add(key)
            received += len(page.rows)
            yield page
            if not page.rows:
                break
            offset += len(page.rows)
        if total is not None and received != total:
            raise EiaPaginationError(f"received {received} rows, API reported {total}")

    @staticmethod
    def _params(series_ids: list[str], start: date, end: date, offset: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "frequency": "daily",
            "data[0]": "value",
            "facets[series][]": sorted(series_ids),
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "sort[1][column]": "series",
            "sort[1][direction]": "asc",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "offset": offset,
            "length": PAGE_SIZE,
        }
        return params

    def _get_with_retry(self, route: str, params: dict[str, Any]) -> Page:
        url = f"{self._base}/{route.strip('/')}/data/"
        for attempt in range(self._retry.max_attempts):
            resp: httpx.Response | None = None
            try:
                resp = self._http.get(url, params={**params, "api_key": self._key})
            except httpx.TransportError as exc:
                reason = type(exc).__name__
            else:
                if resp.status_code in (401, 403):
                    raise EiaAuthError(f"EIA rejected the API key (HTTP {resp.status_code})")
                if resp.status_code not in RETRYABLE_STATUS:
                    if resp.status_code != 200:
                        raise EiaRequestError(f"HTTP {resp.status_code} from {route}")
                    return self._page(resp.content, params)
                reason = f"HTTP {resp.status_code}"
            if attempt + 1 >= self._retry.max_attempts:
                break
            retry_after = "" if resp is None else resp.headers.get("Retry-After", "")
            wait = (
                min(float(retry_after), self._retry.max_delay)
                if retry_after.isdigit()
                else self._retry.delay(attempt, self._rng)
            )
            log.warning("eia retry", extra={"route": route, "reason": reason})
            self._sleep(wait)
        raise EiaTransientError(f"{route}: gave up after {self._retry.max_attempts} attempts")

    def _page(self, raw: bytes, params: dict[str, Any]) -> Page:
        body = redact(raw, self._key)
        try:
            doc = json.loads(body)
            response = doc["response"]
            rows = response["data"]
            total = int(response["total"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise EiaRequestError(f"unexpected EIA response shape: {exc}") from exc
        if not isinstance(rows, list):
            raise EiaRequestError("response.data is not a list")
        return Page(body=body, params=params, retrieved_at=self._clock(), rows=rows, total=total)
