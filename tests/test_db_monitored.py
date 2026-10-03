from datetime import date
from pathlib import Path

import polars as pl
import psycopg
import pytest
from db_support import FEB, JAN, T1, T2, ingest

from energy_curves.db import importer as imp
from energy_curves.db import outbox
from energy_curves.db.migrate import migrate
from energy_curves.db.monitored import (
    MonitoredValue,
    MonitoredValuesUnavailable,
    monitored_values,
)
from energy_curves.pipeline.publish import read_artifact, read_manifest
from energy_curves.storage.artifacts import LocalArtifactStore

pytestmark = pytest.mark.postgres


@pytest.fixture
def db(pg_dsn: str) -> str:
    migrate(pg_dsn)
    return pg_dsn


def distinct_versions_store(store: Path) -> Path:
    """v1 January, v2 February, v3 a correction to 29 February: every version has different
    latest curve points, so a handler reading another version's values is caught."""
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, FEB, retrieved_at=T1)
    ingest(store, FEB, retrieved_at=T2, overrides={("RWTC", date(2024, 2, 29)): "99.99"})
    return store


def expected_values(store_dir: Path) -> dict[int, dict[tuple[str, str], MonitoredValue]]:
    """Independent oracle: each version's own gold_curves artifact, latest point per curve and
    position."""
    store = LocalArtifactStore(store_dir)
    expected = {}
    for record in imp.published_versions(store):
        curves = read_artifact(store, read_manifest(store, record), "gold_curves")
        latest = (
            curves.with_columns(pl.col("position").cast(pl.Utf8))
            .sort("as_of_date", descending=True)
            .unique(["curve_id", "position"], keep="first")
        )
        expected[record.dataset_version] = {
            (r["curve_id"], r["position"]): MonitoredValue(
                r["curve_id"], r["position"], r["as_of_date"], r["price"], r["status"]
            )
            for r in latest.iter_rows(named=True)
        }
    return expected


def test_each_event_resolves_the_values_of_its_own_version(db: str, tmp_path: Path) -> None:
    """Finding 4: three versions are imported before anything is consumed. Each event's
    handler must see its own version's monitored values, not the latest snapshot."""
    store = distinct_versions_store(tmp_path / "store")
    imp.import_pending(db, store)
    expected = expected_values(store)
    assert expected[1] != expected[2] != expected[3] != expected[1]  # the test discriminates
    seen: dict[int, dict[tuple[str, str], MonitoredValue]] = {}

    def handler(conn: psycopg.Connection, event: outbox.OutboxEvent) -> None:
        seen[event.dataset_version] = monitored_values(conn, event.dataset_version)

    with psycopg.connect(db) as conn:
        assert outbox.process(conn, {"dataset_imported": handler}).done == 3
    for version in (1, 2, 3):
        assert seen[version] == expected[version], f"version {version}"


def test_unknown_versions_are_refused(db: str, tmp_path: Path) -> None:
    imp.import_pending(db, distinct_versions_store(tmp_path / "store"))
    with psycopg.connect(db) as conn, pytest.raises(MonitoredValuesUnavailable, match="4"):
        monitored_values(conn, 4)


def test_rebuild_reproduces_every_versions_values(db: str, tmp_path: Path) -> None:
    store = distinct_versions_store(tmp_path / "store")
    imp.import_pending(db, store)
    with psycopg.connect(db) as conn:
        before = {v: monitored_values(conn, v) for v in (1, 2, 3)}
    imp.rebuild_market(db, store)
    with psycopg.connect(db) as conn:
        assert {v: monitored_values(conn, v) for v in (1, 2, 3)} == before


def test_monitored_values_are_append_only(db: str, tmp_path: Path) -> None:
    imp.import_pending(db, distinct_versions_store(tmp_path / "store"))
    with psycopg.connect(db) as conn, pytest.raises(psycopg.errors.RaiseException, match="append"):
        conn.execute("UPDATE market.monitored_values SET price = 1 WHERE dataset_version = 1")


def test_versions_imported_before_the_table_existed_ask_for_a_rebuild(
    db: str, tmp_path: Path
) -> None:
    store = distinct_versions_store(tmp_path / "store")
    imp.import_pending(db, store)
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM market.monitored_values")  # as if imported before 0003
        with pytest.raises(MonitoredValuesUnavailable, match="--rebuild"):
            monitored_values(conn, 1)
    imp.rebuild_market(db, store)
    with psycopg.connect(db) as conn:
        assert monitored_values(conn, 1) == expected_values(store)[1]
