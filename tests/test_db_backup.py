import json
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from db_support import fresh_database, three_version_store

from energy_curves.db import backup as bk
from energy_curves.db import importer as imp
from energy_curves.db.migrate import migrate
from energy_curves.storage.artifacts import LocalArtifactStore, sha256

pytestmark = pytest.mark.postgres


@pytest.fixture
def db(pg_dsn: str, tmp_path: Path) -> str:
    migrate(pg_dsn)
    imp.import_pending(pg_dsn, three_version_store(tmp_path / "store"))
    with psycopg.connect(pg_dsn) as conn:  # some realistic, non-default app state
        conn.execute(
            "UPDATE app.outbox_events SET status = 'done', attempts = 1 WHERE dataset_version = 1"
        )
        conn.execute(
            "UPDATE app.outbox_events SET attempts = 2, last_error = 'boom, \"quoted\"'"
            " WHERE dataset_version = 2"
        )
    return pg_dsn


def app_state(dsn: str) -> list[tuple]:  # type: ignore[type-arg]
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT * FROM app.outbox_events ORDER BY dataset_version").fetchall()


def test_round_trip_restores_app_state_exactly(db: str, tmp_path: Path) -> None:
    before = app_state(db)
    manifest = bk.backup(db, tmp_path / "bk")
    assert manifest["tables"]["outbox_events"]["rows"] == 3 and manifest["schema_version"] == 2
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM app.outbox_events WHERE dataset_version = 3")
        conn.execute("UPDATE app.outbox_events SET status = 'dead'")
    assert bk.restore(db, tmp_path / "bk") == {"outbox_events": 3, "recreated_events": 0}
    assert app_state(db) == before


def test_backup_never_overwrites(db: str, tmp_path: Path) -> None:
    bk.backup(db, tmp_path / "bk")
    with pytest.raises(FileExistsError):
        bk.backup(db, tmp_path / "bk")


def test_tampered_file_is_rejected_before_anything_changes(db: str, tmp_path: Path) -> None:
    bk.backup(db, tmp_path / "bk")
    with psycopg.connect(db) as conn:
        conn.execute("DELETE FROM app.outbox_events WHERE dataset_version = 3")
    current = app_state(db)
    f = tmp_path / "bk" / "outbox_events.csv"
    f.write_bytes(f.read_bytes().replace(b"pending", b"done"))
    with pytest.raises(bk.BackupError, match="checksum"):
        bk.restore(db, tmp_path / "bk")
    assert app_state(db) == current


def test_schema_version_mismatch_is_rejected(db: str, tmp_path: Path) -> None:
    bk.backup(db, tmp_path / "bk")
    m = tmp_path / "bk" / bk.MANIFEST
    doc = json.loads(m.read_text())
    doc["schema_version"] = 1
    m.write_text(json.dumps(doc))
    with pytest.raises(bk.BackupError, match="schema version 1"):
        bk.restore(db, tmp_path / "bk")


def test_failing_restore_is_atomic(db: str, tmp_path: Path) -> None:
    bk.backup(db, tmp_path / "bk")
    current = app_state(db)
    # A row that violates the status CHECK, with checksums re-sealed: COPY fails part-way.
    f = tmp_path / "bk" / "outbox_events.csv"
    f.write_bytes(f.read_bytes().replace(b",pending,", b",bogus,", 1))
    m = tmp_path / "bk" / bk.MANIFEST
    doc = json.loads(m.read_text())
    doc["tables"]["outbox_events"]["sha256"] = sha256(f.read_bytes())
    m.write_text(json.dumps(doc))
    with pytest.raises(psycopg.errors.CheckViolation):
        bk.restore(db, tmp_path / "bk")
    assert app_state(db) == current  # TRUNCATE and partial COPY rolled back together


# --- finding 3: a restored app must keep an event for every imported version --------------------


def events(dsn: str) -> list[tuple]:  # type: ignore[type-arg]
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "SELECT dataset_version, event_id, payload, status FROM app.outbox_events"
            " ORDER BY dataset_version"
        ).fetchall()


def test_restoring_an_older_backup_recreates_missing_events_as_pending(
    pg_dsn: str, tmp_path: Path
) -> None:
    """Finding 3: a backup taken before versions 2 and 3 were imported has no events for them.
    The restore re-creates them, pending, with the same deterministic id and payload, so those
    versions are still processed."""
    migrate(pg_dsn)
    store_dir = three_version_store(tmp_path / "store")
    store = LocalArtifactStore(store_dir)
    with psycopg.connect(pg_dsn) as conn:
        imp.import_version(conn, store, imp.published_versions(store)[0])
        conn.execute("UPDATE app.outbox_events SET status = 'done'")
    bk.backup(pg_dsn, tmp_path / "bk")
    imp.import_pending(pg_dsn, store_dir)
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("UPDATE app.outbox_events SET status = 'done'")  # processed after the backup
    expected = [
        (
            r.dataset_version,
            imp.event_id("dataset_imported", r.dataset_version, r.manifest_sha256),
            {"dataset_version": r.dataset_version, "logical_input_id": r.logical_input_id,
             "manifest_sha256": r.manifest_sha256},
            "done" if r.dataset_version == 1 else "pending",
        )
        for r in imp.published_versions(store)
    ]  # fmt: skip
    restored = bk.restore(pg_dsn, tmp_path / "bk")
    assert events(pg_dsn) == expected
    assert restored == {"outbox_events": 1, "recreated_events": 2}


def test_restore_refuses_an_event_that_conflicts_with_market(db: str, tmp_path: Path) -> None:
    """Finding 3: a backed-up event for other content than the imported version is refused;
    nothing changes."""
    bk.backup(db, tmp_path / "bk")
    with psycopg.connect(db) as conn:
        sha = conn.execute(
            "SELECT manifest_sha256 FROM market.dataset_versions WHERE dataset_version = 2"
        ).fetchone()[0]  # type: ignore[index]
    f = tmp_path / "bk" / "outbox_events.csv"
    f.write_bytes(f.read_bytes().replace(sha.encode(), b"0" * 64))  # checksums re-sealed
    m = tmp_path / "bk" / bk.MANIFEST
    doc = json.loads(m.read_text())
    doc["tables"]["outbox_events"]["sha256"] = sha256(f.read_bytes())
    m.write_text(json.dumps(doc))
    current = app_state(db)
    with pytest.raises(bk.BackupError, match="version 2"):
        bk.restore(db, tmp_path / "bk")
    assert app_state(db) == current


@pytest.fixture
def other_db() -> Iterator[str]:
    yield from fresh_database()


def test_restore_refuses_events_for_versions_market_has_not_imported(
    db: str, other_db: str, tmp_path: Path
) -> None:
    """Finding 3: an event whose version is not in market could never resolve its values, so
    the restore is refused until db-import has caught market up."""
    bk.backup(db, tmp_path / "bk")
    other = other_db  # a database whose market has only version 1
    migrate(other)
    store = LocalArtifactStore(tmp_path / "store")
    with psycopg.connect(other) as conn:
        imp.import_version(conn, store, imp.published_versions(store)[0])
    current = app_state(other)
    with pytest.raises(bk.BackupError, match="db-import"):
        bk.restore(other, tmp_path / "bk")
    assert app_state(other) == current
