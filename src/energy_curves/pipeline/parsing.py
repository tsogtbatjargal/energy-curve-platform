"""Strict parsing of EIA observation rows into typed values.

EIA returns numbers as JSON strings ("61.25"). Valid numeric strings must parse; anything else
is rejected with a reason rather than coerced. Non-positive prices are valid observations
(WTI settled at -37.63 on 2020-04-20) and are kept; only shape estimation excludes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from energy_curves.catalog import SERIES

PRICE_SCALE = 6  # Silver contract: Decimal(18, 6)
_NUMERIC = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


class Rejected(ValueError):
    """A row that cannot enter Silver; `reason` is stored with the quarantined row."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Observation:
    series_id: str
    observation_date: date
    price: Decimal
    unit: str


def parse_price(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise Rejected("missing or non-numeric value")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        # Floats would already have lost exactness; EIA does not send them.
        raise Rejected(f"unsupported value type {type(value).__name__}")
    if not _NUMERIC.fullmatch(text):
        raise Rejected(f"malformed numeric string {text!r}")
    price = Decimal(text)
    exponent = price.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > PRICE_SCALE:
        raise Rejected(f"more than {PRICE_SCALE} decimal places: {text!r}")
    return price


def parse_row(row: dict[str, Any]) -> Observation:
    series_id = row.get("series")
    if not isinstance(series_id, str) or series_id not in SERIES:
        raise Rejected(f"unknown series {series_id!r}")
    expected_unit = SERIES[series_id].unit
    if row.get("units") != expected_unit:
        raise Rejected(f"unit {row.get('units')!r}, expected {expected_unit!r}")
    period = row.get("period")
    try:
        observation_date = date.fromisoformat(str(period))
    except ValueError as exc:
        raise Rejected(f"unparseable period {period!r}") from exc
    if len(str(period)) != 10:
        raise Rejected(f"period is not a daily date: {period!r}")
    return Observation(series_id, observation_date, parse_price(row.get("value")), expected_unit)
