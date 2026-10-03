import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from db_support import FEB, JAN, T1, T2, ingest
from db_support import seed_version as seed

from energy_curves.db import alerts, outbox
from energy_curves.db import importer as imp
from energy_curves.db.migrate import migrate
from energy_curves.db.monitored import monitored_values
from energy_curves.storage.artifacts import LocalArtifactStore

pytestmark = pytest.mark.postgres
T0 = datetime(2030, 1, 1, tzinfo=UTC)
KEY = ("WTI", "Spot")


@pytest.fixture
def db(pg_dsn: str) -> str:
    migrate(pg_dsn)
    return pg_dsn


def rule(dsn: str, threshold: str, key: tuple[str, str] = KEY) -> uuid.UUID:
    with psycopg.connect(dsn) as conn:
        return alerts.create_rule(conn, key[0], key[1], Decimal(threshold))["rule_id"]  # type: ignore[no-any-return]


def run(dsn: str, now: datetime = T0) -> outbox.ProcessStats:
    with psycopg.connect(dsn) as conn:
        return outbox.process(conn, {"dataset_imported": alerts.evaluate}, now=now)


def fired(dsn: str) -> list[tuple[int, str, str | None]]:
    """(dataset_version, price, previous_price) in log order."""
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT dataset_version, price, previous_price FROM app.fired_alerts ORDER BY seq"
        ).fetchall()
    return [(v, str(p), None if pp is None else str(pp)) for v, p, pp in rows]


def walk(dsn: str, prices: list[str | None], threshold: str = "70", first: int = 1) -> None:
    """Seed versions first.. with WTI Spot prices, creating the rule after the first one."""
    seed(dsn, first, {KEY: prices[0]})
    rule(dsn, threshold)
    for i, price in enumerate(prices[1:], start=first + 1):
        seed(dsn, i, {KEY: price})
    run(dsn)


# --- ADR-0013 rules -------------------------------------------------------------------------------


def test_upward_crossing_fires_once_then_rearms_at_or_below(db: str) -> None:
    walk(db, ["65", "72", "75", "70", "70.0001"])
    assert fired(db) == [(2, "72.0000", "65.0000"), (5, "70.0001", "70.0000")]


def test_a_new_rule_above_the_threshold_takes_a_baseline_without_firing(db: str) -> None:
    walk(db, ["75", "76", "69", "71"])  # baseline 75: not armed; 69 re-arms; 71 fires
    assert fired(db) == [(4, "71.0000", "69.0000")]


def test_a_rule_created_before_any_data_never_fires_on_its_first_value(db: str) -> None:
    rule(db, "70")
    for version, price in enumerate(["80", "85", "60", "71"], start=1):
        seed(db, version, {KEY: price})
    run(db)
    assert fired(db) == [(4, "71.0000", "60.0000")]


def test_pending_events_at_or_below_the_baseline_are_not_evaluated(db: str) -> None:
    seed(db, 1, {KEY: "80"})
    seed(db, 2, {KEY: "65"})  # both events still pending when the rule is created
    rule(db, "70")
    seed(db, 3, {KEY: "72"})
    assert run(db).done == 3
    assert fired(db) == [(3, "72.0000", "65.0000")]


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        (["65", None, "72"], [(3, "72.0000", "65.0000")]),  # a gap does not disarm
        (["75", None, "72"], []),  # ... and does not re-arm
    ],
)
def test_gaps_neither_fire_nor_rearm(db: str, prices: list[str | None], expected: list) -> None:  # type: ignore[type-arg]
    walk(db, prices)
    assert fired(db) == expected


def test_a_missing_value_neither_fires_nor_rearms(db: str) -> None:
    seed(db, 1, {KEY: "75"})
    rule(db, "70")
    seed(db, 2, {("WTI", "C1"): "60"})  # WTI Spot absent
    seed(db, 3, {KEY: "72"})
    run(db)
    assert fired(db) == []


def test_watermark_only_versions_are_silent_and_price_changes_is_not_consulted(db: str) -> None:
    seed(db, 1, {KEY: "65"})
    rule(db, "70")
    seed(db, 2, {KEY: "65"}, changes=0)  # watermark only: unchanged value cannot cross
    seed(db, 3, {KEY: "72"}, changes=0)  # e.g. new shape parameters: a changed value fires
    run(db)
    assert fired(db) == [(3, "72.0000", "65.0000")]


def test_several_rules_fire_on_one_version_in_rule_order(db: str) -> None:
    seed(db, 1, {KEY: "65", ("WTI", "C1"): "66", ("BRENT", "Spot"): "60"})
    ids = [rule(db, "70"), rule(db, "70", ("WTI", "C1")), rule(db, "61", ("BRENT", "Spot"))]
    seed(db, 2, {KEY: "71", ("WTI", "C1"): "72", ("BRENT", "Spot"): "62"})
    run(db)
    with psycopg.connect(db) as conn:
        rows = conn.execute("SELECT seq, rule_id FROM app.fired_alerts ORDER BY seq").fetchall()
    assert [r[1] for r in rows] == ids
    assert [r[0] for r in rows] == list(range(rows[0][0], rows[0][0] + 3))  # consecutive


def test_deleted_rules_are_not_evaluated(db: str) -> None:
    seed(db, 1, {KEY: "65"})
    rule_id = rule(db, "70")
    with psycopg.connect(db) as conn:
        assert alerts.delete_rule(conn, rule_id) is True
        assert alerts.delete_rule(conn, rule_id) is False
        assert alerts.list_rules(conn) == []
    seed(db, 2, {KEY: "72"})
    run(db)
    assert fired(db) == []


def test_a_failing_evaluation_leaves_no_trace_and_the_retry_fires_exactly_once(
    db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity (ADR-0013): the first rule's alert and state change are rolled back with the
    failure of the second, and the retry produces each alert once."""
    seed(db, 1, {KEY: "65", ("WTI", "C1"): "66"})
    rule(db, "70")
    rule(db, "70", ("WTI", "C1"))
    seed(db, 2, {KEY: "71", ("WTI", "C1"): "72"})
    with psycopg.connect(db) as conn:
        before = conn.execute("SELECT * FROM app.alert_rules ORDER BY created_at").fetchall()
    original, calls = alerts.alert_id, []

    def flaky(rule_id: uuid.UUID, version: int) -> uuid.UUID:
        calls.append(rule_id)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return original(rule_id, version)

    monkeypatch.setattr(alerts, "alert_id", flaky)
    stats = run(db)
    assert (stats.done, stats.retried) == (1, 1)  # version 1 done; version 2 failed once
    assert fired(db) == []
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT * FROM app.alert_rules ORDER BY created_at").fetchall() == [
            tuple(r) for r in before
        ]
    assert run(db, now=T0 + timedelta(seconds=2)).done == 0  # backoff: not due yet
    assert run(db, now=T0 + timedelta(minutes=1)).done == 1
    assert [v for v, _, _ in fired(db)] == [2, 2]
    assert run(db, now=T0 + timedelta(minutes=2)).done == 0  # nothing left: never twice


def test_end_to_end_on_a_published_store_and_reimport_never_refires(
    db: str, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, FEB, retrieved_at=T1, overrides={("RWTC", date(2024, 2, 29)): "99.99"})
    s = LocalArtifactStore(store)
    with psycopg.connect(db) as conn:
        imp.import_version(conn, s, imp.published_versions(s)[0])
        baseline = monitored_values(conn, 1)[KEY].price
    assert baseline is not None and baseline < Decimal("90")
    rule(db, "90")
    imp.import_pending(db, store)
    assert alerts.process_events(db).done == 2
    assert fired(db) == [(2, "99.9900", str(baseline))]
    imp.import_pending(db, store)  # re-import: already imported, no event
    imp.rebuild_market(db, store)  # rebuild: events exist and are done
    assert alerts.process_events(db).done == 0
    assert len(fired(db)) == 1


# --- validation -----------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["abc", "NaN", "Infinity", "1e20", "1.23456", ""])
def test_bad_thresholds_are_refused(text: str) -> None:
    with pytest.raises(alerts.RuleError):
        alerts.parse_threshold(text)


def test_good_thresholds_including_negative_spreads() -> None:
    assert alerts.parse_threshold("-5.25") == Decimal("-5.25")
    assert alerts.parse_threshold("70") == Decimal("70")


def test_unknown_curve_or_position_is_refused(db: str) -> None:
    with psycopg.connect(db) as conn:
        with pytest.raises(alerts.RuleError, match="curve"):
            alerts.create_rule(conn, "GOLD", "Spot", Decimal(1))
        with pytest.raises(alerts.RuleError, match="position"):
            alerts.create_rule(conn, "WTI", "C9", Decimal(1))


# --- the log and cursor semantics (ADR-0015) ------------------------------------------------------


def state(floor: int = 0, head: int = 10) -> alerts.LogState:
    return alerts.LogState(uuid.UUID(int=7), floor, head)


@pytest.mark.parametrize(
    ("cursor", "expected"),
    [
        (None, alerts.Resume(10, None, fresh=True)),
        (f"{uuid.UUID(int=7).hex}-4", alerts.Resume(4, None, fresh=False)),
        (f"{uuid.UUID(int=7).hex}-10", alerts.Resume(10, None, fresh=False)),
        (f"{uuid.UUID(int=7).hex}-3", alerts.Resume(10, "expired_cursor", fresh=False)),
        (f"{uuid.UUID(int=7).hex}-11", alerts.Resume(10, "unknown_cursor", fresh=False)),
        (f"{uuid.UUID(int=8).hex}-4", alerts.Resume(10, "unknown_cursor", fresh=False)),
        ("garbage", alerts.Resume(10, "unknown_cursor", fresh=False)),
        (f"{uuid.UUID(int=7).hex}-04", alerts.Resume(10, "unknown_cursor", fresh=False)),
    ],
)
def test_cursor_resolution(cursor: str | None, expected: alerts.Resume) -> None:
    assert alerts.resolve(state(floor=4), cursor) == expected


def test_prune_deletes_a_prefix_and_raises_the_floor(db: str) -> None:
    walk(db, ["65", "72", "60", "72", "60", "72"])  # alerts at versions 2, 4, 6
    with psycopg.connect(db) as conn:
        seqs = [r[0] for r in conn.execute("SELECT seq FROM app.fired_alerts ORDER BY seq")]
        conn.execute("UPDATE app.fired_alerts SET fired_at = %s WHERE seq <= %s", (T1, seqs[1]))
        assert alerts.prune(conn, T2) == 2
        after = alerts.log_state(conn)
        assert (after.floor, after.head) == (seqs[1], seqs[2])
        assert alerts.prune(conn, T2) == 0
        conn.execute("DELETE FROM app.fired_alerts")  # an empty log keeps its floor and head
        assert alerts.log_state(conn).head == seqs[1]


# --- review findings (PR #7) ----------------------------------------------------------------------


def reseal(backup_dir: Path, table: str, old: bytes, new: bytes) -> None:
    """Edit a backed-up table and re-seal its checksum, so the restore gets past verification."""
    import json

    from energy_curves.db import backup as bk
    from energy_curves.storage.artifacts import sha256

    f = backup_dir / f"{table}.csv"
    f.write_bytes(f.read_bytes().replace(old, new, 1))
    m = backup_dir / bk.MANIFEST
    doc = json.loads(m.read_text())
    doc["tables"][table]["sha256"] = sha256(f.read_bytes())
    m.write_text(json.dumps(doc))


def test_f1_a_failed_restore_does_not_rewind_the_alert_sequence(db: str, tmp_path: Path) -> None:
    """Finding F1: setval is not transactional. A restore that fails after loading
    fired_alerts must not leave the sequence below alerts that still exist; otherwise the next
    alert collides on seq and its event retries until it is dead."""
    from energy_curves.db import backup as bk

    walk(db, ["65", "72"])  # one alert
    bk.backup(db, tmp_path / "bk")
    for version, price in ((3, "60"), (4, "72")):  # one more alert after the backup
        seed(db, version, {KEY: price})
    run(db)
    reseal(tmp_path / "bk", "outbox_events", b",done,", b",bogus,")  # fails after fired_alerts
    with pytest.raises(psycopg.errors.CheckViolation):
        bk.restore(db, tmp_path / "bk")
    seed(db, 5, {KEY: "60"})
    seed(db, 6, {KEY: "72"})
    stats = run(db, now=T0 + timedelta(minutes=1))
    assert stats.errors == []
    assert (stats.retried, stats.dead) == (0, 0)
    assert [v for v, _, _ in fired(db)] == [2, 4, 6]


@pytest.fixture
def fresh_db() -> Iterator[str]:
    from db_support import fresh_database

    for dsn in fresh_database():
        migrate(dsn)
        yield dsn


@pytest.mark.parametrize("target", ["same_database", "fresh_database"])
def test_f2_after_restoring_an_empty_pruned_log_new_alerts_stay_above_the_floor(
    db: str, fresh_db: str, tmp_path: Path, target: str
) -> None:
    """Finding F2: the restored log is empty but its floor says seq up to N were pruned.
    A new alert must get a seq above the floor, or every cursor (which starts at the floor)
    skips it and it is never streamed."""
    from energy_curves.db import backup as bk

    prices = ["65", "72", "60", "72"]  # alerts at versions 2 and 4
    walk(db, prices)
    with psycopg.connect(db) as conn:
        assert alerts.prune(conn, datetime.now(UTC) + timedelta(seconds=1)) == 2
        floor = alerts.log_state(conn).floor
    bk.backup(db, tmp_path / "bk")
    dsn = db if target == "same_database" else fresh_db
    if dsn == fresh_db:  # the same imported history, so the restore's reconciliation accepts it
        for version, price in enumerate(prices, start=1):
            seed(dsn, version, {KEY: price})
    bk.restore(dsn, tmp_path / "bk")
    seed(dsn, 5, {KEY: "60"})
    seed(dsn, 6, {KEY: "72"})
    run(dsn)
    with psycopg.connect(dsn) as conn:
        state = alerts.log_state(conn)
        visible = alerts.alerts_after(conn, state.floor)
    assert state.floor == floor
    assert [a["dataset_version"] for a in visible] == [6]
    assert visible[0]["seq"] > floor and state.head == visible[0]["seq"]
