"""Deterministic synthetic EIA-shaped data for tests and the offline demo.

Values are invented (ADR-0011: real EIA spot and futures prices are third-party data and are
not redistributed). Every value is a pure function of (series, date), so any window returns the
same prices and pagination behaves like the real API. Pages carry "x-synthetic": true.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from energy_curves.catalog import SERIES
from energy_curves.ingestion.eia import PAGE_SIZE, Page
from energy_curves.synthetic_prices import (
    EPOCH,
    FUTURES_END,
    NEGATIVE_DAY,
    synthetic_price,
    true_shape,
)

__all__ = [
    "EPOCH",
    "FUTURES_END",
    "NEGATIVE_DAY",
    "SyntheticSource",
    "synthetic_price",
    "true_shape",
]


class SyntheticSource:
    """Drop-in replacement for EiaClient.fetch()."""

    def __init__(
        self,
        overrides: dict[tuple[str, date], Any] | None = None,
        retrieved_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
        omit: Callable[[str, date], bool] | None = None,
    ) -> None:
        # overrides lets tests inject revisions or malformed values for specific rows;
        # retrieved_at lets them order retrievals in time.
        self._overrides = overrides or {}
        self._retrieved_at = retrieved_at
        self._omit = omit or (lambda _sid, _d: False)  # simulate rows the source never returns

    def _rows(self, series_ids: list[str], start: date, end: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        d = start
        while d <= end:
            for sid in sorted(series_ids):
                if self._omit(sid, d):
                    continue
                value: Any = synthetic_price(sid, d)
                if (sid, d) in self._overrides:
                    value = self._overrides[(sid, d)]
                elif value is None:
                    continue
                else:
                    value = str(value)
                s = SERIES[sid]
                rows.append(
                    {
                        "period": d.isoformat(),
                        "series": sid,
                        "series-description": f"{s.label} (synthetic)",
                        "value": value,
                        "units": s.unit,
                    }
                )
            d += timedelta(days=1)
        return rows

    def fetch(self, route: str, series_ids: list[str], start: date, end: date) -> Iterator[Page]:
        rows = self._rows(series_ids, start, end)
        total = len(rows)
        for offset in range(0, max(total, 1), PAGE_SIZE):
            chunk = rows[offset : offset + PAGE_SIZE]
            params = {
                "route": route,
                "series": sorted(series_ids),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "offset": offset,
                "length": PAGE_SIZE,
            }
            body = {
                "x-synthetic": True,
                "response": {"total": str(total), "frequency": "daily", "data": chunk},
                "request": {"params": params},
            }
            yield Page(
                body=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
                params=params,
                retrieved_at=self._retrieved_at,
                rows=chunk,
                total=total,
            )
