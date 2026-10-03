"""Series this project ingests, with the units and routes EIA uses for them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Kind(StrEnum):
    SPOT = "spot"
    FUTURE = "future"


@dataclass(frozen=True)
class Series:
    series_id: str
    route: str  # EIA API v2 route under /v2/
    kind: Kind
    unit: str  # unit string exactly as EIA returns it
    label: str
    position: str | None = None  # "C1".."C4" for futures; None for spot
    discontinued: date | None = None  # last date the source publishes


SERIES: dict[str, Series] = {
    s.series_id: s
    for s in [
        Series("RWTC", "petroleum/pri/spt", Kind.SPOT, "$/BBL", "WTI Cushing spot"),
        Series("RBRTE", "petroleum/pri/spt", Kind.SPOT, "$/BBL", "Brent spot"),
        *[
            Series(
                f"RCLC{k}",
                "petroleum/pri/fut",
                Kind.FUTURE,
                "$/BBL",
                f"WTI C{k}",
                f"C{k}",
                discontinued=date(2024, 4, 5),
            )
            for k in range(1, 5)
        ],
    ]
}

SPOT_SERIES = ("RWTC", "RBRTE")
WTI_FUTURES = ("RCLC1", "RCLC2", "RCLC3", "RCLC4")
POSITIONS = ("Spot", "C1", "C2", "C3", "C4")
