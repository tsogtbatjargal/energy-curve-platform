"""Backup and restore of the Postgres-owned `app` schema (ADR-0013).

`market` is rebuilt from published artifacts; `app` (outbox, and from M3c alert rules, state,
history, and notification intents) exists only in Postgres, so it is backed up here: one CSV per
table via COPY, plus a manifest with row counts, SHA-256 per file, and the migration version.
Restore verifies every checksum and the migration version first, then replaces all `app` tables
in a single transaction, so it either fully succeeds or changes nothing.

`market` is the ledger of imported versions, and every imported version needs its
`dataset_imported` event. In the same transaction, restore therefore re-creates a missing event
(a backup older than the import) as `pending` with its deterministic id and payload. It refuses
an event recorded for other content, and an event for a version `market` has not imported (run
`db-import` first).
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from energy_curves.db.importer import (
    IMPORT_LOCK_KEY,
    IncompatibleHistory,
    ensure_import_event,
)
from energy_curves.db.migrate import current_version

MANIFEST = "manifest.json"


class BackupError(RuntimeError):
    pass


def app_tables(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'app' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'app' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def backup(dsn: str, target: Path) -> dict[str, Any]:
    target.mkdir(parents=True, exist_ok=False)  # never overwrite an existing backup
    manifest: dict[str, Any] = {"created_at": datetime.now(UTC).isoformat(), "tables": {}}
    with psycopg.connect(dsn) as conn:
        # One repeatable-read snapshot so all tables are mutually consistent.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        manifest["schema_version"] = current_version(conn)
        for table in app_tables(conn):
            cols = _columns(conn, table)
            buf = io.BytesIO()
            query = sql.SQL(
                "COPY (SELECT {} FROM app.{} ORDER BY 1) TO STDOUT (FORMAT csv, HEADER)"
            )
            with (
                conn.cursor() as cur,
                cur.copy(
                    query.format(
                        sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier(table)
                    )
                ) as copy,
            ):
                for chunk in copy:
                    buf.write(chunk)
            data = buf.getvalue()
            (target / f"{table}.csv").write_bytes(data)
            manifest["tables"][table] = {
                "file": f"{table}.csv",
                "columns": cols,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "rows": _count(conn, table),
            }
    (target / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _count(conn: psycopg.Connection, table: str) -> int:
    row = conn.execute(
        sql.SQL("SELECT count(*) FROM app.{}").format(sql.Identifier(table))
    ).fetchone()
    return int(row[0]) if row else 0


def restore(dsn: str, source: Path) -> dict[str, int]:
    manifest = json.loads((source / MANIFEST).read_text())
    payloads = {}
    for table, meta in manifest["tables"].items():
        data = (source / meta["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise BackupError(f"{meta['file']} does not match its manifest checksum")
        payloads[table] = data
    restored = {}
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))  # no import meanwhile
        if current_version(conn) != manifest["schema_version"]:
            raise BackupError(
                f"backup is for schema version {manifest['schema_version']}, "
                f"database is at {current_version(conn)}"
            )
        tables = app_tables(conn)
        if sorted(tables) != sorted(manifest["tables"]):
            raise BackupError(
                f"app tables differ: backup {sorted(manifest['tables'])}, db {tables}"
            )
        conn.execute(
            sql.SQL("TRUNCATE {}").format(
                sql.SQL(", ").join(sql.Identifier("app", t) for t in tables)
            )
        )
        for table in tables:
            cols = manifest["tables"][table]["columns"]
            query = sql.SQL("COPY app.{} ({}) FROM STDIN (FORMAT csv, HEADER)").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols))
            )
            with conn.cursor() as cur, cur.copy(query) as copy:
                copy.write(payloads[table])
            _reset_sequences(conn, table, cols)
            restored[table] = _count(conn, table)
            if restored[table] != manifest["tables"][table]["rows"]:
                raise BackupError(
                    f"app.{table}: restored {restored[table]} rows, "
                    f"backup has {manifest['tables'][table]['rows']}"
                )
        restored["recreated_events"] = _reconcile_events(conn)
    return restored


def _reconcile_events(conn: psycopg.Connection) -> int:
    """Give every imported version its `dataset_imported` event; return how many were created."""
    versions = conn.execute(
        "SELECT dataset_version, logical_input_id, manifest_sha256 FROM market.dataset_versions"
        " ORDER BY dataset_version"
    ).fetchall()
    try:
        created = sum(ensure_import_event(conn, *version) for version in versions)
    except IncompatibleHistory as exc:
        raise BackupError(f"backup conflicts with market: {exc}") from exc
    ahead = [
        r[0]
        for r in conn.execute(
            "SELECT dataset_version FROM app.outbox_events WHERE event_type = 'dataset_imported'"
            " AND dataset_version NOT IN (SELECT dataset_version FROM market.dataset_versions)"
            " ORDER BY dataset_version"
        )
    ]
    if ahead:
        raise BackupError(
            f"backup has events for versions {ahead} that market has not imported;"
            " run db-import first"
        )
    return created


def _reset_sequences(conn: psycopg.Connection, table: str, cols: list[str]) -> None:
    """After a bulk load, move any owned sequence past the restored maximum."""
    for col in cols:
        row = conn.execute(
            "SELECT pg_get_serial_sequence(%s, %s)", (f"app.{table}", col)
        ).fetchone()
        if row and row[0]:
            conn.execute(
                sql.SQL(
                    "SELECT setval(%s, coalesce((SELECT max({}) FROM app.{}), 0) + 1, false)"
                ).format(sql.Identifier(col), sql.Identifier(table)),
                (row[0],),
            )
