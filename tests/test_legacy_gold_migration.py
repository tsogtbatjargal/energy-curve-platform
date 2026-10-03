"""Datasets published before `last_seen_at` existed must keep ingesting and keep their history.

A legacy store is built exactly as the earlier code wrote it: Gold current with the Silver
columns only, and revisions with the Silver columns followed by the superseded_* columns.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.medallion import REVISION_SCHEMA, SILVER_SCHEMA
from energy_curves.pipeline.publish import dumps, load_published, parquet_bytes, publish
from energy_curves.pipeline.runner import FetchRequest, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 1, 5, tzinfo=UTC)
T3 = datetime(2026, 1, 9, tzinfo=UTC)
DAY = date(2024, 1, 10)
JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]
FEB = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 1), date(2024, 2, 29))]
LEGACY_REVISION_COLUMNS = [
    *SILVER_SCHEMA,
    "superseded_at_version",
    "superseded_by_logical_input_id",
]


def legacy_store(tmp_path: Path, with_history: bool) -> tuple[Path, pl.DataFrame, pl.DataFrame]:
    """Publish version 1 of a store in the pre-`last_seen_at` layout."""
    scratch, store_dir = tmp_path / "scratch", tmp_path / "legacy"
    run_ingest(scratch, SyntheticSource(retrieved_at=T1), JAN, source_name="synthetic")
    if with_history:
        corrected = SyntheticSource({("RWTC", DAY): "99.99"}, retrieved_at=T2)
        run_ingest(scratch, corrected, JAN, source_name="synthetic")
    modern = load_published(LocalArtifactStore(scratch))
    current = modern.current.select(list(SILVER_SCHEMA))
    revisions = modern.revisions.select(LEGACY_REVISION_COLUMNS)
    assert "last_seen_at" not in current.columns + revisions.columns
    assert revisions.height == (1 if with_history else 0)

    store = LocalArtifactStore(store_dir)
    artifacts = {}
    for name, df in (("gold_current", current), ("gold_revisions", revisions)):
        key = f"runs/legacy/gold/{name}.parquet"
        artifacts[name] = {
            "key": key,
            "sha256": store.put(key, parquet_bytes(df)),
            "rows": df.height,
        }
    manifest = {
        "manifest_schema_version": 1,
        "dataset_version": 1,
        "base_dataset_version": 0,
        "logical_input_id": "legacy",
        "artifacts": artifacts,
        "identity": {},
        "quality": {},
        "merge": {},
    }
    store.put("runs/legacy/manifest.json", dumps(manifest))
    publish(store, "runs/legacy/manifest.json", manifest)
    return store_dir, current, revisions


def legacy_columns(df: pl.DataFrame, columns: list[str]) -> list[dict[str, object]]:
    return df.select(columns).sort(["series_id", "observation_date"]).to_dicts()


@pytest.mark.parametrize("with_history", [False, True], ids=["empty-history", "populated-history"])
def test_ingesting_new_dates_into_a_legacy_store(tmp_path: Path, with_history: bool) -> None:
    store_dir, old_current, old_revisions = legacy_store(tmp_path, with_history)
    result = run_ingest(store_dir, SyntheticSource(retrieved_at=T3), FEB, source_name="synthetic")
    assert (result.status, result.dataset_version) == ("published", 2)

    published = load_published(LocalArtifactStore(store_dir))
    assert list(published.revisions.columns) == list(REVISION_SCHEMA)
    assert legacy_columns(published.revisions, LEGACY_REVISION_COLUMNS) == legacy_columns(
        old_revisions, LEGACY_REVISION_COLUMNS
    )
    jan = published.current.filter(pl.col("observation_date") < date(2024, 2, 1))
    assert legacy_columns(jan, list(SILVER_SCHEMA)) == legacy_columns(
        old_current, list(SILVER_SCHEMA)
    )
    # Migrated rows take retrieved_at as their watermark.
    assert jan.filter(pl.col("last_seen_at") != pl.col("retrieved_at")).is_empty()


@pytest.mark.parametrize("with_history", [False, True], ids=["empty-history", "populated-history"])
def test_revising_a_legacy_store_appends_to_its_history(tmp_path: Path, with_history: bool) -> None:
    store_dir, _, old_revisions = legacy_store(tmp_path, with_history)
    later = SyntheticSource({("RWTC", DAY): "123.45"}, retrieved_at=T3)
    result = run_ingest(store_dir, later, JAN, source_name="synthetic")
    assert (result.status, result.merge["revised"]) == ("published", 1)

    published = load_published(LocalArtifactStore(store_dir))
    assert list(published.revisions.columns) == list(REVISION_SCHEMA)
    assert published.revisions.height == old_revisions.height + 1
    new = published.revisions.filter(
        pl.col("superseded_by_logical_input_id") == result.logical_input_id
    )
    assert new.height == 1 and new["price"][0] == Decimal("99.99" if with_history else "69.16")
    kept = published.revisions.filter(
        pl.col("superseded_by_logical_input_id") != result.logical_input_id
    )
    assert legacy_columns(kept, LEGACY_REVISION_COLUMNS) == legacy_columns(
        old_revisions, LEGACY_REVISION_COLUMNS
    )
    row = published.current.filter(
        (pl.col("series_id") == "RWTC") & (pl.col("observation_date") == DAY)
    ).row(0, named=True)
    assert row["price"] == Decimal("123.45") and row["last_seen_at"] == T3
