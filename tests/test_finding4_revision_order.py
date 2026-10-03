"""Finding 4: a later retrieval wins per observation; replaying an older one cannot revert it."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl

from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import load_published
from energy_curves.pipeline.runner import FetchRequest, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

T1 = datetime(2026, 1, 1, 12, tzinfo=UTC)
T2 = datetime(2026, 1, 5, 12, tzinfo=UTC)
DAY = date(2024, 1, 10)
JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]
JAN_NARROW = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 2), date(2024, 1, 30))]


def ingest(tmp: Path, requests, source):  # type: ignore[no-untyped-def]
    return run_ingest(tmp, source, requests, source_name="synthetic")


def price_on(tmp: Path, day: date) -> Decimal:
    cur = load_published(LocalArtifactStore(tmp)).current
    row = cur.filter((pl.col("series_id") == "RWTC") & (pl.col("observation_date") == day))
    return row["price"][0]  # type: ignore[no-any-return]


def test_replaying_an_older_retrieval_with_a_narrower_window_cannot_revert(tmp_path: Path) -> None:
    original = ingest(tmp_path, JAN, SyntheticSource(retrieved_at=T1))
    corrected = ingest(tmp_path, JAN, SyntheticSource({("RWTC", DAY): "99.99"}, retrieved_at=T2))
    assert corrected.merge["revised"] == 1 and price_on(tmp_path, DAY) == Decimal("99.99")

    replay = ingest(tmp_path, JAN_NARROW, SyntheticSource(retrieved_at=T1))
    assert replay.logical_input_id not in (original.logical_input_id, corrected.logical_input_id)
    assert replay.status == "no_new_data"
    assert replay.merge["stale"] == 1
    assert price_on(tmp_path, DAY) == Decimal("99.99")
    published = load_published(LocalArtifactStore(tmp_path))
    assert published.version == 2 and published.revisions.height == 1


def test_later_retrieval_still_revises(tmp_path: Path) -> None:
    ingest(tmp_path, JAN, SyntheticSource(retrieved_at=T1))
    result = ingest(tmp_path, JAN, SyntheticSource({("RWTC", DAY): "99.99"}, retrieved_at=T2))
    assert (result.status, result.merge["revised"], result.merge["stale"]) == ("published", 1, 0)


def test_same_timestamp_with_a_different_value_does_not_overwrite(tmp_path: Path) -> None:
    ingest(tmp_path, JAN, SyntheticSource(retrieved_at=T1))
    result = ingest(tmp_path, JAN, SyntheticSource({("RWTC", DAY): "99.99"}, retrieved_at=T1))
    assert result.merge["stale"] == 1 and result.merge["revised"] == 0
    assert price_on(tmp_path, DAY) != Decimal("99.99")
    assert any("not later" in w for w in result.quality["warnings"])


def test_older_retrieval_mixed_with_new_dates_publishes_only_the_new_dates(tmp_path: Path) -> None:
    ingest(tmp_path, JAN, SyntheticSource(retrieved_at=T1))
    ingest(tmp_path, JAN, SyntheticSource({("RWTC", DAY): "99.99"}, retrieved_at=T2))
    jan_feb = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 2), date(2024, 2, 29))]
    result = ingest(tmp_path, jan_feb, SyntheticSource(retrieved_at=T1))
    assert result.status == "published"
    assert result.merge["inserted"] == 42 and result.merge["stale"] == 1
    assert price_on(tmp_path, DAY) == Decimal("99.99")
