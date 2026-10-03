"""Content deduplication is separate from retrieval ordering.

Each observation keeps the latest retrieval time at which it was seen (`last_seen_at`), even
when the price is unchanged, so a delayed older retrieval cannot win. Only an exact replay of the
same retrieval (same content *and* same retrieval time) is a no-op.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl

from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import load_published
from energy_curves.pipeline.runner import FetchRequest, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

T1 = datetime(2026, 3, 1, 12, tzinfo=UTC)
T2 = datetime(2026, 3, 2, 12, tzinfo=UTC)
T3 = datetime(2026, 3, 3, 12, tzinfo=UTC)
DAY = date(2024, 1, 10)
JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]


def retrieval(tmp: Path, at: datetime, price: str):  # type: ignore[no-untyped-def]
    source = SyntheticSource({("RWTC", DAY): price}, retrieved_at=at)
    return run_ingest(tmp, source, JAN, source_name="synthetic")


def current_row(tmp: Path) -> dict[str, object]:
    cur = load_published(LocalArtifactStore(tmp)).current
    return cur.filter((pl.col("series_id") == "RWTC") & (pl.col("observation_date") == DAY)).row(
        0, named=True
    )


def revisions(tmp: Path) -> int:
    return load_published(LocalArtifactStore(tmp)).revisions.height


def test_t1_50_t2_60_t3_50_finishes_at_50(tmp_path: Path) -> None:
    first = retrieval(tmp_path, T1, "50.00")
    retrieval(tmp_path, T2, "60.00")
    third = retrieval(tmp_path, T3, "50.00")
    assert current_row(tmp_path)["price"] == Decimal("50.00")
    assert third.status == "published"  # not an "old duplicate" of T1
    assert third.logical_input_id != first.logical_input_id
    assert third.merge["revised"] == 1
    assert revisions(tmp_path) == 2  # 50@T1 then 60@T2, each superseded once


def test_t1_50_t3_50_then_delayed_t2_60_stays_50(tmp_path: Path) -> None:
    retrieval(tmp_path, T1, "50.00")
    seen_again = retrieval(tmp_path, T3, "50.00")
    delayed = retrieval(tmp_path, T2, "60.00")
    row = current_row(tmp_path)
    assert row["price"] == Decimal("50.00")
    assert delayed.merge["stale"] == 1 and delayed.merge["revised"] == 0
    # T3 advanced the watermark without a price revision or a price-change event.
    assert row["last_seen_at"] == T3
    assert row["retrieved_at"] == T1  # lineage of the accepted value is unchanged
    assert seen_again.merge["revised"] == 0 and seen_again.merge["inserted"] == 0
    assert seen_again.merge["price_changes"] == 0
    assert revisions(tmp_path) == 0


def test_exact_replay_of_the_same_retrieval_is_a_no_op(tmp_path: Path) -> None:
    first = retrieval(tmp_path, T1, "50.00")
    replay = retrieval(tmp_path, T1, "50.00")
    assert replay.status == "already_published"
    assert replay.dataset_version == first.dataset_version == 1


def test_older_same_price_retrieval_does_not_move_the_watermark_back(tmp_path: Path) -> None:
    retrieval(tmp_path, T3, "50.00")
    older = retrieval(tmp_path, T1, "50.00")
    assert older.status == "no_new_data"
    assert current_row(tmp_path)["last_seen_at"] == T3
