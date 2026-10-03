"""Opt-in checks against a local cache of real EIA data (never committed; ADR-0011).

Run with: ECP_REAL_DATA_DIR=data uv run pytest -m realdata
"""

import json
import os
from pathlib import Path

import pytest

from energy_curves.cli import main

pytestmark = pytest.mark.realdata
DATA = os.environ.get("ECP_REAL_DATA_DIR")
RECORD = Path(__file__).parents[1] / "data_manifests/shape_inputs.eia.json"
skip = pytest.mark.skipif(not DATA, reason="ECP_REAL_DATA_DIR not set")


@skip
def test_shape_parameters_reproduce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(DATA))
    assert main(["verify-shape"]) == 0


@skip
def test_local_inputs_match_recorded_hashes() -> None:
    meta = json.loads((Path(str(DATA)) / "shape/meta.json").read_text())
    record = json.loads(RECORD.read_text())
    assert meta["input_sha256"] == record["input_sha256"]
    assert meta["params_sha256"] == record["params_sha256"]
