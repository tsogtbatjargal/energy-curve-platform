import json
from pathlib import Path

import psycopg
import pytest
from db_support import three_version_store

from energy_curves.db import backup as bk
from energy_curves.db import importer as imp
from energy_curves.db.migrate import migrate
from energy_curves.storage.artifacts import sha256

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
    assert bk.restore(db, tmp_path / "bk") == {"outbox_events": 3}
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
