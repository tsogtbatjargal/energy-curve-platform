"""The image smoke script (scripts/image_smoke.py) runs inside the built image in CI. It must keep
exercising two things the entry-point smoke does not reach: the negative-day values of the synthetic
generator, and the publish repair of a wrong index record at the pointer's version (#69)."""

import runpy
from pathlib import Path

import pytest
import yaml

from energy_curves import synthetic_prices
from energy_curves.pipeline import publish

REPO = Path(__file__).parents[1]
SCRIPT = REPO / "scripts" / "image_smoke.py"


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECP_SOURCE", "synthetic")
    monkeypatch.delenv("ECP_STORE_URL", raising=False)


def run_script() -> None:
    runpy.run_path(str(SCRIPT), run_name="__main__")


def test_the_smoke_script_passes_on_the_current_code() -> None:
    run_script()


def test_the_smoke_script_fails_when_a_wrong_record_is_not_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behaviour before #69: only a missing record at the pointer version was rewritten."""

    def only_missing(store: publish.ArtifactStore, version: int, pointer_doc: bytes) -> bool:
        return store.exists(publish.version_key(version))

    monkeypatch.setattr(publish, "record_matches", only_missing)
    with pytest.raises(AssertionError, match="record"):
        run_script()


def test_the_smoke_script_fails_when_the_negative_day_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moved = synthetic_prices.NEGATIVE_DAY.replace(day=21)
    monkeypatch.setattr(synthetic_prices, "NEGATIVE_DAY", moved)
    with pytest.raises(AssertionError, match="2020-04-20"):
        run_script()


def test_ci_runs_the_smoke_script_in_the_image_without_a_network() -> None:
    steps = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())["jobs"][
        "image"
    ]["steps"]
    run = next(s["run"] for s in steps if "image_smoke.py" in s.get("run", ""))
    assert "docker run --rm -i --network none --entrypoint python ecp-batch:ci -" in run
    assert "< scripts/image_smoke.py" in run
