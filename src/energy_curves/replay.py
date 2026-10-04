"""Replay: step through business days of the synthetic source to show live updates (ADR-0016).

Each step ingests one day into the store (publishing a version with curves), imports it into
Postgres (publishing `dataset_updated` after the commit), and runs one alert consumer pass. The
first step publishes a warm-up window of history at once. Overrides inject prices, so a demo or
a test can make a rule cross on a known day.

Replay only ever uses the synthetic source. It refuses a store holding any non-synthetic
published version and a database whose market holds non-synthetic data (ADR-0011).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

import psycopg

from energy_curves.catalog import SPOT_SERIES
from energy_curves.db import alerts, importer
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import read_manifest
from energy_curves.pipeline.runner import FetchRequest, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore


class ReplayRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    day: date
    ingest_status: str
    imported: list[int]
    events_done: int


def business_days(start: date, end: date) -> list[date]:
    return [
        start + timedelta(days=i)
        for i in range((end - start).days + 1)
        if (start + timedelta(days=i)).weekday() < 5
    ]


def check_synthetic_only(store_dir: Path, dsn: str) -> None:
    """Refuse before writing anything: the database first, then the store if it exists."""
    with psycopg.connect(dsn) as conn:
        sources = {
            r[0] for r in conn.execute("SELECT DISTINCT source FROM market.dataset_versions")
        }
    if sources - {"synthetic"}:
        raise ReplayRefused(
            f"the database holds {sorted(sources)} data; replay only writes to synthetic databases"
        )
    if not store_dir.exists():
        return
    store = LocalArtifactStore(store_dir)
    for record in importer.published_versions(store):
        source = read_manifest(store, record).get("identity", {}).get("source")
        if source != "synthetic":
            raise ReplayRefused(
                f"{store_dir} holds version {record.dataset_version} from source {source!r};"
                " replay only writes to synthetic stores"
            )


def replay(
    store_dir: Path,
    dsn: str,
    start: date,
    end: date,
    *,
    warmup_days: int = 20,
    interval_s: float = 0.0,
    overrides: dict[tuple[str, date], str] | None = None,
    on_committed: importer.OnCommitted | None = None,
    on_events_done: Callable[[], object] | None = None,
    on_step: Callable[[Step], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Step]:
    from energy_curves.cli import load_shape  # the packaged synthetic parameters

    check_synthetic_only(store_dir, dsn)
    shape = load_shape(store_dir, "synthetic")
    days = business_days(start, end)
    steps = []
    for i, day in enumerate(days):
        first = day - timedelta(days=warmup_days) if i == 0 else day
        source = SyntheticSource(
            overrides=overrides, retrieved_at=datetime.combine(day, clock_time(22), UTC)
        )
        result = run_ingest(
            store_dir,
            source,
            [FetchRequest(SPOT_SERIES, first, day)],
            source_name="synthetic",
            shape=shape,
        )
        imported, _ = importer.import_pending(dsn, store_dir, on_committed)
        stats = alerts.process_events(dsn, on_events_done)
        step = Step(
            day,
            result.status,
            [r.dataset_version for r in imported if r.status == "imported"],
            stats.done,
        )
        steps.append(step)
        if on_step:
            on_step(step)
        if interval_s and i < len(days) - 1:
            sleep(interval_s)
    return steps


def parse_override(text: str) -> tuple[tuple[str, date], str]:
    """`SERIES=YYYY-MM-DD:PRICE`, for example `RWTC=2024-02-29:99.99`."""
    try:
        series, rest = text.split("=", 1)
        day, price = rest.split(":", 1)
        return (series, date.fromisoformat(day)), price
    except ValueError as exc:
        raise ValueError(f"override {text!r} is not SERIES=YYYY-MM-DD:PRICE") from exc
