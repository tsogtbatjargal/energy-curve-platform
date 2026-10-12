"""R3 (PLAN.md, ADR-0011 addendum): public outputs are synthetic only. These tests enforce it.

What is public: the README, any publishing workflow, and the output directories in PUBLIC_DIRS.
No artefact builder exists yet; when one does it must call `require_synthetic_store` on its
input, and this file already fails the build if it does not. Nothing here needs real data except
the opt-in `realdata` test, which compares the public files with the local cache.
"""

import json
import os
from datetime import date
from pathlib import Path

import polars as pl
import public_guard as pg
import pytest

from energy_curves.curves.engine import DISCLAIMER
from energy_curves.storage.artifacts import LocalArtifactStore

REPO = Path(__file__).parents[1]
GOOD = f"{DISCLAIMER}. Synthetic data, not EIA prices.\nWTI Spot 61.25\n"


# --- the builder's input must be synthetic -----------------------------------------------------


def publish_store(root: Path, source: str) -> Path:
    store = LocalArtifactStore(root)
    manifest = {"identity": {"source": source}, "artifacts": {}}
    raw = json.dumps(manifest).encode()
    store.put("runs/x/y/manifest.json", raw)
    pointer = {
        "dataset_version": 1, "logical_input_id": "x", "manifest_key": "runs/x/y/manifest.json",
        "manifest_sha256": __import__("hashlib").sha256(raw).hexdigest(),
    }  # fmt: skip
    store.put("published/current.json", json.dumps(pointer).encode())
    return root


def test_a_synthetic_published_store_is_accepted(tmp_path: Path) -> None:
    pg.require_synthetic_store(publish_store(tmp_path / "store", "synthetic"), repo=tmp_path)


@pytest.mark.parametrize("source", ["eia", "EIA", "synthetic2", "", "real"])
def test_a_store_whose_manifest_source_is_not_synthetic_is_refused(
    tmp_path: Path, source: str
) -> None:
    root = publish_store(tmp_path / "store", source)
    with pytest.raises(pg.PublicOutputRefused, match="source"):
        pg.require_synthetic_store(root, repo=tmp_path)


def test_a_store_with_nothing_published_is_refused(tmp_path: Path) -> None:
    (tmp_path / "store").mkdir()
    with pytest.raises(pg.PublicOutputRefused, match="published"):
        pg.require_synthetic_store(tmp_path / "store", repo=tmp_path)


def test_the_repositorys_real_data_folder_is_refused_even_when_it_looks_synthetic(
    tmp_path: Path,
) -> None:
    root = publish_store(tmp_path / "data", "synthetic")  # the repo's git-ignored data/
    with pytest.raises(pg.PublicOutputRefused, match="real-data"):
        pg.require_synthetic_store(root, repo=tmp_path)
    nested = publish_store(tmp_path / "data" / "inner" / "store", "synthetic")
    with pytest.raises(pg.PublicOutputRefused, match="real-data"):
        pg.require_synthetic_store(nested, repo=tmp_path)


@pytest.mark.parametrize("variable", ["ECP_REAL_DATA_DIR", "DATA_DIR"])
def test_the_configured_real_data_directory_is_refused(tmp_path: Path, variable: str) -> None:
    root = publish_store(tmp_path / "elsewhere" / "cache", "synthetic")
    env = {variable: str(tmp_path / "elsewhere")}
    with pytest.raises(pg.PublicOutputRefused, match="real-data"):
        pg.require_synthetic_store(root, repo=tmp_path, env=env)


def test_a_symlink_into_the_real_data_folder_is_refused(tmp_path: Path) -> None:
    real = publish_store(tmp_path / "data" / "store", "synthetic")
    link = tmp_path / "innocent"
    link.symlink_to(real)
    with pytest.raises(pg.PublicOutputRefused, match="real-data"):
        pg.require_synthetic_store(link, repo=tmp_path)


def test_a_corrupt_pointer_or_manifest_is_refused_not_trusted(tmp_path: Path) -> None:
    root = publish_store(tmp_path / "store", "synthetic")
    (root / "published" / "current.json").write_text("not json")
    with pytest.raises(pg.PublicOutputRefused):
        pg.require_synthetic_store(root, repo=tmp_path)


def test_any_snapshot_builder_must_call_the_guard() -> None:
    """Vacuous until the builder exists; then it cannot be added without the call."""
    builders = sorted((REPO / "scripts").glob("*snapshot*.py")) + sorted(
        (REPO / "src" / "energy_curves").rglob("*snapshot*.py")
    )
    for path in builders:
        assert "require_synthetic_store(" in path.read_text(), path.name


# --- every output carries the disclaimer and only synthetic values -----------------------------


def test_an_output_with_the_disclaimer_and_synthetic_values_is_accepted() -> None:
    pg.check_artifact(GOOD, "snapshot.txt")


@pytest.mark.parametrize(
    "text",
    [
        "WTI Spot 61.25\n",
        f"{DISCLAIMER}\nWTI Spot 61.25\n",
        "Synthetic data.\nWTI Spot 61.25\n",
        "",
        f"{DISCLAIMER.lower()}\nsynthetic\n",
    ],
)
def test_an_output_without_both_the_disclaimer_and_the_synthetic_notice_is_refused(
    text: str,
) -> None:
    with pytest.raises(pg.PublicOutputRefused, match="disclaimer"):
        pg.check_artifact(text, "snapshot.txt")


def test_a_price_that_the_synthetic_generator_cannot_produce_is_refused() -> None:
    values = pg.synthetic_prices()
    odd = next(f"{v}.37" for v in range(100, 400) if f"{v}.37" not in values)
    with pytest.raises(pg.PublicOutputRefused, match="price"):
        pg.check_artifact(GOOD + f"Brent spot {odd}\n", "snapshot.txt")


def test_the_synthetic_prices_are_the_generators_and_the_allowed_examples_only() -> None:
    values = pg.synthetic_prices()
    from energy_curves.synthetic_prices import synthetic_price

    assert str(synthetic_price("RWTC", date(2024, 1, 2))) in values
    assert "61.25" in values and "99.99" in values  # the documented examples
    assert len(values) > 1000


def test_versions_and_unrelated_numbers_are_not_mistaken_for_prices() -> None:
    pg.check_artifact(GOOD + "Postgres 17.11 and Valkey 9.1.2 on 2026-10-12, 200 rows.\n", "x")


@pytest.mark.parametrize("name", ["snapshot.json", "snapshot.csv", "index.html", "curves.md"])
def test_the_check_applies_to_every_output_format(name: str) -> None:
    with pytest.raises(pg.PublicOutputRefused):
        pg.check_artifact("Brent 12.34", name)


# --- a publishing workflow reads no real data -----------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "run: cp -r data/ public/",
        "run: python scripts/x.py --store data/published",
        "env: { ECP_REAL_DATA_DIR: data }",
        "env: { DATA_DIR: data }",
        "env: { EIA_API_KEY: ${{ secrets.EIA_API_KEY }} }",
        "run: python -m energy_curves ingest --source eia",
        "run: ECP_SOURCE=eia python x.py",
    ],
)
def test_a_workflow_that_reads_real_data_or_the_eia_key_is_refused(line: str) -> None:
    with pytest.raises(pg.PublicOutputRefused):
        pg.check_workflow(
            f"name: publish\njobs:\n  b:\n    steps:\n      - {line}\n", "publish.yml"
        )


def test_a_workflow_that_builds_from_synthetic_data_is_accepted() -> None:
    text = "name: publish\njobs:\n  b:\n    steps:\n      - run: ECP_SOURCE=synthetic python x.py\n"
    pg.check_workflow(text, "publish.yml")


# --- the repository's public files, today ------------------------------------------------------


def test_the_public_file_set_includes_the_readme() -> None:
    assert REPO / "README.md" in pg.public_files(REPO)


def test_the_readme_holds_no_price_outside_the_synthetic_set() -> None:
    pg.check_prices((REPO / "README.md").read_text(), "README.md")


@pytest.mark.parametrize(
    "path", [p for p in pg.public_files(REPO) if p.suffix in {".yml", ".yaml"}]
)
def test_every_public_workflow_passes_the_workflow_check(path: Path) -> None:
    pg.check_workflow(path.read_text(), path.name)


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in pg.public_files(REPO)
        if p.name != "README.md" and p.suffix not in {".yml", ".yaml"}
    ],
)
def test_every_snapshot_output_passes_the_artifact_check(path: Path) -> None:
    pg.check_artifact(path.read_text(), path.name)


def test_public_output_directories_are_the_reviewed_ones() -> None:
    assert pg.PUBLIC_DIRS == ("public", "site", "snapshot")


# --- opt-in: the local real-data cache never appears in a public file --------------------------

DATA = os.environ.get("ECP_REAL_DATA_DIR")


@pytest.mark.realdata
@pytest.mark.skipif(not DATA, reason="ECP_REAL_DATA_DIR not set")
def test_no_real_price_appears_in_any_public_file() -> None:
    real = pg.real_price_strings(Path(str(DATA)))
    assert len(real) > 1000  # the cache is really read
    leaked = {
        path.name: sorted(v for v in real if v in path.read_text())
        for path in pg.public_files(REPO)
    }
    assert all(not values for values in leaked.values()), leaked


@pytest.mark.realdata
@pytest.mark.skipif(not DATA, reason="ECP_REAL_DATA_DIR not set")
def test_the_real_cache_is_refused_by_the_guard() -> None:
    with pytest.raises(pg.PublicOutputRefused):
        pg.require_synthetic_store(
            Path(str(DATA)) / "..", repo=REPO, env={"ECP_REAL_DATA_DIR": str(DATA)}
        )


def test_a_frame_of_the_generators_prices_passes_as_an_output() -> None:
    from energy_curves.synthetic_prices import history

    rows = list(history(("RWTC",), date(2024, 1, 2), date(2024, 1, 12)))
    frame = pl.DataFrame(rows, schema=["series_id", "observation_date", "price"], orient="row")
    pg.check_artifact(GOOD + frame.write_csv(), "snapshot.csv")
