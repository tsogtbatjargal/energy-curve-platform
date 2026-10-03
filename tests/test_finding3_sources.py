"""Finding 3: synthetic and real observations must never share a store or a curve."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from energy_curves.curves.engine import MixedSourceError as EngineMixedSource
from energy_curves.curves.engine import build_curves
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.medallion import PRICE
from energy_curves.pipeline.publish import POINTER_KEY
from energy_curves.pipeline.runner import FetchRequest, MixedSourceError, run_ingest

JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]


def test_second_source_refused_before_anything_is_written(tmp_path: Path) -> None:
    run_ingest(tmp_path, SyntheticSource(), JAN, source_name="synthetic")
    pointer = (tmp_path / POINTER_KEY).read_bytes()
    runs_before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    feb = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 1), date(2024, 2, 29))]
    with pytest.raises(MixedSourceError, match="synthetic"):
        run_ingest(tmp_path, SyntheticSource(), feb, source_name="eia")
    assert (tmp_path / POINTER_KEY).read_bytes() == pointer
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == runs_before


def test_same_source_still_appends(tmp_path: Path) -> None:
    run_ingest(tmp_path, SyntheticSource(), JAN, source_name="synthetic")
    feb = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 1), date(2024, 2, 29))]
    assert run_ingest(tmp_path, SyntheticSource(), feb, source_name="synthetic").status == (
        "published"
    )


def mixed_gold() -> pl.DataFrame:
    d = date(2026, 9, 29)
    rows = [
        ("synthetic", "RWTC", "55.00"),
        ("eia", "RWTC", "70.00"),
        ("synthetic", "RBRTE", "59.00"),
        ("eia", "RBRTE", "74.00"),
    ]
    return pl.DataFrame(
        [
            {"source": s, "series_id": sid, "observation_date": d, "price": Decimal(p)}
            for s, sid, p in rows
        ],
        schema={
            "source": pl.Utf8,
            "series_id": pl.Utf8,
            "observation_date": pl.Date,
            "price": PRICE,
        },
    )


def test_curve_generation_refuses_mixed_sources() -> None:
    params = pl.DataFrame(
        [
            {"position": f"C{k}", "month": m, "s": Decimal("0.01"), "n_obs": 20}
            for k in range(1, 5)
            for m in range(1, 13)
        ],
        schema={"position": pl.Utf8, "month": pl.Int8, "s": pl.Decimal(20, 10), "n_obs": pl.Int64},
    )
    with pytest.raises(EngineMixedSource, match="eia"):
        build_curves(mixed_gold(), params, params_sha256="x", shape_method_version="v")
