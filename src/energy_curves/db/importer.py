"""Import published datasets into Postgres (ADR-0013).

Each published version is imported in one transaction:
1. take the import advisory lock (one import at a time);
2. refuse conflicts (same version with a different manifest, or a logical input already recorded
   under another version) and out-of-order versions (must be max + 1);
3. read every artifact with its hash verified, COPY it into temporary staging tables, and validate
   staging against the manifest (row counts, key uniqueness, one source);
4. replace the `market` tables from staging, record the version, and write a `dataset_imported`
   outbox event, all in the same commit. An event already recorded for the version must be for
   the same content (deterministic event_id, manifest_sha256, logical_input_id); it is never
   adopted for different content.

Pipeline attempts (including failed and quarantined ones) are synced separately; their files are
immutable, so the sync only inserts.

A rebuild re-imports every version and attempt in one transaction, so it is all or nothing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from energy_curves.pipeline.medallion import GOLD_SCHEMA, REVISION_SCHEMA, conform
from energy_curves.pipeline.publish import Pointer, read_artifact, read_manifest, read_pointer
from energy_curves.storage.artifacts import LocalArtifactStore

IMPORT_LOCK_KEY = 7_212_301_002
EVENT_NAMESPACE = uuid.UUID("6f1c2a53-5e0b-4e55-9a51-0d2f7c3e9b10")

OBSERVATION_COLUMNS = list(GOLD_SCHEMA)
REVISION_COLUMNS = list(REVISION_SCHEMA)
CURVE_COLUMNS = [
    "curve_id", "kind", "as_of_date", "position", "price", "status", "gap_reason",
    "estimate_type", "shape_source", "method_version", "shape_method_version", "params_sha256",
]  # fmt: skip


# Children before parents, for deleting.
MARKET_TABLES = (
    "observations", "revisions", "curve_points", "pipeline_attempts", "dataset_versions",
)  # fmt: skip

STAGING = (
    ("stg_observations", "observations", OBSERVATION_COLUMNS),
    ("stg_revisions", "revisions", REVISION_COLUMNS),
    ("stg_curves", "curve_points", CURVE_COLUMNS),
)


class DatasetImportError(RuntimeError):
    """Base class for refused imports."""


class ConflictingDataset(DatasetImportError):
    pass


class IncompatibleHistory(ConflictingDataset):
    """The published store disagrees with the history recorded in `app`."""


class OutOfOrderImport(DatasetImportError):
    pass


class StagingValidationError(DatasetImportError):
    pass


class MixedSourceImport(DatasetImportError):
    pass


@dataclass(frozen=True)
class ImportResult:
    dataset_version: int
    status: str  # imported | already_imported


def published_versions(store: LocalArtifactStore) -> list[Pointer]:
    """Published version records up to the current pointer, oldest first."""
    pointer = read_pointer(store)
    root = store.path("published/versions")
    if pointer is None or not root.is_dir():
        return []
    records = [Pointer(**json.loads(f.read_bytes())) for f in root.glob("*.json")]
    return sorted(
        (r for r in records if r.dataset_version <= pointer.dataset_version),
        key=lambda r: r.dataset_version,
    )


def event_id(event_type: str, dataset_version: int, manifest_sha256: str) -> uuid.UUID:
    return uuid.uuid5(EVENT_NAMESPACE, f"{event_type}:{dataset_version}:{manifest_sha256}")


def ensure_import_event(
    conn: psycopg.Connection, dataset_version: int, logical_input_id: str, manifest_sha256: str
) -> bool:
    """Make sure the version's `dataset_imported` event exists for exactly this content.

    A missing event is created `pending` with the deterministic id and payload; returns True if
    it was created. An event recorded for other content (a different id, manifest or logical
    input) is refused: adopting it would mark new content as already processed."""
    expected_id = event_id("dataset_imported", dataset_version, manifest_sha256)
    payload = {
        "dataset_version": dataset_version,
        "logical_input_id": logical_input_id,
        "manifest_sha256": manifest_sha256,
    }
    row = conn.execute(
        "SELECT event_id, payload FROM app.outbox_events"
        " WHERE event_type = 'dataset_imported' AND dataset_version = %s FOR UPDATE",
        (dataset_version,),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO app.outbox_events (event_id, event_type, dataset_version, payload)"
            " VALUES (%s, 'dataset_imported', %s, %s)",
            (expected_id, dataset_version, Jsonb(payload)),
        )
        return True
    if row[0] != expected_id or row[1] != payload:
        raise IncompatibleHistory(
            f"version {dataset_version}: the recorded dataset_imported event {row[0]}"
            f" (manifest {row[1].get('manifest_sha256')}, logical input"
            f" {row[1].get('logical_input_id')}) is for different content than manifest"
            f" {manifest_sha256}, logical input {logical_input_id}"
        )
    return False


def _copy(conn: psycopg.Connection, table: str, columns: list[str], df: pl.DataFrame) -> None:
    cols = ", ".join(columns)
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({cols}) FROM STDIN") as copy:  # noqa: S608 - fixed identifiers
        for row in df.select(columns).iter_rows():
            copy.write_row(row)


def _scalar(conn: psycopg.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(query, params).fetchone()
    return row[0] if row else None


def import_version(
    conn: psycopg.Connection, store: LocalArtifactStore, record: Pointer
) -> ImportResult:
    """Import one version in its own transaction: it is published completely or not at all."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        return _import(conn, store, record)


def _import(conn: psycopg.Connection, store: LocalArtifactStore, record: Pointer) -> ImportResult:
    """The import steps; the caller holds the import lock inside an open transaction."""
    version = record.dataset_version
    existing = conn.execute(
        "SELECT manifest_sha256, logical_input_id FROM market.dataset_versions"
        " WHERE dataset_version = %s",
        (version,),
    ).fetchone()
    if existing is not None:
        if existing[0] != record.manifest_sha256 or existing[1] != record.logical_input_id:
            raise ConflictingDataset(
                f"version {version} is already imported with different content"
            )
        ensure_import_event(conn, version, record.logical_input_id, record.manifest_sha256)
        return ImportResult(version, "already_imported")
    other = _scalar(
        conn,
        "SELECT dataset_version FROM market.dataset_versions WHERE logical_input_id = %s",
        (record.logical_input_id,),
    )
    if other is not None:
        raise ConflictingDataset(
            f"logical input {record.logical_input_id} is already imported as version {other}"
        )
    expected = int(
        _scalar(conn, "SELECT coalesce(max(dataset_version), 0) + 1 FROM market.dataset_versions")
    )
    if version != expected:
        raise OutOfOrderImport(f"version {version} cannot be imported before {expected}")
    # Before any artifact is read: the version must match the history recorded in app.
    ensure_import_event(conn, version, record.logical_input_id, record.manifest_sha256)

    manifest = read_manifest(store, record)  # verifies the manifest hash
    if manifest["dataset_version"] != version:
        raise ConflictingDataset(f"manifest says version {manifest['dataset_version']}")
    current = conform(read_artifact(store, manifest, "gold_current"), GOLD_SCHEMA)
    revisions = conform(read_artifact(store, manifest, "gold_revisions"), REVISION_SCHEMA)
    has_curves = "gold_curves" in manifest["artifacts"]
    curves = read_artifact(store, manifest, "gold_curves") if has_curves else None

    sources = set(current["source"].unique().to_list())
    known = {r[0] for r in conn.execute("SELECT DISTINCT source FROM market.dataset_versions")}
    if len(sources) > 1 or (known and known != sources):
        raise MixedSourceImport(f"dataset sources {sorted(sources)}, database {sorted(known)}")

    # Column-only staging copies: no constraints, no dataset_version (set on publication).
    # A rebuild imports every version in one transaction, so ON COMMIT DROP alone would leave
    # the previous version's staging tables in place.
    for stg, table, cols in STAGING:
        conn.execute(f"DROP TABLE IF EXISTS {stg}")
        conn.execute(
            f"CREATE TEMP TABLE {stg} ON COMMIT DROP AS"  # noqa: S608 - fixed identifiers
            f" SELECT {', '.join(cols)} FROM market.{table} WITH NO DATA"
        )
    _copy(conn, "stg_observations", OBSERVATION_COLUMNS, current)
    _copy(conn, "stg_revisions", REVISION_COLUMNS, revisions)
    if curves is not None:
        _copy(
            conn,
            "stg_curves",
            CURVE_COLUMNS,
            curves.with_columns(pl.col("position").cast(pl.Utf8)),
        )
    _validate(conn, manifest, has_curves)

    conn.execute(
        "INSERT INTO market.dataset_versions (dataset_version, logical_input_id,"
        " manifest_sha256, source, created_at, observations, revisions, curve_points,"
        " price_changes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            version, record.logical_input_id, record.manifest_sha256, sources.pop(),
            manifest.get("created_at") or datetime.now(UTC), current.height,
            revisions.height, 0 if curves is None else curves.height,
            int(manifest.get("merge", {}).get("price_changes", 0)),
        ),
    )  # fmt: skip
    for stg, table, cols in STAGING:
        columns = ", ".join(cols)
        versioned = table != "revisions"  # revisions carry their own version columns
        conn.execute(f"DELETE FROM market.{table}")  # noqa: S608 - fixed identifiers
        conn.execute(
            f"INSERT INTO market.{table} ({columns}{', dataset_version' if versioned else ''})"  # noqa: S608
            f" SELECT {columns}{', %s' if versioned else ''} FROM {stg}",
            (version,) if versioned else (),
        )

    return ImportResult(version, "imported")


def _validate(conn: psycopg.Connection, manifest: dict[str, Any], has_curves: bool) -> None:
    checks = [
        ("stg_observations", "gold_current", "source, series_id, observation_date"),
        ("stg_revisions", "gold_revisions",
         "source, series_id, observation_date, superseded_at_version"),
    ]  # fmt: skip
    if has_curves:
        checks.append(("stg_curves", "gold_curves", "curve_id, as_of_date, position"))
    for table, artifact, key in checks:
        rows, distinct = conn.execute(
            f"SELECT count(*), count(DISTINCT ({key})) FROM {table}"  # noqa: S608 - fixed identifiers
        ).fetchone() or (0, 0)
        expected = manifest["artifacts"][artifact]["rows"]
        if rows != expected:
            raise StagingValidationError(
                f"{artifact}: staged {rows} rows, manifest says {expected}"
            )
        if distinct != rows:
            raise StagingValidationError(f"{artifact}: {rows - distinct} duplicate keys")


def sync_attempts(conn: psycopg.Connection, store: LocalArtifactStore) -> int:
    """Insert attempt records not yet in Postgres; return how many were added."""
    # attempts/<id>/*.json, plus runs/<id>/attempts/*.json written before attempts moved out of
    # run prefixes. Attempt files are immutable, so inserting the unseen ones is the whole sync.
    files = sorted(store.path("attempts").glob("*/*.json")) + sorted(
        store.path("runs").glob("*/attempts/*.json")
    )
    added = 0
    with conn.transaction():
        for f in files:
            doc = json.loads(f.read_bytes())
            finished = doc.get("finished_at") or datetime.fromtimestamp(f.stat().st_mtime, UTC)
            cur = conn.execute(
                "INSERT INTO market.pipeline_attempts (attempt_id, logical_input_id, status,"
                " dataset_version, quality, merge, error, duration_s, finished_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    doc["attempt_id"], doc["logical_input_id"], doc["status"],
                    doc.get("dataset_version"), Jsonb(doc.get("quality") or {}),
                    Jsonb(doc.get("merge") or {}), doc.get("error"), doc.get("duration_s"),
                    finished,
                ),
            )  # fmt: skip
            added += cur.rowcount
    return added


def import_pending(dsn: str, data_dir: Path) -> tuple[list[ImportResult], int]:
    """Import every published version not yet in Postgres, oldest first; sync attempts."""
    store = LocalArtifactStore(data_dir)
    results = []
    with psycopg.connect(dsn) as conn:
        for record in published_versions(store):
            results.append(import_version(conn, store, record))
        attempts = sync_attempts(conn, store)
    return results, attempts


def rebuild_market(dsn: str, data_dir: Path) -> tuple[list[ImportResult], int]:
    """Re-import the rebuildable `market` schema from the published versions, all or nothing.

    One transaction holds the import lock, deletes `market` and re-imports every version and
    attempt, so any failure (tampered artifact, conflict, staging validation, out-of-order
    version) rolls back to the previous `market`. DELETE rather than TRUNCATE: readers keep
    seeing the previous rows until the commit instead of blocking on an exclusive lock.
    Every version must match its recorded `dataset_imported` event, and the store must reach
    the newest recorded version (IncompatibleHistory otherwise); `app` is otherwise untouched."""
    store = LocalArtifactStore(data_dir)
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK_KEY,))
        for table in MARKET_TABLES:
            conn.execute(f"DELETE FROM market.{table}")  # noqa: S608 - fixed identifiers
        results = [_import(conn, store, record) for record in published_versions(store)]
        rebuilt = max((r.dataset_version for r in results), default=0)
        recorded = _scalar(
            conn,
            "SELECT max(dataset_version) FROM app.outbox_events"
            " WHERE event_type = 'dataset_imported'",
        )
        if recorded is not None and recorded > rebuilt:
            raise IncompatibleHistory(
                f"app records version {recorded}, but the store publishes only up to {rebuilt}"
            )
        attempts = sync_attempts(conn, store)
    return results, attempts
