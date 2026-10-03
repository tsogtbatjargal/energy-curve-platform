import asyncio
import json
import time
from pathlib import Path

import httpx
import psycopg
import pytest
import redis
from db_support import FEB, JAN, T1, ingest
from http_support import next_event, serve

from energy_curves.api import events as ev
from energy_curves.db import importer as imp
from energy_curves.db.migrate import migrate
from energy_curves.storage.artifacts import LocalArtifactStore

# --- the stream logic, with a scripted version source ---------------------------------------


def run_stream(versions: list[int | None], last_event_id: int | None) -> list[tuple[str, str]]:
    """Feed one version per poll; return the (id, data) of every emitted event."""

    class Done(Exception):
        pass

    async def collect() -> list[tuple[str, str]]:
        script = iter(versions)
        out: list[tuple[str, str]] = []

        async def current() -> int | None:
            for version in script:
                return version
            raise Done

        async def wait(_: float) -> None:
            return None

        stream = ev.dataset_events(current, wait, last_event_id, poll_s=0)
        try:
            async for event in stream:
                assert event.event == ev.EVENT
                out.append((event.id or "", event.raw_data or ""))
        except Done:
            pass
        return out

    return asyncio.run(collect())


def test_first_connect_gets_the_current_version() -> None:
    assert run_stream([3], None) == [("3", '{"dataset_version": 3}')]


def test_repeated_wakeups_for_one_version_emit_one_event() -> None:
    assert [i for i, _ in run_stream([3, 3, 3, 4, 4], None)] == ["3", "4"]


def test_reconnect_with_the_current_id_emits_nothing_until_it_changes() -> None:
    assert [i for i, _ in run_stream([3, 3, 4], 3)] == ["4"]


def test_reconnect_after_missed_versions_gets_the_current_one_at_once() -> None:
    assert [i for i, _ in run_stream([5, 5], 2)] == ["5"]  # coalesced: one event, re-fetch


def test_postgres_unavailable_emits_nothing_and_keeps_going() -> None:
    assert [i for i, _ in run_stream([None, None, 2], 1)] == ["2"]


def test_last_event_id_parsing() -> None:
    assert ev.parse_last_event_id("7") == 7
    assert ev.parse_last_event_id("x") is None and ev.parse_last_event_id(None) is None


# --- end to end: import, publish after commit, uvicorn, SSE ----------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Path:
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, FEB, retrieved_at=T1)
    return store


@pytest.fixture
def db(pg_dsn: str, store: Path) -> str:
    migrate(pg_dsn)
    s = LocalArtifactStore(store)
    with psycopg.connect(pg_dsn) as conn:
        imp.import_version(conn, s, imp.published_versions(s)[0])  # version 1 only
    return pg_dsn


def published_event_arrives(base: str, db: str, store: Path, notify, timeout: float) -> None:  # type: ignore[no-untyped-def]
    with (
        httpx.Client(timeout=timeout) as client,
        client.stream("GET", f"{base}/api/events") as response,
    ):
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.iter_lines()
        first = next_event(lines)
        assert (first["event"], first["id"]) == ("dataset_updated", "1")
        started = time.monotonic()
        imp.import_pending(db, store, on_committed=notify)
        second = next_event(lines)
        assert second["id"] == "2" and json.loads(second["data"]) == {"dataset_version": 2}
        assert time.monotonic() - started < timeout


@pytest.mark.postgres
def test_published_update_reaches_the_stream_through_valkey(
    db: str, store: Path, redis_url: str
) -> None:
    """Polling is set to a minute, so the event can only arrive through the publish that
    db-import makes after the commit."""
    client = redis.Redis.from_url(redis_url)
    with serve(db, redis_url, poll_s=60) as base:
        published_event_arrives(
            base, db, store, lambda v: ev.publish_dataset_updated(client, v), timeout=5
        )


@pytest.mark.postgres
def test_with_valkey_down_the_poll_still_delivers(db: str, store: Path) -> None:
    with serve(db, "redis://127.0.0.1:1/0", poll_s=0.3) as base:
        down = redis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
        published_event_arrives(
            base, db, store, lambda v: ev.publish_dataset_updated(down, v), timeout=5
        )


@pytest.mark.postgres
def test_reconnect_with_last_event_id_recovers_a_missed_version(db: str, store: Path) -> None:
    imp.import_pending(db, store)  # version 2 committed while the client was away
    with (
        serve(db, None, poll_s=60) as base,
        httpx.Client(timeout=5) as client,
        client.stream("GET", f"{base}/api/events", headers={"Last-Event-ID": "1"}) as r,
    ):
        assert next_event(r.iter_lines())["id"] == "2"


@pytest.mark.postgres
def test_on_committed_runs_after_the_commit(db: str, store: Path) -> None:
    seen: list[tuple[int, int | None]] = []

    def check(version: int) -> None:
        with psycopg.connect(db) as other:  # another session sees the version already
            row = other.execute("SELECT max(dataset_version) FROM market.dataset_versions")
            seen.append((version, row.fetchone()[0]))  # type: ignore[index]

    imp.import_pending(db, store, on_committed=check)
    imp.import_pending(db, store, on_committed=check)  # already imported: no notification
    assert seen == [(2, 2)]
    imp.rebuild_market(db, store, on_committed=check)
    assert seen == [(2, 2), (2, 2)]


def test_publish_reports_an_unreachable_valkey() -> None:
    down = redis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    assert ev.publish_dataset_updated(down, 1) is False


def test_publish_delivers_to_subscribers(redis_url: str) -> None:
    client = redis.Redis.from_url(redis_url)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(ev.CHANNEL)
    pubsub.get_message(timeout=1)  # subscription confirmation
    assert ev.publish_dataset_updated(client, 9) is True
    message = pubsub.get_message(timeout=2)
    assert message and json.loads(message["data"]) == {"type": ev.EVENT, "dataset_version": 9}
    pubsub.close()
