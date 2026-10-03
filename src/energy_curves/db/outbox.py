"""Transactional outbox consumer (ADR-0013).

Events are processed in dataset-version order per event type. One consumer per type runs at a
time (transaction-scoped advisory lock); the head event is locked `FOR UPDATE`, and the handler's
database effects commit in the same transaction that marks the event `done`. A handler error
rolls its effects back (savepoint), counts an attempt, and schedules a retry with exponential
backoff; after `max_attempts` the event is `dead` and later events proceed. Delivery is
at-least-once with atomic effects, so a crash at any point loses nothing and duplicates nothing.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg

from energy_curves.logging_setup import redact_text

BASE_BACKOFF = timedelta(seconds=5)
MAX_BACKOFF = timedelta(minutes=15)


@dataclass(frozen=True)
class OutboxEvent:
    event_id: Any
    event_type: str
    dataset_version: int
    payload: dict[str, Any]
    attempts: int


Handler = Callable[[psycopg.Connection, OutboxEvent], None]


@dataclass
class ProcessStats:
    done: int = 0
    retried: int = 0
    dead: int = 0
    errors: list[str] = field(default_factory=list)


def backoff(attempts: int) -> timedelta:
    return min(MAX_BACKOFF, BASE_BACKOFF * 2 ** max(0, attempts - 1))


def _type_lock(event_type: str) -> int:
    return 7_212_400_000 + zlib.crc32(event_type.encode()) % 100_000


def process(
    conn: psycopg.Connection,
    handlers: Mapping[str, Handler],
    *,
    max_attempts: int = 5,
    now: datetime | None = None,
    limit: int = 1000,
) -> ProcessStats:
    """Process due events for every handled type, oldest version first."""
    stats = ProcessStats()
    for event_type, handler in handlers.items():
        for _ in range(limit):
            if not _step(conn, event_type, handler, max_attempts, now, stats):
                break
    return stats


def _step(
    conn: psycopg.Connection,
    event_type: str,
    handler: Handler,
    max_attempts: int,
    now: datetime | None,
    stats: ProcessStats,
) -> bool:
    """Handle the head event of one type. Returns False when there is nothing due to do."""
    with conn.transaction():
        locked = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s)", (_type_lock(event_type),)
        ).fetchone()
        if not (locked and locked[0]):
            return False  # another consumer owns this type right now
        row = conn.execute(
            "SELECT event_id, dataset_version, payload, attempts, next_attempt_at"
            " FROM app.outbox_events WHERE status = 'pending' AND event_type = %s"
            " ORDER BY dataset_version, created_at LIMIT 1 FOR UPDATE",
            (event_type,),
        ).fetchone()
        if row is None:
            return False
        clock = now or conn.execute("SELECT now()").fetchone()[0]  # type: ignore[index]
        if row[4] > clock:
            return False  # head-of-line: later versions wait for the head's retry
        event = OutboxEvent(row[0], event_type, row[1], row[2], row[3])
        try:
            with conn.transaction():  # savepoint: a failing handler leaves no effects
                handler(conn, event)
        except Exception as exc:  # noqa: BLE001 - every handler failure is recorded and retried
            attempts = event.attempts + 1
            message = redact_text(f"{type(exc).__name__}: {exc}", [])[:2000]
            if attempts >= max_attempts:
                conn.execute(
                    "UPDATE app.outbox_events SET status = 'dead', attempts = %s,"
                    " last_error = %s, processed_at = %s WHERE event_id = %s",
                    (attempts, message, clock, event.event_id),
                )
                stats.dead += 1
            else:
                conn.execute(
                    "UPDATE app.outbox_events SET attempts = %s, last_error = %s,"
                    " next_attempt_at = %s WHERE event_id = %s",
                    (attempts, message, clock + backoff(attempts), event.event_id),
                )
                stats.retried += 1
            stats.errors.append(message)
            return attempts >= max_attempts  # a dead head no longer blocks later events
        conn.execute(
            "UPDATE app.outbox_events SET status = 'done', attempts = attempts + 1,"
            " processed_at = %s, last_error = NULL WHERE event_id = %s",
            (clock, event.event_id),
        )
        stats.done += 1
        return True


def counts(conn: psycopg.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, count(*) FROM app.outbox_events GROUP BY status").fetchall()
    return {status: int(n) for status, n in rows}
