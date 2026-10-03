import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest
import redis
from db_support import seed_version
from fastapi.testclient import TestClient
from http_support import next_event, serve

from energy_curves.api import alerts as api_alerts
from energy_curves.api.app import create_app
from energy_curves.api.cache import VersionCache
from energy_curves.api.events import publish_alerts_wakeup
from energy_curves.db import alerts, outbox
from energy_curves.db import backup as bk
from energy_curves.db.migrate import migrate

BASE = "http://127.0.0.1:8000"
T0 = datetime(2030, 1, 1, tzinfo=UTC)
WTI, C1, C2 = ("WTI", "Spot"), ("WTI", "C1"), ("WTI", "C2")

# --- the stream logic, with a scripted log ------------------------------------------------------

EPOCH = uuid.UUID(int=7)


def scripted(snapshots: list[tuple[alerts.LogState, list[int]]], cursor: str | None) -> list:  # type: ignore[type-arg]
    """Run alert_stream over scripted reads; each read returns the next (state, seqs > position).
    Returns (event, id, data) for every emitted event."""

    class Done(Exception):
        pass

    async def collect() -> list:  # type: ignore[type-arg]
        script = iter(snapshots)
        out = []

        async def read(seq: int | None) -> tuple[alerts.LogState, list[dict]]:  # type: ignore[type-arg]
            for state, seqs in script:
                return state, [{"seq": s} for s in seqs if seq is not None and s > seq]
            raise Done

        async def wait(_: float) -> None:
            return None

        try:
            async for e in api_alerts.alert_stream(read, wait, cursor, poll_s=0):
                out.append((e.event, e.id, json.loads(e.raw_data or "{}")))
        except Done:
            pass
        return out

    return asyncio.run(collect())


def st(head: int, floor: int = 0, epoch: uuid.UUID = EPOCH) -> alerts.LogState:
    return alerts.LogState(epoch, floor, head)


def cid(seq: int, epoch: uuid.UUID = EPOCH) -> str:
    return f"{epoch.hex}-{seq}"


def test_stream_without_a_cursor_starts_at_the_head_and_says_so() -> None:
    events = scripted([(st(5), []), (st(7), [6, 7])], None)
    assert [(e, i) for e, i, _ in events] == [
        ("alerts_cursor", cid(5)),
        ("alert_fired", cid(6)),
        ("alert_fired", cid(7)),
    ]


def test_stream_with_a_valid_cursor_replays_in_order_without_a_marker() -> None:
    events = scripted([(st(5), []), (st(5), [3, 4, 5])], cid(2))
    assert [i for _, i, _ in events] == [cid(3), cid(4), cid(5)]


@pytest.mark.parametrize(
    ("cursor", "reason"),
    [(cid(9), "unknown_cursor"), ("nope", "unknown_cursor"), (cid(1), "expired_cursor")],
)
def test_stream_with_an_unknown_or_expired_cursor_resets_to_the_head(
    cursor: str, reason: str
) -> None:
    events = scripted([(st(5, floor=2), []), (st(6, floor=2), [6])], cursor)
    assert events[0] == ("alerts_reset", cid(5), {"cursor": cid(5), "reason": reason})
    assert [i for _, i, _ in events[1:]] == [cid(6)]


def test_a_restore_while_connected_resets_the_stream() -> None:
    other = uuid.UUID(int=8)
    events = scripted([(st(5), []), (st(5), []), (st(3, epoch=other), [])], cid(5))
    assert events == [("alerts_reset", cid(3, other), {"cursor": cid(3, other),
                                                        "reason": "unknown_cursor"})]  # fmt: skip


def test_a_prune_past_the_position_while_connected_resets_the_stream() -> None:
    events = scripted([(st(5), []), (st(8, floor=7), [8])], cid(5))
    assert events[0][0:2] == ("alerts_reset", cid(8)) and events[0][2]["reason"] == "expired_cursor"


def test_postgres_down_mid_stream_is_survived() -> None:
    class Down(psycopg.OperationalError):
        pass

    calls = []

    async def run() -> list[str]:
        async def read(seq: int | None):  # type: ignore[no-untyped-def]
            calls.append(seq)
            if len(calls) == 2:
                raise Down("gone")
            if len(calls) > 3:
                raise asyncio.CancelledError
            return st(6), [{"seq": 6}] if seq is not None and seq < 6 else []

        async def wait(_: float) -> None:
            return None

        out = []
        try:
            async for e in api_alerts.alert_stream(read, wait, cid(5), poll_s=0):
                out.append(e.id)
        except asyncio.CancelledError:
            pass
        return out

    assert asyncio.run(run()) == [cid(6)]


# --- HTTP: rules, CSRF, history ------------------------------------------------------------------


@pytest.fixture
def db(pg_dsn: str) -> str:
    migrate(pg_dsn)
    return pg_dsn


def client(db: str) -> TestClient:
    app = create_app(database_url=db, cache=VersionCache(None), redis_url=None, port=8000)
    return TestClient(app, base_url=BASE)


@pytest.mark.postgres
def test_rule_changes_need_the_csrf_token(db: str) -> None:
    c = client(db)
    body = {"curve_id": "WTI", "position": "Spot", "threshold": "70.5"}
    assert c.post("/api/alerts/rules", json=body).status_code == 403
    assert c.post("/api/alerts/rules", json=body, headers={"x-csrf-token": "x"}).status_code == 403
    token = c.get("/api/csrf").json()["token"]
    headers = {"x-csrf-token": token}
    cross = {**headers, "origin": "http://evil.example"}
    assert c.post("/api/alerts/rules", json=body, headers=cross).status_code == 403
    created = c.post("/api/alerts/rules", json=body, headers=headers)
    assert created.status_code == 201
    rule = created.json()
    assert (rule["threshold"], rule["baseline_version"], rule["armed"]) == ("70.5000", 0, None)
    assert [r["rule_id"] for r in c.get("/api/alerts/rules").json()["rules"]] == [rule["rule_id"]]
    path = f"/api/alerts/rules/{rule['rule_id']}"
    assert c.delete(path).status_code == 403
    assert c.delete(path, headers=headers).status_code == 204
    assert c.delete(path, headers=headers).status_code == 404
    assert c.get("/api/alerts/rules").json()["rules"] == []


@pytest.mark.postgres
@pytest.mark.parametrize(
    "body",
    [
        {"curve_id": "GOLD", "position": "Spot", "threshold": "1"},
        {"curve_id": "WTI", "position": "C9", "threshold": "1"},
        {"curve_id": "WTI", "position": "Spot", "threshold": "1.23456"},
        {"curve_id": "WTI", "position": "Spot", "threshold": 70.5},  # a float, not a decimal
        {"curve_id": "WTI", "position": "Spot", "threshold": "1", "extra": 1},
    ],
)
def test_invalid_rules_are_422(db: str, body: dict) -> None:  # type: ignore[type-arg]
    c = client(db)
    headers = {"x-csrf-token": c.get("/api/csrf").json()["token"]}
    assert c.post("/api/alerts/rules", json=body, headers=headers).status_code == 422


def fire_three(db: str) -> None:
    """Version 1 is the baseline for three rules; version 2 makes all three cross."""
    seed_version(db, 1, {WTI: "65", C1: "66", C2: "67"})
    with psycopg.connect(db) as conn:
        for key in (WTI, C1, C2):
            alerts.create_rule(conn, key[0], key[1], Decimal("70"))
    seed_version(db, 2, {WTI: "71", C1: "72", C2: "73"})


def head_cursor(db: str) -> str:
    with psycopg.connect(db) as conn:
        return alerts.log_state(conn).cursor()


@pytest.mark.postgres
def test_history_and_its_cursor(db: str) -> None:
    fire_three(db)
    before = head_cursor(db)
    alerts.process_events(db)
    c = client(db)
    latest = c.get("/api/alerts").json()
    assert [(a["curve_id"], a["position"], a["price"]) for a in latest["alerts"]] == [
        ("WTI", "Spot", "71.0000"), ("WTI", "C1", "72.0000"), ("WTI", "C2", "73.0000"),
    ]  # fmt: skip
    assert latest["cursor"] == head_cursor(db) and latest["reset"] is None
    first = latest["alerts"][0]["seq"]
    page = c.get("/api/alerts", params={"after": before, "limit": 2}).json()
    assert [a["seq"] for a in page["alerts"]] == [first, first + 1]
    rest = c.get("/api/alerts", params={"after": page["cursor"]}).json()
    assert [a["seq"] for a in rest["alerts"]] == [first + 2]
    unknown = c.get("/api/alerts", params={"after": "nope"}).json()
    assert (unknown["reset"], unknown["alerts"], unknown["cursor"]) == (
        "unknown_cursor", [], head_cursor(db)
    )  # fmt: skip


# --- end to end: real uvicorn, real streams ------------------------------------------------------

Stream = Iterator[str]


def open_stream(base: str, headers: dict[str, str] | None = None, after: str | None = None):  # type: ignore[no-untyped-def]
    c = httpx.Client(timeout=5)
    params = {"after": after} if after else None
    return c, c.stream("GET", f"{base}/api/alerts/events", headers=headers, params=params)


def read_events(base: str, n: int, **kw) -> list[dict[str, str]]:  # type: ignore[no-untyped-def]
    c, stream = open_stream(base, **kw)
    with c, stream as r:
        lines = r.iter_lines()
        return [next_event(lines) for _ in range(n)]


@pytest.mark.postgres
def test_several_alerts_from_one_version_with_a_disconnect_between_them(db: str) -> None:
    """The required reconnect regression: three alerts from version 2. The client sees the
    first, disconnects, and resumes with Last-Event-ID: it gets exactly the other two, in
    order. A later alert then follows them directly, so nothing was duplicated."""
    fire_three(db)
    start = head_cursor(db)
    alerts.process_events(db)
    with psycopg.connect(db) as conn:
        seqs = [r[0] for r in conn.execute("SELECT seq FROM app.fired_alerts ORDER BY seq")]
    epoch = start.split("-")[0]
    with serve(db, None, poll_s=0.2) as base:
        (first,) = read_events(base, 1, after=start)  # then the connection closes
        assert (first["event"], first["id"]) == ("alert_fired", f"{epoch}-{seqs[0]}")
        c, stream = open_stream(base, headers={"Last-Event-ID": first["id"]})
        with c, stream as r:
            lines = r.iter_lines()
            resumed = [next_event(lines) for _ in range(2)]
            assert [e["id"] for e in resumed] == [f"{epoch}-{s}" for s in seqs[1:]]
            assert [json.loads(e["data"])["position"] for e in resumed] == ["C1", "C2"]
            seed_version(db, 3, {WTI: "60", C1: "60", C2: "60"})  # re-arm all three
            seed_version(db, 4, {WTI: "75", C1: "60", C2: "60"})  # one more crossing
            alerts.process_events(db)
            later = next_event(lines)
            assert later["event"] == "alert_fired" and int(later["id"].split("-")[1]) > seqs[2]
            assert json.loads(later["data"])["dataset_version"] == 4


@pytest.mark.postgres
def test_alerts_fired_while_connected_arrive_live_in_order(db: str, redis_url: str) -> None:
    """Polling is a minute, so these can only arrive through the after-commit wake-up."""
    fire_three(db)
    client = redis.Redis.from_url(redis_url)
    with serve(db, redis_url, poll_s=60) as base:
        c, stream = open_stream(base)
        with c, stream as r:
            lines = r.iter_lines()
            assert next_event(lines)["event"] == "alerts_cursor"
            alerts.process_events(db, lambda: publish_alerts_wakeup(client))
            live = [next_event(lines) for _ in range(3)]
            assert [json.loads(e["data"])["position"] for e in live] == ["Spot", "C1", "C2"]


@pytest.mark.postgres
def test_with_valkey_down_alerts_still_arrive_by_polling(db: str) -> None:
    fire_three(db)
    down = redis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    with serve(db, "redis://127.0.0.1:1/0", poll_s=0.3) as base:
        c, stream = open_stream(base)
        with c, stream as r:
            lines = r.iter_lines()
            assert next_event(lines)["event"] == "alerts_cursor"
            alerts.process_events(db, lambda: publish_alerts_wakeup(down))  # publish fails
            assert len([next_event(lines) for _ in range(3)]) == 3


@pytest.mark.postgres
def test_a_failed_evaluation_and_its_retry_deliver_each_alert_once(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fire_three(db)
    original, calls = alerts.alert_id, []

    def flaky(rule_id: uuid.UUID, version: int) -> uuid.UUID:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk full")  # after the first alert of the version was written
        return original(rule_id, version)

    monkeypatch.setattr(alerts, "alert_id", flaky)
    with serve(db, None, poll_s=0.2) as base:
        c, stream = open_stream(base)
        with c, stream as r:
            lines = r.iter_lines()
            assert next_event(lines)["event"] == "alerts_cursor"
            with psycopg.connect(db) as conn:
                handlers = {"dataset_imported": alerts.evaluate}
                assert outbox.process(conn, handlers, now=T0).retried == 1  # rolled back
                assert outbox.process(conn, handlers, now=T0 + timedelta(minutes=1)).done == 1
            got = [next_event(lines) for _ in range(3)]
            ids = [e["id"] for e in got]
            assert len(set(ids)) == 3
            assert [json.loads(e["data"])["position"] for e in got] == ["Spot", "C1", "C2"]
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM app.fired_alerts").fetchone() == (3,)


@pytest.mark.postgres
def test_unknown_cursor_end_to_end(db: str) -> None:
    fire_three(db)
    alerts.process_events(db)
    with serve(db, None, poll_s=0.2) as base:
        for bad in ("garbage", f"{uuid.uuid4().hex}-1", f"{head_cursor(db).split('-')[0]}-999"):
            (reset,) = read_events(base, 1, headers={"Last-Event-ID": bad})
            assert reset["event"] == "alerts_reset" and reset["id"] == head_cursor(db)
            assert json.loads(reset["data"])["reason"] == "unknown_cursor"


@pytest.mark.postgres
def test_expired_cursor_end_to_end(db: str) -> None:
    fire_three(db)
    start = head_cursor(db)
    alerts.process_events(db)
    with psycopg.connect(db) as conn:
        assert alerts.prune(conn, datetime.now(UTC) + timedelta(seconds=1)) == 3
    with serve(db, None, poll_s=0.2) as base:
        (reset,) = read_events(base, 1, headers={"Last-Event-ID": start})
        assert json.loads(reset["data"])["reason"] == "expired_cursor"
        assert reset["id"] == head_cursor(db)


@pytest.mark.postgres
def test_a_cursor_from_before_a_restore_is_unknown_end_to_end(db: str, tmp_path: Path) -> None:
    fire_three(db)
    alerts.process_events(db)
    bk.backup(db, tmp_path / "bk")
    seen = head_cursor(db)
    bk.restore(db, tmp_path / "bk")  # same alerts, new epoch
    with serve(db, None, poll_s=0.2) as base:
        (reset,) = read_events(base, 1, headers={"Last-Event-ID": seen})
        assert json.loads(reset["data"])["reason"] == "unknown_cursor"
        assert reset["id"] == head_cursor(db) != seen
