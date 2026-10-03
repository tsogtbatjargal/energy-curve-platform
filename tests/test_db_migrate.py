import shutil
import threading
from pathlib import Path

import psycopg
import pytest

from energy_curves.db import migrate as m

pytestmark = pytest.mark.postgres


def applied(dsn: str) -> list[tuple[int, str]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT version, name FROM schema_migrations ORDER BY 1").fetchall()


SHIPPED = [(1, "market_schema"), (2, "app_schema"), (3, "monitored_values")]
VERSIONS = [v for v, _ in SHIPPED]


def copy_migrations(tmp_path: Path) -> Path:
    target = tmp_path / "migrations"
    shutil.copytree(m.default_directory(), target)
    return target


def test_fresh_database_gets_both_schemas(pg_dsn: str) -> None:
    assert m.migrate(pg_dsn) == VERSIONS
    assert applied(pg_dsn) == SHIPPED
    with psycopg.connect(pg_dsn) as conn:
        schemas = {
            r[0] for r in conn.execute("SELECT schema_name FROM information_schema.schemata")
        }
    assert {"market", "app"} <= schemas


def test_rerun_is_a_no_op(pg_dsn: str) -> None:
    m.migrate(pg_dsn)
    assert m.migrate(pg_dsn) == []


def test_concurrent_runners_apply_each_migration_exactly_once(pg_dsn: str) -> None:
    barrier, results, errors = threading.Barrier(4), [], []

    def run() -> None:
        barrier.wait()
        try:
            results.append(m.migrate(pg_dsn))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert sorted(v for r in results for v in r) == VERSIONS  # each applied once in total
    assert applied(pg_dsn) == SHIPPED


def test_failed_migration_rolls_back_and_reruns_after_fix(pg_dsn: str, tmp_path: Path) -> None:
    d = copy_migrations(tmp_path)
    broken = d / "0004_broken.sql"
    broken.write_text("CREATE TABLE app.partial (id int);\nSELECT 1 / 0;\n")
    with pytest.raises(m.MigrationFailed, match="0004_broken"):
        m.migrate(pg_dsn, d)
    assert applied(pg_dsn) == SHIPPED  # the shipped ones committed; 0004 left nothing
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT to_regclass('app.partial')").fetchone() == (None,)
    broken.write_text("CREATE TABLE app.partial (id int);\n")
    assert m.migrate(pg_dsn, d) == [4]


def test_edited_applied_migration_is_refused(pg_dsn: str, tmp_path: Path) -> None:
    d = copy_migrations(tmp_path)
    m.migrate(pg_dsn, d)
    f = d / "0001_market_schema.sql"
    f.write_text(f.read_text() + "\n-- edited\n")
    with pytest.raises(m.ChecksumMismatch, match="0001_market_schema"):
        m.migrate(pg_dsn, d)


def test_missing_applied_migration_is_refused(pg_dsn: str, tmp_path: Path) -> None:
    d = copy_migrations(tmp_path)
    m.migrate(pg_dsn, d)
    (d / "0003_monitored_values.sql").unlink()
    with pytest.raises(m.MissingMigration, match="0003"):
        m.migrate(pg_dsn, d)


def test_numbering_gaps_are_refused(tmp_path: Path) -> None:
    d = copy_migrations(tmp_path)
    (d / "0005_skipped_four.sql").write_text("SELECT 1;")
    with pytest.raises(m.MigrationError, match="without gaps"):
        m.load(d)


def test_lock_timeout_when_another_runner_holds_the_lock(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (m.LOCK_KEY,))
        with pytest.raises(m.MigrationLockTimeout):
            m.migrate(pg_dsn, lock_timeout_s=0.3)
    assert m.migrate(pg_dsn) == VERSIONS  # lock released with the holder's session
