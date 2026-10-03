"""Threshold alerts: rules, evaluation as an outbox handler, and the alert log (ADR-0015).

Evaluation consumes `dataset_imported` events in version order and reads each event's own
monitored values (ADR-0013, F4). Rule state, fired alerts and the event's `done` marker commit
together. Fired alerts form the durable, ordered alert log: `seq` is assigned under the
alert-log lock, so commit order is `seq` order and a reader never sees a later alert while an
earlier one is still uncommitted. Cursors are `<epoch>-<seq>`; the epoch names the log's history
and is rotated by a restore, so a cursor from elsewhere is detectably unknown.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

from energy_curves.catalog import POSITIONS
from energy_curves.curves.engine import load_config
from energy_curves.db import outbox
from energy_curves.db.monitored import monitored_values

ALERT_LOG_LOCK = 7_212_500_001
ALERT_NAMESPACE = uuid.UUID("0b6f3f1e-9c2d-4a51-8e7b-3d2a6c1f5e90")
MAX_THRESHOLD = Decimal("1e14")  # numeric(18,4)
CURSOR = re.compile(r"^([0-9a-f]{32})-(0|[1-9][0-9]{0,18})$")


class RuleError(ValueError):
    pass


def curve_ids() -> frozenset[str]:
    config = load_config()
    return frozenset(c["id"] for c in [*config.outrights, *config.spreads])


def parse_threshold(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError) as exc:
        raise RuleError(f"threshold {text!r} is not a decimal number") from exc
    if not value.is_finite() or abs(value) >= MAX_THRESHOLD:
        raise RuleError(f"threshold {text!r} is out of range")
    if value.as_tuple().exponent < -4:  # type: ignore[operator]
        raise RuleError(f"threshold {text!r} has more than 4 decimal places")
    return value


def alert_id(rule_id: uuid.UUID, dataset_version: int) -> uuid.UUID:
    return uuid.uuid5(ALERT_NAMESPACE, f"{rule_id}:{dataset_version}")


def _rows(cur: psycopg.Cursor[Any]) -> list[dict[str, Any]]:
    names = [c.name for c in cur.description or []]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


# --- rules --------------------------------------------------------------------------------------

RULE_COLUMNS = (
    "rule_id, curve_id, position, threshold, created_at, baseline_version, armed, last_value,"
    " last_as_of, last_version"
)


def create_rule(
    conn: psycopg.Connection, curve_id: str, position: str, threshold: Decimal
) -> dict[str, Any]:
    """Create a rule whose baseline is the current version's monitored value. One statement,
    so the version and the value come from one snapshot. A new rule never fires on versions up
    to its baseline (ADR-0013: new rules establish a baseline without firing)."""
    if curve_id not in curve_ids():
        raise RuleError(f"unknown curve {curve_id!r}")
    if position not in POSITIONS:
        raise RuleError(f"unknown position {position!r}")
    with conn.transaction():
        cur = conn.execute(
            "INSERT INTO app.alert_rules (rule_id, curve_id, position, threshold,"
            " baseline_version, armed, last_value, last_as_of, last_version)"
            " SELECT %(id)s, %(curve)s, %(pos)s, %(t)s, coalesce(v.dv, 0),"
            "  CASE WHEN m.status = 'ok' THEN m.price <= %(t)s END,"
            "  CASE WHEN m.status = 'ok' THEN m.price END,"
            "  CASE WHEN m.status = 'ok' THEN m.as_of_date END,"
            "  CASE WHEN m.status = 'ok' THEN v.dv END"
            " FROM (SELECT max(dataset_version) AS dv FROM market.dataset_versions) v"
            " LEFT JOIN market.monitored_values m ON m.dataset_version = v.dv"
            "  AND m.curve_id = %(curve)s AND m.position = %(pos)s"
            " RETURNING rule_id, curve_id, position, threshold, created_at, baseline_version,"
            " armed, last_value, last_as_of, last_version",
            {"id": uuid.uuid4(), "curve": curve_id, "pos": position, "t": threshold},
        )
        return _rows(cur)[0]


def list_rules(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            f"SELECT {RULE_COLUMNS} FROM app.alert_rules WHERE deleted_at IS NULL"  # noqa: S608
            " ORDER BY created_at, rule_id"
        )
    )


def delete_rule(conn: psycopg.Connection, rule_id: uuid.UUID) -> bool:
    """Soft delete: fired alerts keep their rule. Returns False if there was no live rule."""
    with conn.transaction():
        cur = conn.execute(
            "UPDATE app.alert_rules SET deleted_at = now()"
            " WHERE rule_id = %s AND deleted_at IS NULL",
            (rule_id,),
        )
        return cur.rowcount == 1


# --- evaluation (an outbox handler for dataset_imported) ----------------------------------------


def evaluate(conn: psycopg.Connection, event: outbox.OutboxEvent) -> None:
    """Apply one dataset version to every live rule (ADR-0015 state machine). Runs inside the
    outbox transaction: its effects commit with the event's `done` marker or not at all."""
    version = event.dataset_version
    values = monitored_values(conn, version)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (ALERT_LOG_LOCK,))
    rules = conn.execute(
        "SELECT rule_id, curve_id, position, threshold, armed, last_value FROM app.alert_rules"
        " WHERE deleted_at IS NULL AND baseline_version < %s"
        " AND (last_version IS NULL OR last_version < %s)"
        " ORDER BY created_at, rule_id FOR UPDATE",
        (version, version),
    ).fetchall()
    for rule_id, curve_id, position, threshold, armed, last_value in rules:
        value = values.get((curve_id, position))
        if value is None or value.price is None:
            continue  # missing or a gap: neither fires nor re-arms
        if armed and value.price > threshold:
            conn.execute(
                "INSERT INTO app.fired_alerts (alert_id, rule_id, dataset_version, curve_id,"
                " position, as_of, price, previous_price, threshold)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    alert_id(rule_id, version), rule_id, version, curve_id, position,
                    value.as_of_date, value.price, last_value, threshold,
                ),
            )  # fmt: skip
        conn.execute(
            "UPDATE app.alert_rules SET armed = %s, last_value = %s, last_as_of = %s,"
            " last_version = %s WHERE rule_id = %s",
            (value.price <= threshold, value.price, value.as_of_date, version, rule_id),
        )


def process_events(dsn: str, on_done: Callable[[], object] | None = None) -> outbox.ProcessStats:
    """One consumer pass over pending dataset_imported events. `on_done` runs after the pass if
    any event was completed (for example, to wake alert streams); it never runs mid-transaction.
    """
    with psycopg.connect(dsn) as conn:
        stats = outbox.process(conn, {"dataset_imported": evaluate})
    if on_done and stats.done:
        on_done()
    return stats


# --- the alert log and its cursor ---------------------------------------------------------------


@dataclass(frozen=True)
class LogState:
    epoch: uuid.UUID
    floor: int
    head: int  # highest seq ever retained or pruned

    def cursor(self, seq: int | None = None) -> str:
        return f"{self.epoch.hex}-{self.head if seq is None else seq}"


@dataclass(frozen=True)
class Resume:
    start: int  # stream alerts with seq > start
    reset: str | None  # unknown_cursor | expired_cursor | None
    fresh: bool  # no cursor was given


def log_state(conn: psycopg.Connection) -> LogState:
    row = conn.execute(
        "SELECT epoch, floor, greatest(floor, coalesce((SELECT max(seq) FROM app.fired_alerts),"
        " 0)) FROM app.alert_log"
    ).fetchone()
    if row is None:
        raise RuntimeError("app.alert_log is empty; run db-migrate")
    return LogState(row[0], int(row[1]), int(row[2]))


def resolve(state: LogState, cursor: str | None) -> Resume:
    """Where a stream starts, given a client cursor (ADR-0015, cursor semantics)."""
    if cursor is None or cursor == "":
        return Resume(state.head, None, fresh=True)
    match = CURSOR.match(cursor)
    if match is None or match.group(1) != state.epoch.hex or int(match.group(2)) > state.head:
        return Resume(state.head, "unknown_cursor", fresh=False)
    seq = int(match.group(2))
    if seq < state.floor:
        return Resume(state.head, "expired_cursor", fresh=False)
    return Resume(seq, None, fresh=False)


def alerts_after(conn: psycopg.Connection, seq: int, limit: int = 100) -> list[dict[str, Any]]:
    return _rows(
        conn.execute(
            "SELECT seq, alert_id, rule_id, dataset_version, curve_id, position, as_of, price,"
            " previous_price, threshold, fired_at FROM app.fired_alerts WHERE seq > %s"
            " ORDER BY seq LIMIT %s",
            (seq, limit),
        )
    )


def prune(conn: psycopg.Connection, before: datetime) -> int:
    """Delete alerts fired before `before`, as a prefix of the log, and raise the floor, in one
    transaction. Returns how many were deleted."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ALERT_LOG_LOCK,))
        row = conn.execute(
            "SELECT max(seq) FROM app.fired_alerts WHERE fired_at < %s", (before,)
        ).fetchone()
        cutoff = row[0] if row else None
        if cutoff is None:
            return 0
        deleted = conn.execute("DELETE FROM app.fired_alerts WHERE seq <= %s", (cutoff,)).rowcount
        conn.execute("UPDATE app.alert_log SET floor = greatest(floor, %s)", (cutoff,))
        return deleted


def rotate_epoch(conn: psycopg.Connection) -> None:
    """A restored log may reuse seq values clients have seen: invalidate every old cursor."""
    conn.execute("UPDATE app.alert_log SET epoch = gen_random_uuid()")
