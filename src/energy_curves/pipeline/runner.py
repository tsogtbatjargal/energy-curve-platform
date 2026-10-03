"""One ingestion run: fetch -> Bronze -> Silver -> quality gate -> Gold (+ curves) -> publish.

Identity: `logical_input_id` hashes what the run is *about* (source, requests, Bronze content,
transform version); `attempt_id` identifies one execution. Re-running a published input is a
no-op. Artifacts live under runs/<logical_input_id>/ and are only ever overwritten by retries of
an input that has not been published.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from energy_curves.catalog import SERIES
from energy_curves.curves.engine import build_curves
from energy_curves.ingestion.eia import Page
from energy_curves.pipeline.lock import single_writer
from energy_curves.pipeline.medallion import BronzePage, merge_gold, quality_check, to_silver
from energy_curves.pipeline.publish import (
    MANIFEST_SCHEMA_VERSION,
    dumps,
    load_published,
    parquet_bytes,
    publish,
    published_logical_ids,
)
from energy_curves.storage.artifacts import LocalArtifactStore

log = logging.getLogger(__name__)
TRANSFORM_VERSION = "medallion-v1"


class Source(Protocol):
    def fetch(
        self, route: str, series_ids: list[str], start: date, end: date
    ) -> Iterator[Page]: ...


@dataclass(frozen=True)
class FetchRequest:
    series_ids: tuple[str, ...]
    start: date
    end: date

    @property
    def route(self) -> str:
        routes = {SERIES[s].route for s in self.series_ids}
        if len(routes) != 1:
            raise ValueError(f"series span several EIA routes: {sorted(routes)}")
        return routes.pop()


@dataclass(frozen=True)
class ShapeInput:
    params: pl.DataFrame
    params_sha256: str
    method_version: str
    origin: str  # source the parameters were estimated from: "synthetic" or "eia"


class MixedSourceError(ValueError):
    """A run would add observations from a second source to a single-source store."""


class ShapeOriginMismatch(ValueError):
    """Shape parameters estimated from one source applied to data from another."""


@dataclass
class RunResult:
    status: str  # published | already_published | no_new_data | quarantined
    logical_input_id: str
    attempt_id: str
    dataset_version: int
    quality: dict[str, Any] = field(default_factory=dict)
    merge: dict[str, int] = field(default_factory=dict)


def _no_fault(stage: str) -> None:
    return None


def run_ingest(
    data_dir: Path,
    source: Source,
    requests: list[FetchRequest],
    *,
    source_name: str,
    shape: ShapeInput | None = None,
    fault: Callable[[str], None] = _no_fault,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunResult:
    """Run under the single-writer lock. `fault(stage)` lets tests simulate crashes."""
    if shape is not None and shape.origin != source_name:
        raise ShapeOriginMismatch(
            f"shape parameters come from {shape.origin!r} data; this run ingests {source_name!r}"
        )
    with single_writer(data_dir / ".pipeline.lock"):
        return _run(
            LocalArtifactStore(data_dir), source, requests, source_name, shape, fault, clock
        )


def _run(
    store: LocalArtifactStore,
    source: Source,
    requests: list[FetchRequest],
    source_name: str,
    shape: ShapeInput | None,
    fault: Callable[[str], None],
    clock: Callable[[], datetime],
) -> RunResult:
    started = time.monotonic()
    attempt_id = uuid.uuid4().hex
    requests = sorted(
        (FetchRequest(tuple(sorted(r.series_ids)), r.start, r.end) for r in requests),
        key=lambda r: (r.route, r.series_ids, r.start, r.end),
    )

    pages: list[tuple[FetchRequest, Page]] = [
        (req, page)
        for req in requests
        for page in source.fetch(req.route, list(req.series_ids), req.start, req.end)
    ]
    page_hashes = [hashlib.sha256(p.body).hexdigest() for _, p in pages]
    identity = {
        "source": source_name,
        "requests": [
            [list(r.series_ids), r.start.isoformat(), r.end.isoformat()] for r in requests
        ],
        "bronze_sha256": page_hashes,
        "transform_version": TRANSFORM_VERSION,
        "shape_params_sha256": shape.params_sha256 if shape else None,
    }
    logical_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]

    try:
        return _run_identified(
            store,
            source_name,
            requests,
            shape,
            fault,
            clock,
            started,
            attempt_id,
            pages,
            page_hashes,
            identity,
            logical_id,
        )
    except Exception as exc:
        store.put(
            f"attempts/{logical_id}/{attempt_id}.json",
            dumps(
                {
                    "status": "failed",
                    "logical_input_id": logical_id,
                    "attempt_id": attempt_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_s": round(time.monotonic() - started, 3),
                }
            ),
        )
        raise


def _run_identified(
    store: LocalArtifactStore,
    source_name: str,
    requests: list[FetchRequest],
    shape: ShapeInput | None,
    fault: Callable[[str], None],
    clock: Callable[[], datetime],
    started: float,
    attempt_id: str,
    pages: list[tuple[FetchRequest, Page]],
    page_hashes: list[str],
    identity: dict[str, Any],
    logical_id: str,
) -> RunResult:
    prefix = f"runs/{logical_id}"
    prev = load_published(store)
    # A store holds one source. Mixing synthetic and EIA observations would let curves and shape
    # estimation silently combine them, so refuse before anything is written.
    stored_sources = set(prev.current["source"].unique().to_list())
    if stored_sources - {source_name}:
        raise MixedSourceError(
            f"store holds {sorted(stored_sources)} data; refusing to add {source_name!r} data"
        )
    already = published_logical_ids(store)
    if logical_id in already:
        return _finish(
            store,
            prefix,
            RunResult("already_published", logical_id, attempt_id, already[logical_id]),
            started,
        )

    bronze: list[BronzePage] = []
    for i, ((req, page), digest) in enumerate(zip(pages, page_hashes, strict=True), start=1):
        key = f"{prefix}/bronze/page-{i:04d}.json"
        store.put(key, page.body)
        store.put(
            f"{key}.meta.json",
            dumps(
                {
                    "route": req.route,
                    "params": page.params,
                    "retrieved_at": page.retrieved_at,
                    "sha256": digest,
                    "rows": len(page.rows),
                    "schema_version": 1,
                }
            ),
        )
        bronze.append(BronzePage(key, digest, page.retrieved_at, page.rows))
    fault("after_bronze")

    silver, rejected = to_silver(bronze, source=source_name, logical_input_id=logical_id)
    requested = sorted({s for r in requests for s in r.series_ids})
    window_end = max(r.end for r in requests)
    quality = quality_check(silver, rejected, requested, window_end, prev.current)
    artifacts: dict[str, dict[str, Any]] = {}

    def put_parquet(name: str, key: str, df: pl.DataFrame) -> None:
        artifacts[name] = {
            "key": key,
            "sha256": store.put(key, parquet_bytes(df)),
            "rows": df.height,
        }

    put_parquet("silver_observations", f"{prefix}/silver/observations.parquet", silver)
    put_parquet("silver_rejected", f"{prefix}/silver/rejected.parquet", rejected)
    store.put(f"{prefix}/quality.json", dumps(quality.to_dict()))
    if not quality.passed:
        log.warning("batch quarantined", extra={"logical_input_id": logical_id})
        return _finish(
            store,
            prefix,
            RunResult("quarantined", logical_id, attempt_id, prev.version, quality.to_dict()),
            started,
        )

    version = prev.version + 1
    current, revisions, stats = merge_gold(
        prev.current, prev.revisions, silver, dataset_version=version, logical_input_id=logical_id
    )
    merge = stats.__dict__
    prev_shape = (prev.manifest or {}).get("identity", {}).get("shape_params_sha256")
    shape_changed = shape is not None and shape.params_sha256 != prev_shape
    if not stats.changed and not shape_changed:
        return _finish(
            store,
            prefix,
            RunResult(
                "no_new_data", logical_id, attempt_id, prev.version, quality.to_dict(), merge
            ),
            started,
        )

    put_parquet("gold_current", f"{prefix}/gold/current.parquet", current)
    put_parquet("gold_revisions", f"{prefix}/gold/revisions.parquet", revisions)
    if shape is not None:
        curves = build_curves(
            current,
            shape.params,
            params_sha256=shape.params_sha256,
            shape_method_version=shape.method_version,
        )
        put_parquet("gold_curves", f"{prefix}/gold/curves.parquet", curves)
    fault("after_artifacts")

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_version": version,
        "base_dataset_version": prev.version,
        "logical_input_id": logical_id,
        "attempt_id": attempt_id,
        "created_at": clock(),
        "identity": identity,
        "artifacts": artifacts,
        "quality": quality.to_dict(),
        "merge": merge,
    }
    manifest_key = f"{prefix}/manifest.json"
    store.put(manifest_key, dumps(manifest))
    fault("after_manifest")
    publish(store, manifest_key, manifest)
    return _finish(
        store,
        prefix,
        RunResult("published", logical_id, attempt_id, version, quality.to_dict(), merge),
        started,
    )


def _finish(store: LocalArtifactStore, prefix: str, result: RunResult, started: float) -> RunResult:
    # Attempt records live outside runs/<id>/ so re-running a published input never writes into
    # its run prefix.
    store.put(
        f"attempts/{result.logical_input_id}/{result.attempt_id}.json",
        dumps(
            {
                **result.__dict__,
                "duration_s": round(time.monotonic() - started, 3),
            }
        ),
    )
    log.info(
        "run finished", extra={"status": result.status, "logical_input_id": result.logical_input_id}
    )
    return result
