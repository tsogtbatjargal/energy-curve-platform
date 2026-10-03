import json
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from db_support import FEB, FEB_ROWS, JAN, JAN_ROWS, T1, T2, T3, ingest, three_version_store

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


def serving_state(dsn: str) -> dict[str, list[tuple]]:  # type: ignore[type-arg]
    """Everything a failed import or rebuild must leave exactly as it was."""
    return {
        **market_snapshot(dsn),
        "pipeline_attempts": q(dsn, "SELECT * FROM market.pipeline_attempts ORDER BY 1"),
        "outbox_events": q(dsn, "SELECT * FROM app.outbox_events ORDER BY dataset_version"),
    }


def row_counts(state: dict[str, list[tuple]]) -> dict[str, int]:  # type: ignore[type-arg]
    return {table: len(rows) for table, rows in state.items()}


# --- ways a published store can be invalid, each applied to one version ------------------------


def _record_path(store_dir: Path, version: int) -> Path:
    return store_dir / "published" / "versions" / f"{version:08d}.json"


def tamper_artifact(store_dir: Path, version: int) -> None:
    store = LocalArtifactStore(store_dir)
    manifest = json.loads(store.get(imp.published_versions(store)[version - 1].manifest_key))
    gold = store_dir / manifest["artifacts"]["gold_current"]["key"]
    gold.write_bytes(gold.read_bytes() + b"x")


def reuse_logical_input(store_dir: Path, version: int) -> None:
    """The version record claims the logical input already published as version 1."""
    first = json.loads(_record_path(store_dir, 1).read_bytes())
    record = json.loads(_record_path(store_dir, version).read_bytes())
    _record_path(store_dir, version).write_bytes(
        dumps({**record, "logical_input_id": first["logical_input_id"]})
    )


def unpublish(store_dir: Path, version: int) -> None:
    """Remove a version record, so the next version arrives out of order."""
    _record_path(store_dir, version).unlink()


def overstate_rows(store_dir: Path, version: int) -> None:
    """A publisher bug: the manifest's row count is wrong, with every hash re-sealed."""
    store = LocalArtifactStore(store_dir)
    record = imp.published_versions(store)[version - 1]
    manifest = json.loads(store.get(record.manifest_key))
    manifest["artifacts"]["gold_current"]["rows"] += 1
    store.put(record.manifest_key, dumps(manifest))
    sealed = Pointer(
        version,
        record.logical_input_id,
        record.manifest_key,
        sha256(store.get(record.manifest_key)),
    )
    store.put(f"published/versions/{version:08d}.json", dumps(sealed.__dict__))


INVALID_INPUTS = {
    "tampered_artifact": (tamper_artifact, IntegrityError),
    "conflict": (reuse_logical_input, imp.ConflictingDataset),
    "staging_validation": (overstate_rows, imp.StagingValidationError),
    "out_of_order": (unpublish, imp.OutOfOrderImport),
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


@pytest.mark.parametrize("case", INVALID_INPUTS)
def test_failed_rebuild_preserves_the_previous_serving_state(
    db: str, tmp_path: Path, case: str
) -> None:
    """Finding 1: a rebuild is all-or-nothing. Whatever stops it, market and app stay as they
    were before the rebuild started; nothing is left empty or partial."""
    store = three_version_store(tmp_path / "store")
    imp.import_pending(db, store)
    before = serving_state(db)
    breaker, error = INVALID_INPUTS[case]
    if case == "staging_validation":
        # Hashes are checked before staging, so an already-imported version cannot reach
        # staging with different content; a new, not yet imported version can.
        ingest(store, FEB, retrieved_at=T3, overrides={("RWTC", date(2024, 2, 29)): "77.77"})
        breaker(store, 4)
    else:
        breaker(store, 2)
    with pytest.raises(error):
        imp.rebuild_market(db, store)
    after = serving_state(db)
    assert row_counts(after) == row_counts(before)
    assert after == before


@pytest.mark.parametrize("case", INVALID_INPUTS)
def test_failed_import_leaves_the_previous_version_serving(
    db: str, tmp_path: Path, case: str
) -> None:
    """Finding 1, normal import: each version is its own all-or-nothing unit, so a refused
    version leaves the last good version serving, untouched."""
    store_dir = three_version_store(tmp_path / "store")
    store = LocalArtifactStore(store_dir)
    with psycopg.connect(db) as conn:
        imp.import_version(conn, store, imp.published_versions(store)[0])
    before = serving_state(db)
    breaker, error = INVALID_INPUTS[case]
    breaker(store_dir, 2)
    with pytest.raises(error):
        imp.import_pending(db, store_dir)
    after = serving_state(db)
    assert row_counts(after) == row_counts(before)
    assert after == before


def test_readers_keep_the_previous_market_during_a_rebuild(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: until the rebuild commits, another connection reads the previous rows
    without waiting for it."""
    store = three_version_store(tmp_path / "store")
    imp.import_pending(db, store)
    seen = []
    sync = imp.sync_attempts

    def sync_then_read(conn: psycopg.Connection, store: LocalArtifactStore) -> int:
        with psycopg.connect(db) as reader:
            reader.execute("SET statement_timeout = '2s'")  # a blocked read fails the test
            seen.append(reader.execute("SELECT count(*) FROM market.observations").fetchone())
        return sync(conn, store)

    monkeypatch.setattr(imp, "sync_attempts", sync_then_read)
    imp.rebuild_market(db, store)
    assert seen == [(JAN_ROWS + FEB_ROWS,)]


# --- finding 2: history recorded in app must match the store ------------------------------------


def republish_first_version(store_dir: Path, db: str) -> None:
    """A different store: the same January data published again, so a different manifest."""
    ingest(store_dir.parent / "other", JAN, retrieved_at=T1)
    import shutil

    shutil.rmtree(store_dir)
    (store_dir.parent / "other").rename(store_dir)


def relabel_logical_input(store_dir: Path, db: str) -> None:
    record = json.loads(_record_path(store_dir, 2).read_bytes())
    _record_path(store_dir, 2).write_bytes(dumps({**record, "logical_input_id": "f" * 32}))


def replace_event_id(store_dir: Path, db: str) -> None:
    with psycopg.connect(db) as conn:
        conn.execute(
            "UPDATE app.outbox_events SET event_id = gen_random_uuid() WHERE dataset_version = 2"
        )


HISTORY_MISMATCHES = {
    "different_manifest": (republish_first_version, "version 1"),
    "different_logical_input": (relabel_logical_input, "version 2"),
    "non_deterministic_event_id": (replace_event_id, "version 2"),
}


@pytest.mark.parametrize("case", HISTORY_MISMATCHES)
def test_rebuild_refuses_history_that_differs_from_the_recorded_events(
    db: str, tmp_path: Path, case: str
) -> None:
    """Finding 2: a rebuild compares each version with its recorded dataset_imported event
    (manifest_sha256, logical_input_id, deterministic event_id) and refuses any mismatch,
    instead of adopting an event (here already done) that belongs to other content."""
    store = three_version_store(tmp_path / "store")
    imp.import_pending(db, store)
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE app.outbox_events SET status = 'done'")
    mismatch, where = HISTORY_MISMATCHES[case]
    mismatch(store, db)
    before = serving_state(db)
    with pytest.raises(imp.IncompatibleHistory, match=where):
        imp.rebuild_market(db, store)
    assert serving_state(db) == before


def test_rebuild_refuses_a_store_behind_the_recorded_history(db: str, tmp_path: Path) -> None:
    """Finding 2: app has an event for version 3, so a store publishing only versions 1-2
    cannot rebuild market; version 3's event would refer to data market no longer has."""
    store_dir = three_version_store(tmp_path / "store")
    imp.import_pending(db, store_dir)
    store = LocalArtifactStore(store_dir)
    unpublish(store_dir, 3)
    store.put("published/current.json", dumps(imp.published_versions(store)[-1].__dict__))
    before = serving_state(db)
    with pytest.raises(imp.IncompatibleHistory, match="version 3"):
        imp.rebuild_market(db, store_dir)
    assert serving_state(db) == before


def test_import_never_adopts_an_event_recorded_for_other_content(db: str, tmp_path: Path) -> None:
    """Finding 2, normal import: an existing event for the version (from a different
    publication, already done) is refused, not silently reused."""
    store = three_version_store(tmp_path / "store")
    other = imp.event_id("dataset_imported", 1, "0" * 64)
    with psycopg.connect(db) as conn:
        conn.execute(
            "INSERT INTO app.outbox_events (event_id, event_type, dataset_version, payload,"
            " status) VALUES (%s, 'dataset_imported', 1, %s, 'done')",
            (other, json.dumps({"dataset_version": 1, "logical_input_id": "a" * 32,
                                "manifest_sha256": "0" * 64})),
        )  # fmt: skip
    before = serving_state(db)
    with pytest.raises(imp.IncompatibleHistory, match="version 1"):
        imp.import_pending(db, store)
    assert serving_state(db) == before
