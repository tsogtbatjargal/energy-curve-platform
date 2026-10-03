import json
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from db_support import FEB_ROWS, JAN, JAN_ROWS, T1, T2, T3, ingest, three_version_store

from energy_curves.db import importer as imp
from energy_curves.db.migrate import migrate
from energy_curves.pipeline.publish import IntegrityError, Pointer, dumps
from energy_curves.storage.artifacts import LocalArtifactStore, sha256

pytestmark = pytest.mark.postgres


@pytest.fixture
def db(pg_dsn: str) -> str:
    migrate(pg_dsn)
    return pg_dsn


def q(dsn: str, query: str, params: tuple = ()) -> list[tuple]:  # type: ignore[type-arg]
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()


def market_snapshot(dsn: str) -> dict[str, list[tuple]]:  # type: ignore[type-arg]
    return {
        t: q(dsn, f"SELECT * FROM market.{t} ORDER BY 1, 2, 3")  # noqa: S608
        for t in ("observations", "revisions", "curve_points", "dataset_versions")
    }


def test_imports_every_version_in_order_with_an_outbox_event_each(db: str, tmp_path: Path) -> None:
    store = three_version_store(tmp_path / "store")
    results, attempts = imp.import_pending(db, store)
    assert [(r.dataset_version, r.status) for r in results] == [
        (1, "imported"),
        (2, "imported"),
        (3, "imported"),
    ]
    assert attempts == 3
    assert q(db, "SELECT count(*) FROM market.observations") == [(JAN_ROWS + FEB_ROWS,)]
    assert q(
        db,
        "SELECT price FROM market.observations WHERE series_id = 'RWTC'"
        " AND observation_date = '2024-01-10'",
    )[0][0] == Decimal("99.99")
    assert q(db, "SELECT count(*) FROM market.revisions") == [(1,)]
    assert q(db, "SELECT count(*) > 0, bool_and(dataset_version = 3) FROM market.curve_points") == [
        (True, True)
    ]
    assert q(db, "SELECT dataset_version, status FROM app.outbox_events ORDER BY 1") == [
        (1, "pending"),
        (2, "pending"),
        (3, "pending"),
    ]


def test_reimport_is_a_no_op(db: str, tmp_path: Path) -> None:
    store = three_version_store(tmp_path / "store")
    imp.import_pending(db, store)
    before = market_snapshot(db)
    results, attempts = imp.import_pending(db, store)
    assert {r.status for r in results} == {"already_imported"} and attempts == 0
    assert market_snapshot(db) == before
    assert q(db, "SELECT count(*) FROM app.outbox_events") == [(3,)]


def test_conflicting_content_for_an_imported_version_is_refused(db: str, tmp_path: Path) -> None:
    imp.import_pending(db, three_version_store(tmp_path / "a"))
    before = market_snapshot(db)
    other = tmp_path / "b"  # same data, but a different publication (different manifest)
    ingest(other, JAN, retrieved_at=T1)
    with pytest.raises(imp.ConflictingDataset, match="version 1"):
        imp.import_pending(db, other)
    assert market_snapshot(db) == before


def test_out_of_order_import_is_refused(db: str, tmp_path: Path) -> None:
    store = LocalArtifactStore(three_version_store(tmp_path / "store"))
    v2 = imp.published_versions(store)[1]
    with psycopg.connect(db) as conn, pytest.raises(imp.OutOfOrderImport, match="before 1"):
        imp.import_version(conn, store, v2)
    assert q(db, "SELECT count(*) FROM market.dataset_versions") == [(0,)]


def test_concurrent_imports_import_each_version_exactly_once(db: str, tmp_path: Path) -> None:
    store = three_version_store(tmp_path / "store")
    barrier, outcomes, errors = threading.Barrier(4), [], []

    def run() -> None:
        barrier.wait()
        try:
            outcomes.extend(imp.import_pending(db, store)[0])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    imported = sorted(r.dataset_version for r in outcomes if r.status == "imported")
    assert imported == [1, 2, 3]
    assert q(db, "SELECT count(*) FROM app.outbox_events") == [(3,)]


def test_tampered_artifact_rolls_the_version_back(db: str, tmp_path: Path) -> None:
    store_dir = three_version_store(tmp_path / "store")
    store = LocalArtifactStore(store_dir)
    manifest = json.loads(store.get(imp.published_versions(store)[1].manifest_key))
    gold = store_dir / manifest["artifacts"]["gold_current"]["key"]
    gold.write_bytes(gold.read_bytes() + b"x")
    with pytest.raises(IntegrityError):
        imp.import_pending(db, store_dir)
    assert q(db, "SELECT dataset_version FROM market.dataset_versions") == [(1,)]
    assert q(db, "SELECT count(*) FROM app.outbox_events") == [(1,)]


def test_staging_validation_rejects_a_manifest_row_count_mismatch(db: str, tmp_path: Path) -> None:
    store_dir = three_version_store(tmp_path / "store")
    store = LocalArtifactStore(store_dir)
    record = imp.published_versions(store)[0]
    manifest = json.loads(store.get(record.manifest_key))
    manifest["artifacts"]["gold_current"]["rows"] += 1  # a publisher bug, hashes re-sealed
    store.put(record.manifest_key, dumps(manifest))
    sealed = Pointer(
        1, record.logical_input_id, record.manifest_key, sha256(store.get(record.manifest_key))
    )
    store.put("published/versions/00000001.json", dumps(sealed.__dict__))
    with pytest.raises(imp.StagingValidationError, match="manifest says"):
        imp.import_pending(db, store_dir)
    assert q(db, "SELECT count(*) FROM market.dataset_versions") == [(0,)]


def test_a_second_source_is_refused(db: str, tmp_path: Path) -> None:
    store_dir = three_version_store(tmp_path / "store")
    with psycopg.connect(db) as conn:
        conn.execute(
            "INSERT INTO market.dataset_versions VALUES (1, 'x', 'y', 'eia', now(), now(),"
            " 0, 0, 0, 0)"
        )
    store = LocalArtifactStore(store_dir)
    with psycopg.connect(db) as conn, pytest.raises(imp.MixedSourceImport):
        imp.import_version(conn, store, imp.published_versions(store)[1])


def test_watermark_only_version_imports_without_revisions(db: str, tmp_path: Path) -> None:
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, JAN, retrieved_at=T3)  # same prices, later retrieval: watermark only
    imp.import_pending(db, store)
    assert q(db, "SELECT price_changes FROM market.dataset_versions ORDER BY dataset_version") == [
        (JAN_ROWS,),
        (0,),
    ]
    assert q(db, "SELECT count(*) FROM market.revisions") == [(0,)]
    assert q(db, "SELECT bool_and(last_seen_at = %s) FROM market.observations", (T3,)) == [(True,)]
    assert q(db, "SELECT count(*) FROM app.outbox_events") == [(2,)]  # evaluation decides silence


def test_failed_and_quarantined_attempts_are_synced(db: str, tmp_path: Path) -> None:
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, JAN, retrieved_at=T2, overrides={("RWTC", date(2024, 1, 3)): "NA"})
    imp.import_pending(db, store)
    statuses = sorted(r[0] for r in q(db, "SELECT status FROM market.pipeline_attempts"))
    assert statuses == ["published", "quarantined"]
    quality = q(
        db, "SELECT quality->>'status' FROM market.pipeline_attempts WHERE status = 'quarantined'"
    )
    assert quality == [("quarantined",)]
    assert imp.import_pending(db, store)[1] == 0  # attempt files are immutable: synced once


def test_rebuild_market_reproduces_market_and_leaves_app_alone(db: str, tmp_path: Path) -> None:
    store = three_version_store(tmp_path / "store")
    imp.import_pending(db, store)
    expected = market_snapshot(db)
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE app.outbox_events SET status = 'done' WHERE dataset_version = 1")
        conn.execute("DELETE FROM market.curve_points")
    imp.rebuild_market(db, store)
    rebuilt = market_snapshot(db)
    assert {t: rows for t, rows in rebuilt.items() if t != "dataset_versions"} == {
        t: rows for t, rows in expected.items() if t != "dataset_versions"
    }
    assert [r[:4] for r in rebuilt["dataset_versions"]] == [
        r[:4] for r in expected["dataset_versions"]
    ]  # imported_at differs by design
    assert q(db, "SELECT dataset_version, status FROM app.outbox_events ORDER BY 1") == [
        (1, "done"),
        (2, "pending"),
        (3, "pending"),
    ]


def test_cli_end_to_end(
    pg_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from energy_curves.cli import main

    store = three_version_store(tmp_path / "store")
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    monkeypatch.setenv("DATA_DIR", str(store))
    assert main(["db-migrate"]) == 0
    assert main(["db-import"]) == 0
    assert main(["db-backup", str(tmp_path / "bk")]) == 0
    assert main(["db-restore", str(tmp_path / "bk")]) == 0
    capsys.readouterr()
    assert main(["db-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {
        "schema_version": 2,
        "dataset_version": 3,
        "outbox": {"pending": 3},
        "attempts": {"published": 3},
    }


def test_attempts_in_the_pre_change_layout_are_synced(db: str, tmp_path: Path) -> None:
    """Stores written before attempts moved out of run prefixes keep them under
    runs/<id>/attempts/; the Health tab must still see them."""
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    for f in list((store / "attempts").glob("*/*.json")):
        legacy = store / "runs" / f.parent.name / "attempts" / f.name
        legacy.parent.mkdir(parents=True, exist_ok=True)
        f.rename(legacy)
    assert imp.import_pending(db, store)[1] == 1
    assert q(db, "SELECT status FROM market.pipeline_attempts") == [("published",)]
