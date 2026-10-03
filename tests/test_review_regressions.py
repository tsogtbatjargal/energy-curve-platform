"""Regression tests for defects found in the M2 self-review (each failed before its fix)."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx

from energy_curves.curves.shape import ShapeEstimationError, estimate_shape
from energy_curves.ingestion.eia import EiaClient, EiaTransientError, RetryPolicy
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.parsing import Rejected, parse_price
from energy_curves.pipeline.publish import dumps, load_published, read_pointer
from energy_curves.pipeline.runner import FetchRequest, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]
FEB = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 1), date(2024, 2, 29))]


def ingest(data_dir: Path, requests=JAN, source=None):  # type: ignore[no-untyped-def]
    return run_ingest(data_dir, source or SyntheticSource(), requests, source_name="synthetic")


def test_crash_between_version_record_and_pointer_is_not_treated_as_published(
    tmp_path: Path,
) -> None:
    """Publishing writes published/versions/N.json, then the pointer. A crash in between must
    not make the retry report already_published while version N-1 is still current."""
    ingest(tmp_path)
    store = LocalArtifactStore(tmp_path)
    pointer_v1 = store.get("published/current.json")

    import energy_curves.pipeline.publish as publish_mod

    real_put = store.put

    class CrashOnPointer(Exception):
        pass

    def put(key: str, data: bytes) -> str:
        if key == publish_mod.POINTER_KEY:
            raise CrashOnPointer
        return real_put(key, data)

    original = publish_mod.publish

    def crashing_publish(st, manifest_key, manifest):  # type: ignore[no-untyped-def]
        st.put = put  # type: ignore[method-assign]
        return original(st, manifest_key, manifest)

    publish_mod_runner = __import__("energy_curves.pipeline.runner", fromlist=["publish"])
    publish_mod_runner.publish = crashing_publish
    try:
        with pytest.raises(CrashOnPointer):
            ingest(tmp_path, FEB)
    finally:
        publish_mod_runner.publish = original
    assert store.get("published/current.json") == pointer_v1
    assert store.exists("published/versions/00000002.json")  # the orphaned version record
    assert load_published(store).version == 1  # readers still see the old dataset

    retry = ingest(tmp_path, FEB)
    assert (retry.status, retry.dataset_version) == ("published", 2)
    assert read_pointer(store).dataset_version == 2  # type: ignore[union-attr]
    assert load_published(store).version == 2  # readers now see the retried publication


def test_price_beyond_silver_precision_is_rejected_not_a_crash() -> None:
    with pytest.raises(Rejected, match="exceeds"):
        parse_price("1234567890123.5")  # 13 integer digits > Decimal(18, 6)
    assert parse_price("999999999999.999999") == Decimal("999999999999.999999")


def test_oversized_price_quarantines_batch(tmp_path: Path) -> None:
    source = SyntheticSource({("RWTC", date(2024, 1, 10)): "1234567890123.5"})
    assert ingest(tmp_path, source=source).status == "quarantined"


def test_shape_refuses_mixed_sources() -> None:
    rows = [
        {
            "source": src,
            "series_id": sid,
            "observation_date": date(2014, 1, 2),
            "price": Decimal("50"),
        }
        for src in ("synthetic", "eia")
        for sid in ("RWTC", "RCLC1")
    ]
    df = pl.DataFrame(
        rows,
        schema={
            "source": pl.Utf8,
            "series_id": pl.Utf8,
            "observation_date": pl.Date,
            "price": pl.Decimal(18, 6),
        },
    )
    with pytest.raises(ShapeEstimationError, match="more than one source"):
        estimate_shape(df)


def test_series_order_does_not_change_logical_identity(tmp_path: Path) -> None:
    first = ingest(tmp_path, [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))])
    second = ingest(
        tmp_path, [FetchRequest(("RWTC", "RBRTE"), date(2024, 1, 1), date(2024, 1, 31))]
    )
    assert second.logical_input_id == first.logical_input_id
    assert second.status == "already_published"


def test_rerun_does_not_modify_a_published_run_prefix(tmp_path: Path) -> None:
    first = ingest(tmp_path)
    prefix = tmp_path / "runs" / first.logical_input_id
    before = sorted(p.relative_to(prefix).as_posix() for p in prefix.rglob("*") if p.is_file())
    ingest(tmp_path)
    after = sorted(p.relative_to(prefix).as_posix() for p in prefix.rglob("*") if p.is_file())
    assert after == before
    attempts = list((tmp_path / "attempts" / first.logical_input_id).glob("*.json"))
    assert len(attempts) == 2


@respx.mock
def test_no_sleep_after_final_attempt() -> None:
    sleeps: list[float] = []
    client = EiaClient("k" * 40, "https://api.eia.test/v2", sleep=sleeps.append)
    respx.get("https://api.eia.test/v2/petroleum/pri/spt/data/").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "2"})
    )
    with pytest.raises(EiaTransientError):
        list(client.fetch("petroleum/pri/spt", ["RWTC"], date(2024, 1, 1), date(2024, 1, 2)))
    assert len(sleeps) == RetryPolicy().max_attempts - 1


def test_published_dataset_still_loads_after_rerun(tmp_path: Path) -> None:
    ingest(tmp_path)
    ingest(tmp_path)
    assert load_published(LocalArtifactStore(tmp_path)).version == 1
    _ = dumps, json  # keep imports used
