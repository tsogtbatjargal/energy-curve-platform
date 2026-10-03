"""Version-specific monitored values: the data contract for outbox consumers (ADR-0013).

A `dataset_imported` event is evaluated against the values of its own `dataset_version`, never
the latest snapshot in the serving tables: a consumer that falls behind (several versions
imported before it runs) still sees exactly what each version published. The values live in the
append-only `market.monitored_values` table, written in the same transaction as the import: for
each curve and position, the version's latest curve point, with its as-of date and a NULL price
for a gap. Alert evaluation compares version v with version v - 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import psycopg


@dataclass(frozen=True)
class MonitoredValue:
    curve_id: str
    position: str
    as_of_date: date
    price: Decimal | None  # None for a gap
    status: str  # ok | gap


class MonitoredValuesUnavailable(LookupError):
    pass


def monitored_values(
    conn: psycopg.Connection, dataset_version: int
) -> dict[tuple[str, str], MonitoredValue]:
    """The monitored values of one imported version, keyed by (curve_id, position)."""
    version = conn.execute(
        "SELECT curve_points FROM market.dataset_versions WHERE dataset_version = %s",
        (dataset_version,),
    ).fetchone()
    if version is None:
        raise MonitoredValuesUnavailable(f"dataset version {dataset_version} is not imported")
    rows = conn.execute(
        "SELECT curve_id, position, as_of_date, price, status FROM market.monitored_values"
        " WHERE dataset_version = %s",
        (dataset_version,),
    ).fetchall()
    if version[0] and not rows:  # imported before migration 0003
        raise MonitoredValuesUnavailable(
            f"dataset version {dataset_version} has no monitored values; run db-import --rebuild"
        )
    return {(r[0], r[1]): MonitoredValue(*r) for r in rows}
