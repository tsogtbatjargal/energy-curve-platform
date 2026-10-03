import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from db_support import three_version_store

from energy_curves.db import importer as imp
from energy_curves.db import outbox
from energy_curves.db.migrate import migrate

pytestmark = pytest.mark.postgres
T0 = datetime(2030, 1, 1, tzinfo=UTC)  # explicit clock: retries are scheduled from it


@pytest.fixture
def db(pg_dsn: str, tmp_path: Path) -> str:
    migrate(pg_dsn)
    imp.import_pending(pg_dsn, three_version_store(tmp_path / "store"))  # 3 pending events
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("CREATE TABLE app.test_effects (dataset_version int PRIMARY KEY)")
    return pg_dsn


def q(dsn: str, query: str, params: tuple = ()) -> list[tuple]:  # type: ignore[type-arg]
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def record(conn: psycopg.Connection, event: outbox.OutboxEvent) -> None:
    conn.execute("INSERT INTO app.test_effects VALUES (%s)", (event.dataset_version,))


def run(dsn: str, handler, now: datetime = T0, **kw) -> outbox.ProcessStats:  # type: ignore[no-untyped-def]
    with psycopg.connect(dsn) as conn:
        return outbox.process(conn, {"dataset_imported": handler}, now=now, **kw)


def test_events_survive_a_crash_after_import_commit(db: str) -> None:
    # The import committed and the process "crashed" before any consumer ran: events are durable.
    assert q(db, "SELECT count(*) FROM app.outbox_events WHERE status = 'pending'") == [(3,)]
    stats = run(db, record)
    assert stats.done == 3
    assert q(db, "SELECT dataset_version FROM app.test_effects ORDER BY 1") == [(1,), (2,), (3,)]
    assert q(db, "SELECT count(*) FROM app.outbox_events WHERE status = 'done'") == [(3,)]


def test_failed_handler_effects_roll_back_and_retry_later(db: str) -> None:
    calls: list[int] = []

    def flaky(conn: psycopg.Connection, event: outbox.OutboxEvent) -> None:
        record(conn, event)
        calls.append(event.dataset_version)
        if len(calls) == 1:
            raise RuntimeError("transient")

    stats = run(db, flaky)
    assert (stats.done, stats.retried) == (0, 1)  # head failed: later versions wait behind it
    assert q(db, "SELECT count(*) FROM app.test_effects") == [(0,)]  # its write rolled back
    assert q(
        db,
        "SELECT attempts, next_attempt_at > %s FROM app.outbox_events WHERE dataset_version = 1",
        (T0,),
    ) == [(1, True)]
    assert run(db, flaky, now=T0 + timedelta(seconds=1)).done == 0  # retry not yet due
    stats = run(db, flaky, now=T0 + outbox.backoff(1))
    assert stats.done == 3
    assert q(db, "SELECT dataset_version FROM app.test_effects ORDER BY 1") == [(1,), (2,), (3,)]


def test_poison_event_goes_dead_and_later_events_proceed(db: str) -> None:
    def poison_v1(conn: psycopg.Connection, event: outbox.OutboxEvent) -> None:
        if event.dataset_version == 1:
            raise ValueError("cannot evaluate")
        record(conn, event)

    now = T0
    for attempt in range(1, 4):
        run(db, poison_v1, now=now, max_attempts=3)
        now += outbox.backoff(attempt)
    assert q(db, "SELECT dataset_version, status, attempts FROM app.outbox_events ORDER BY 1") == [
        (1, "dead", 3),
        (2, "done", 1),
        (3, "done", 1),
    ]
    assert q(db, "SELECT last_error FROM app.outbox_events WHERE dataset_version = 1") == [
        ("ValueError: cannot evaluate",)
    ]


def test_events_are_handled_in_version_order(db: str) -> None:
    seen: list[int] = []
    run(db, lambda conn, e: seen.append(e.dataset_version))
    assert seen == [1, 2, 3]


def test_concurrent_consumers_handle_each_event_exactly_once_in_order(db: str) -> None:
    barrier, errors = threading.Barrier(4), []

    def consume() -> None:
        barrier.wait()
        try:
            for _ in range(5):
                run(db, record)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=consume) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []  # a duplicate would violate test_effects' primary key
    assert q(db, "SELECT dataset_version FROM app.test_effects ORDER BY 1") == [(1,), (2,), (3,)]
    assert q(db, "SELECT count(*) FROM app.outbox_events WHERE status = 'done'") == [(3,)]


def test_errors_are_stored_redacted(db: str) -> None:
    def leaky(conn: psycopg.Connection, event: outbox.OutboxEvent) -> None:
        raise RuntimeError("GET https://api.eia.gov/v2/x?api_key=SECRETKEY123&frequency=daily")

    run(db, leaky)
    (error,) = q(db, "SELECT last_error FROM app.outbox_events WHERE dataset_version = 1")[0]
    assert "SECRETKEY123" not in error and "api_key=REDACTED" in error
