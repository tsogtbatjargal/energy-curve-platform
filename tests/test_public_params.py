"""Public outputs use synthetic shape parameters; real-derived ones stay local (ADR-0011)."""

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from energy_curves.cli import PACKAGED_PARAMS, load_shape, main, packaged_params_dir
from energy_curves.curves import shape
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import load_published, read_artifact
from energy_curves.pipeline.runner import FetchRequest, ShapeInput, ShapeOriginMismatch, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

BASE = packaged_params_dir()
META = json.loads((BASE / f"{PACKAGED_PARAMS}.json").read_text())
PARAMS = shape.params_from_csv((BASE / f"{PACKAGED_PARAMS}.csv").read_text())


def test_packaged_parameters_are_synthetic_and_hash_consistent() -> None:
    assert META["origin"] == "synthetic"
    assert shape.params_sha256(PARAMS) == META["params_sha256"]
    assert PARAMS.height == 48


def test_packaged_parameters_regenerate_from_the_synthetic_source(tmp_path: Path) -> None:
    """Proves the shipped numbers derive from synthetic data: same inputs, same parameters."""
    assert main(["write-synthetic-params", "--out-dir", str(tmp_path)]) == 0
    fresh_meta = json.loads((tmp_path / f"{PACKAGED_PARAMS}.json").read_text())
    fresh = shape.params_from_csv((tmp_path / f"{PACKAGED_PARAMS}.csv").read_text())
    assert shape.compare_params(PARAMS, fresh) == []
    assert fresh_meta["input_sha256"] == META["input_sha256"]
    assert fresh_meta["params_sha256"] == META["params_sha256"]


def test_csv_round_trip() -> None:
    assert shape.params_from_csv(shape.params_to_csv(PARAMS)).equals(PARAMS)


def test_only_synthetic_artifacts_are_packaged() -> None:
    files = sorted(p.name for p in BASE.iterdir() if not p.name.startswith("__"))
    assert files == [f"{PACKAGED_PARAMS}.csv", f"{PACKAGED_PARAMS}.json"]


def test_parameters_from_another_source_are_refused(tmp_path: Path) -> None:
    eia_params = ShapeInput(PARAMS, META["params_sha256"], "shape-v1", origin="eia")
    req = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]
    with pytest.raises(ShapeOriginMismatch):
        run_ingest(tmp_path, SyntheticSource(), req, source_name="synthetic", shape=eia_params)
    assert not (tmp_path / "runs").exists()


def test_synthetic_runs_fall_back_to_packaged_parameters(tmp_path: Path) -> None:
    loaded = load_shape(tmp_path, "synthetic")
    assert loaded is not None and loaded.origin == "synthetic"
    assert loaded.params_sha256 == META["params_sha256"]


def test_real_runs_never_use_packaged_parameters(tmp_path: Path) -> None:
    assert load_shape(tmp_path, "eia") is None


def test_offline_demo_curves_cite_packaged_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert (
        main(["ingest", "--source", "synthetic", "--start", "2024-04-01", "--end", "2024-04-30"])
        == 0
    )
    store = LocalArtifactStore(tmp_path)
    published = load_published(store)
    curves = read_artifact(store, published.manifest, "gold_curves")  # type: ignore[arg-type]
    assert curves["params_sha256"].unique().to_list() == [META["params_sha256"]]
    assert curves.filter(pl.col("status") == "ok").height > 0


def test_estimate_shape_records_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    main(
        [
            "ingest",
            "--source",
            "synthetic",
            "--start",
            "2014-01-01",
            "--end",
            "2024-04-05",
            "--with-futures",
        ]
    )
    assert main(["estimate-shape"]) == 0
    assert json.loads((tmp_path / "shape/meta.json").read_text())["origin"] == "synthetic"
