import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import (
    POINTER_KEY,
    IntegrityError,
    PublishConflict,
    load_published,
    publish,
    read_pointer,
)
from energy_curves.pipeline.runner import FetchRequest, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

SPOT_JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]
SPOT_FEB = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 1), date(2024, 2, 29))]
BUSINESS_KEY = ["source", "series_id", "observation_date", "price"]


def ingest(data_dir: Path, requests=SPOT_JAN, source=None, **kw):  # type: ignore[no-untyped-def]
    return run_ingest(
        data_dir, source or SyntheticSource(), requests, source_name="synthetic", **kw
    )


def business(df: pl.DataFrame) -> list[dict[str, object]]:
    return df.select(BUSINESS_KEY).sort(BUSINESS_KEY[:3]).to_dicts()


class Crash(Exception):
    pass


def crash_at(stage: str):  # type: ignore[no-untyped-def]
    def fault(s: str) -> None:
        if s == stage:
            raise Crash(stage)

    return fault


# --- idempotency ---


def test_repeated_input_is_a_no_op(tmp_path: Path) -> None:
    first = ingest(tmp_path)
    before = load_published(LocalArtifactStore(tmp_path))
    second = ingest(tmp_path)
    after = load_published(LocalArtifactStore(tmp_path))
    assert (first.status, second.status) == ("published", "already_published")
    assert first.dataset_version == second.dataset_version == 1
    assert business(before.current) == business(after.current)
    assert after.revisions.is_empty()


def test_same_data_in_a_different_window_publishes_nothing(tmp_path: Path) -> None:
    ingest(tmp_path)
    narrower = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 2), date(2024, 1, 30))]
    result = ingest(tmp_path, narrower)
    assert result.status == "no_new_data"
    assert result.merge == {"inserted": 0, "revised": 0, "unchanged": 42}  # 21 weekdays x 2
    assert read_pointer(LocalArtifactStore(tmp_path)).dataset_version == 1  # type: ignore[union-attr]


def test_new_data_appends(tmp_path: Path) -> None:
    ingest(tmp_path)
    result = ingest(tmp_path, SPOT_FEB)
    assert (result.status, result.dataset_version) == ("published", 2)
    assert result.merge["inserted"] == 2 * 21  # 21 weekdays in Feb 2024, two series


# --- revisions ---


def test_revision_changes_current_once_and_keeps_previous(tmp_path: Path) -> None:
    first = ingest(tmp_path)
    revised = SyntheticSource({("RWTC", date(2024, 1, 10)): "99.99"})
    second = ingest(tmp_path, source=revised)
    third = ingest(tmp_path, source=revised)
    published = load_published(LocalArtifactStore(tmp_path))

    assert (second.status, second.merge["revised"]) == ("published", 1)
    assert third.status == "already_published"
    current = published.current.filter(
        (pl.col("series_id") == "RWTC") & (pl.col("observation_date") == date(2024, 1, 10))
    )
    assert current["price"].to_list() == [Decimal("99.99")]
    assert published.revisions.height == 1
    old = published.revisions.row(0, named=True)
    assert old["logical_input_id"] == first.logical_input_id
    assert old["superseded_by_logical_input_id"] == second.logical_input_id
    assert old["superseded_at_version"] == 2


def test_resending_an_older_retrieval_does_not_revert(tmp_path: Path) -> None:
    ingest(tmp_path)
    ingest(tmp_path, source=SyntheticSource({("RWTC", date(2024, 1, 10)): "99.99"}))
    stale = ingest(tmp_path)  # the original January retrieval again
    assert stale.status == "already_published"
    cur = load_published(LocalArtifactStore(tmp_path)).current
    assert Decimal("99.99") in cur["price"].to_list()


# --- quality gate and quarantine ---


@pytest.mark.parametrize("bad", ["NA", "", "1,234.5", "NaN", None])
def test_malformed_batch_quarantined_and_previous_dataset_kept(tmp_path: Path, bad) -> None:  # type: ignore[no-untyped-def]
    ingest(tmp_path)
    pointer_before = (tmp_path / POINTER_KEY).read_bytes()
    result = ingest(tmp_path, SPOT_FEB, source=SyntheticSource({("RWTC", date(2024, 2, 5)): bad}))
    assert result.status == "quarantined"
    assert result.dataset_version == 1
    assert (tmp_path / POINTER_KEY).read_bytes() == pointer_before
    rejected = pl.read_parquet(tmp_path / f"runs/{result.logical_input_id}/silver/rejected.parquet")
    assert rejected.height == 1 and rejected["reason"][0]
    quality = json.loads((tmp_path / f"runs/{result.logical_input_id}/quality.json").read_text())
    assert quality["status"] == "quarantined"


def test_numeric_strings_are_accepted_not_quarantined(tmp_path: Path) -> None:
    source = SyntheticSource({("RWTC", date(2024, 1, 10)): "70.123456"})
    assert ingest(tmp_path, source=source).status == "published"


def test_wrong_unit_quarantined(tmp_path: Path) -> None:
    class WrongUnit(SyntheticSource):
        def _rows(self, *a):  # type: ignore[no-untyped-def]
            rows = super()._rows(*a)
            rows[0]["units"] = "$/GAL"
            return rows

    assert ingest(tmp_path, source=WrongUnit()).status == "quarantined"


def test_conflicting_duplicates_quarantined(tmp_path: Path) -> None:
    class Dupes(SyntheticSource):
        def _rows(self, *a):  # type: ignore[no-untyped-def]
            rows = super()._rows(*a)
            return [*rows, {**rows[0], "value": "1.00"}]

    assert ingest(tmp_path, source=Dupes()).status == "quarantined"


# --- calendar ---


def test_weekend_only_window_is_not_a_failure(tmp_path: Path) -> None:
    ingest(tmp_path)
    weekend = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 3), date(2024, 2, 4))]
    result = ingest(tmp_path, weekend)
    assert result.status == "no_new_data"
    assert result.quality["status"] == "passed"
    assert result.quality["warnings"] == []


def test_long_gap_warns_but_does_not_fail(tmp_path: Path) -> None:
    class Silent(SyntheticSource):
        def _rows(self, *a):  # type: ignore[no-untyped-def]
            return []  # source answers successfully but has published nothing new

    ingest(tmp_path)  # last observations: 2024-01-31
    march = [FetchRequest(("RBRTE", "RWTC"), date(2024, 3, 1), date(2024, 3, 15))]
    result = ingest(tmp_path, march, source=Silent())
    assert result.status == "no_new_data"
    assert result.quality["status"] == "passed"
    assert result.quality["warnings"] == [
        "RBRTE: last observation 2024-01-31, 32 business days old",
        "RWTC: last observation 2024-01-31, 32 business days old",
    ]


def test_discontinued_futures_do_not_warn(tmp_path: Path) -> None:
    req = [FetchRequest(("RCLC1",), date(2024, 3, 1), date(2024, 6, 30))]
    result = ingest(tmp_path, req)
    assert result.status == "published"
    assert result.quality["warnings"] == []


def test_non_positive_price_preserved(tmp_path: Path) -> None:
    req = [FetchRequest(("RWTC",), date(2020, 4, 1), date(2020, 4, 30))]
    ingest(tmp_path, req)
    cur = load_published(LocalArtifactStore(tmp_path)).current
    row = cur.filter(pl.col("observation_date") == date(2020, 4, 20)).row(0, named=True)
    assert row["price"] == Decimal("-37.63") and row["non_positive"] is True


# --- crash recovery ---


@pytest.mark.parametrize("stage", ["after_bronze", "after_artifacts", "after_manifest"])
def test_crash_leaves_previous_dataset_usable_and_retry_completes(
    tmp_path: Path, stage: str
) -> None:
    ingest(tmp_path)
    store = LocalArtifactStore(tmp_path)
    before = load_published(store)
    with pytest.raises(Crash):
        ingest(tmp_path, SPOT_FEB, fault=crash_at(stage))

    during = load_published(store)  # still readable, hashes verified
    assert during.version == 1
    assert business(during.current) == business(before.current)
    failed = list((tmp_path / "attempts").glob("*/*.json"))
    assert any(json.loads(f.read_text())["status"] == "failed" for f in failed)

    retry = ingest(tmp_path, SPOT_FEB)
    assert (retry.status, retry.dataset_version) == ("published", 2)
    assert load_published(store).current.height == before.current.height + 42


def test_publish_refuses_to_move_backward(tmp_path: Path) -> None:
    ingest(tmp_path)
    ingest(tmp_path, SPOT_FEB)
    store = LocalArtifactStore(tmp_path)
    stale = {"base_dataset_version": 1, "dataset_version": 2, "logical_input_id": "x"}
    store.put("runs/x/manifest.json", b"{}")
    with pytest.raises(PublishConflict):
        publish(store, "runs/x/manifest.json", stale)
    assert read_pointer(store).dataset_version == 2  # type: ignore[union-attr]


def test_tampered_artifact_detected(tmp_path: Path) -> None:
    result = ingest(tmp_path)
    gold = tmp_path / f"runs/{result.logical_input_id}/gold/current.parquet"
    gold.write_bytes(gold.read_bytes() + b"x")
    with pytest.raises(IntegrityError):
        load_published(LocalArtifactStore(tmp_path))


def test_bronze_has_sanitised_metadata_and_hashes(tmp_path: Path) -> None:
    result = ingest(tmp_path)
    metas = list((tmp_path / f"runs/{result.logical_input_id}/bronze").glob("*.meta.json"))
    assert metas
    for m in metas:
        meta = json.loads(m.read_text())
        assert "api_key" not in json.dumps(meta)
        assert len(meta["sha256"]) == 64 and meta["schema_version"] == 1
