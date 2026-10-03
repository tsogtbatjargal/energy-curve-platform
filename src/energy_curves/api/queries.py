"""Read queries behind the API (ADR-0014).

The caller opens one REPEATABLE READ, READ ONLY transaction per request, so the dataset version
that labels a response and the rows in it come from the same snapshot. An import committing in
between can therefore never put version N + 1 rows under a version N label or cache key.
Values are returned JSON-safe: prices as decimal strings (never floats), dates as ISO strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg

DISCLAIMER = "Modelled estimate, not market quotes."
POSITIONS = ("Spot", "C1", "C2", "C3", "C4")


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _rows(cur: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    names = [c.name for c in cur.description or []]
    return [{n: jsonable(v) for n, v in zip(names, row, strict=True)} for row in cur.fetchall()]


@dataclass(frozen=True)
class Current:
    dataset_version: int
    source: str
    created_at: datetime
    imported_at: datetime
    manifest_sha256: str  # identifies the history: every served row comes from this manifest

    @property
    def synthetic(self) -> bool:
        return self.source == "synthetic"

    def envelope(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "source": self.source,
            "synthetic": self.synthetic,
            "dataset_created_at": jsonable(self.created_at),
            "imported_at": jsonable(self.imported_at),
            "disclaimer": DISCLAIMER,
        }


def current(conn: psycopg.Connection) -> Current | None:
    row = conn.execute(
        "SELECT dataset_version, source, created_at, imported_at, manifest_sha256"
        " FROM market.dataset_versions ORDER BY dataset_version DESC LIMIT 1"
    ).fetchone()
    return Current(*row) if row else None


def latest_curves(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Per curve, the latest available point of each position, with that point's own date.

    Same rule as the monitored values (ADR-0013): the point at the position's latest as-of
    date, a gap included."""
    cur = conn.execute(
        "SELECT DISTINCT ON (curve_id, position) curve_id, kind, position,"
        " as_of_date AS as_of, price, status, gap_reason, estimate_type, shape_source,"
        " method_version, shape_method_version FROM market.curve_points"
        " ORDER BY curve_id, position, as_of_date DESC"
    )
    curves: dict[str, dict[str, Any]] = {}
    for point in _rows(cur):
        curve_id, kind = point.pop("curve_id"), point.pop("kind")
        curves.setdefault(curve_id, {"curve_id": curve_id, "kind": kind, "points": []})
        curves[curve_id]["points"].append(point)
    for curve in curves.values():
        curve["points"].sort(key=lambda p: POSITIONS.index(p["position"]))
        curve["as_of"] = max(p["as_of"] for p in curve["points"])  # ISO dates sort as strings
    return [curves[k] for k in sorted(curves)]


def curve_exists(conn: psycopg.Connection, curve_id: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM market.curve_points WHERE curve_id = %s)", (curve_id,)
    ).fetchone()
    return bool(row and row[0])


def series_exists(conn: psycopg.Connection, series_id: str) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM market.observations WHERE series_id = %s)", (series_id,)
    ).fetchone()
    return bool(row and row[0])


def curve_history(
    conn: psycopg.Connection, curve_id: str, position: str, start: date | None, end: date | None
) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT as_of_date AS as_of, price, status, gap_reason, estimate_type"
        " FROM market.curve_points WHERE curve_id = %s AND position = %s"
        " AND as_of_date >= coalesce(%s, '-infinity'::date)"
        " AND as_of_date <= coalesce(%s, 'infinity'::date) ORDER BY as_of_date",
        (curve_id, position, start, end),
    )
    return _rows(cur)


def price_history(
    conn: psycopg.Connection, series_id: str, start: date | None, end: date | None
) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT observation_date AS date, price, unit, last_seen_at FROM market.observations"
        " WHERE series_id = %s AND observation_date >= coalesce(%s, '-infinity'::date)"
        " AND observation_date <= coalesce(%s, 'infinity'::date) ORDER BY observation_date",
        (series_id, start, end),
    )
    return _rows(cur)


def health(conn: psycopg.Connection, *, limit: int = 100) -> dict[str, Any]:
    """Published versions, failed and quarantined attempts, and dead outbox events."""
    versions = _rows(
        conn.execute(
            "SELECT dataset_version, logical_input_id, source, created_at, imported_at,"
            " observations, revisions, curve_points, price_changes FROM market.dataset_versions"
            " ORDER BY dataset_version DESC"
        )
    )
    attempt_counts: dict[str, int] = dict(
        conn.execute(
            "SELECT status, count(*) FROM market.pipeline_attempts GROUP BY status"
        ).fetchall()
    )
    problems = _rows(
        conn.execute(
            "SELECT attempt_id, logical_input_id, status, dataset_version, error, quality,"
            " finished_at FROM market.pipeline_attempts WHERE status IN ('failed', 'quarantined')"
            " ORDER BY finished_at DESC LIMIT %s",
            (limit,),
        )
    )
    outbox_counts: dict[str, int] = dict(
        conn.execute("SELECT status, count(*) FROM app.outbox_events GROUP BY status").fetchall()
    )
    dead = _rows(
        conn.execute(
            "SELECT event_id, event_type, dataset_version, attempts, last_error, processed_at"
            " FROM app.outbox_events WHERE status = 'dead' ORDER BY dataset_version LIMIT %s",
            (limit,),
        )
    )
    return {
        "versions": versions,
        "attempts": {"counts": attempt_counts, "failed_or_quarantined": problems},
        "outbox": {"counts": outbox_counts, "dead": dead},
    }
