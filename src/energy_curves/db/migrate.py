"""Numbered SQL migrations (ADR-0013).

Files are `NNNN_name.sql`, numbered contiguously from 0001. Each applied migration's SHA-256 (of
the exact file bytes) is recorded; an applied migration that is edited, missing, or unknown stops
the run. Runners serialize on a Postgres advisory lock, and each migration commits together with
its record in one transaction, so a failure leaves nothing behind and can be re-run once fixed.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import psycopg

LOCK_KEY = 7_212_301_001  # project-wide advisory lock id for migrations
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    pass


class ChecksumMismatch(MigrationError):
    pass


class MissingMigration(MigrationError):
    pass


class MigrationLockTimeout(MigrationError):
    pass


class MigrationFailed(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def default_directory() -> Path:
    return Path(str(resources.files("energy_curves.db").joinpath("migrations")))


def load(directory: Path | None = None) -> list[Migration]:
    directory = directory or default_directory()
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE.match(path.name)
        if not match:
            raise MigrationError(f"badly named migration file: {path.name}")
        raw = path.read_bytes()
        found.append(
            Migration(int(match[1]), match[2], raw.decode(), hashlib.sha256(raw).hexdigest())
        )
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migrations must be numbered 1..n without gaps, found {versions}")
    return found


def _lock(conn: psycopg.Connection, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()
        if row and row[0]:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeout(f"another migration run held the lock for {timeout_s}s")
        time.sleep(0.05)


def migrate(dsn: str, directory: Path | None = None, lock_timeout_s: float = 30.0) -> list[int]:
    """Apply pending migrations; return the versions applied by this call."""
    migrations = load(directory)
    by_version = {m.version: m for m in migrations}
    with psycopg.connect(dsn, autocommit=True) as conn:
        _lock(conn, lock_timeout_s)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
                " version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            applied = dict(
                conn.execute("SELECT version, checksum FROM public.schema_migrations").fetchall()
            )
            for version, checksum in sorted(applied.items()):
                if version not in by_version:
                    raise MissingMigration(f"applied migration {version:04d} has no file")
                if by_version[version].checksum != checksum:
                    raise ChecksumMismatch(
                        f"migration {version:04d}_{by_version[version].name} changed after it "
                        "was applied; add a new migration instead"
                    )
            done = []
            for m in migrations:
                if m.version in applied:
                    continue
                try:
                    with conn.transaction():
                        conn.execute(m.sql.encode())  # bytes: no %-placeholder interpretation
                        conn.execute(
                            "INSERT INTO public.schema_migrations (version, name, checksum)"
                            " VALUES (%s, %s, %s)",
                            (m.version, m.name, m.checksum),
                        )
                except psycopg.Error as exc:
                    raise MigrationFailed(f"{m.version:04d}_{m.name}: {exc}") from exc
                done.append(m.version)
            return done
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def current_version(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT coalesce(max(version), 0) FROM public.schema_migrations").fetchone()
    return int(row[0]) if row else 0
