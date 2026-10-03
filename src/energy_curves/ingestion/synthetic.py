"""Deterministic synthetic EIA-shaped data for tests and the offline demo.

Values are invented (ADR-0011: real EIA spot and futures prices are third-party data and are
not redistributed). Every value is a pure function of (series, date), so any window returns the
same prices and pagination behaves like the real API. Pages carry "x-synthetic": true.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from energy_curves.catalog import SERIES, Kind
from energy_curves.ingestion.eia import PAGE_SIZE, Page

FUTURES_END = date(2024, 4, 5)  # mirrors EIA: no futures after this date
NEGATIVE_DAY = date(2020, 4, 20)  # mirrors the real negative WTI settlement
EPOCH = date(2000, 1, 1)


def _noise(series_id: str, d: date) -> float:
    """Deterministic uniform value in [-1, 1) for (series, date)."""
    h = hashlib.sha256(f"{series_id}|{d.isoformat()}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**63 - 1.0


def true_shape(position: int, month: int) -> float:
    """The log-spread of contract `position` over spot that the generator bakes in."""
    return 0.004 * position + 0.003 * position * math.cos(2 * math.pi * month / 12)


def _wti(d: date) -> float:
    t = (d - EPOCH).days
    return 70 + 15 * math.sin(t / 200) + 1.5 * _noise("RWTC", d)


def synthetic_price(series_id: str, d: date) -> Decimal | None:
    """Price for a business day, or None where the real source would have no row."""
    if d.weekday() >= 5:
        return None
    series = SERIES[series_id]
    if series_id == "RWTC":
        value = -37.63 if d == NEGATIVE_DAY else _wti(d)
    elif series_id == "RBRTE":
        value = 19.33 if d == NEGATIVE_DAY else _wti(d) + 4 + 0.8 * _noise("RBRTE", d)
    else:
        if series.kind is not Kind.FUTURE or d > FUTURES_END:
            return None
        k = int(series_id[-1])
        spot = abs(_wti(d))
        value = spot * math.exp(true_shape(k, d.month)) * (1 + 0.002 * _noise(series_id, d))
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


class SyntheticSource:
    """Drop-in replacement for EiaClient.fetch()."""

    def __init__(self, overrides: dict[tuple[str, date], Any] | None = None) -> None:
        # overrides lets tests inject revisions or malformed values for specific rows.
        self._overrides = overrides or {}

    def _rows(self, series_ids: list[str], start: date, end: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        d = start
        while d <= end:
            for sid in sorted(series_ids):
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
                retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                rows=chunk,
                total=total,
            )
