"""The deterministic synthetic EIA-shaped price function, with the standard library only.

Every value is a pure function of (series, date), so any window returns the same prices. It lives
apart from `ingestion/synthetic.py` (which builds EIA-shaped pages) because the Glue job generates
its history from it and may import only PySpark and the standard library (ADR-0007, ADR-0023).
Values are invented (ADR-0011).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from energy_curves.catalog import SERIES, Kind

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


SHAPE_SERIES = ("RWTC", "RCLC1", "RCLC2", "RCLC3", "RCLC4")  # what the shape estimate reads


def history(
    series_ids: tuple[str, ...] | list[str], start: date, end: date
) -> Iterator[tuple[str, date, Decimal]]:
    """(series, date, price) for every business day in [start, end], ordered by date then series.
    A pure function of the arguments: no clock, no randomness beyond the seeded hash."""
    d = start
    while d <= end:
        for sid in sorted(series_ids):
            value = synthetic_price(sid, d)
            if value is not None:
                yield sid, d, value
        d += timedelta(days=1)
