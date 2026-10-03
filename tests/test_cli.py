from pathlib import Path

import pytest

from energy_curves.cli import main
from energy_curves.curves.engine import DISCLAIMER


def test_offline_demo_from_clean_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    assert (
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
        == 0
    )
    assert main(["estimate-shape"]) == 0
    assert (
        main(["ingest", "--source", "synthetic", "--start", "2024-04-06", "--end", "2024-04-30"])
        == 0
    )
    assert main(["verify-shape"]) == 0
    out = tmp_path / "curves.csv"
    assert main(["export-curves", "--as-of", "2024-04-30", "--out", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0] == f"# {DISCLAIMER}"
    assert len(lines) == 2 + 3 * 5  # disclaimer, header, 3 curves x 5 positions
    capsys.readouterr()
    assert main(["status"]) == 0
    assert '"dataset_version": 2' in capsys.readouterr().out


def test_export_without_curves_explains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from energy_curves.ingestion.synthetic import SyntheticSource
    from energy_curves.pipeline.runner import FetchRequest, run_ingest

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    req = [FetchRequest(("RWTC",), date(2024, 1, 1), date(2024, 1, 31))]
    run_ingest(tmp_path, SyntheticSource(), req, source_name="synthetic", shape=None)
    assert main(["export-curves"]) == 2


def test_eia_source_without_key_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EIA_API_KEY", "")
    from energy_curves.ingestion.eia import EiaAuthError

    with pytest.raises(EiaAuthError):
        main(["ingest", "--source", "eia", "--start", "2024-01-01", "--end", "2024-01-31"])
