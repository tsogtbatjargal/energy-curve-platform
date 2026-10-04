"""M4 cloud entry points (ADR-0020): Lambda staging, the Fargate task, and synthetic-only data."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from energy_curves import cloud
from energy_curves.ingestion import eia
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import load_published
from energy_curves.pipeline.runner import FetchRequest, run_ingest_store
from energy_curves.storage.artifacts import LocalArtifactStore

T = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)
TODAY = date(2024, 3, 15)  # inside the synthetic source's calendar
REQUESTS = cloud.daily_requests(TODAY)


@pytest.fixture
def s3_env() -> Iterator[dict[str, str]]:
    with mock_aws():
        s3 = boto3.client("s3", region_name="ca-central-1")
        s3.create_bucket(
            Bucket="ecp-batch-test",
            CreateBucketConfiguration={"LocationConstraint": "ca-central-1"},
        )
        yield {"ECP_STORE_URL": "s3://ecp-batch-test/batch", "ECP_SOURCE": "synthetic",
               "AWS_DEFAULT_REGION": "ca-central-1"}  # fmt: skip


def run_both_steps(env: dict[str, str], run_id: str, at: datetime = T) -> int:
    cloud.lambda_handler({"run_id": run_id, "today": TODAY.isoformat()}, env=env, clock=lambda: at)
    return cloud.task_main(["task", run_id], env=env)


def test_the_staged_replay_is_the_same_logical_input_as_one_local_run(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "cloud")
    cloud.stage(store, "run-1", SyntheticSource(retrieved_at=T), REQUESTS)
    staged = cloud.StagedSource(store, "run-1")
    assert staged.requests() == REQUESTS
    replayed = run_ingest_store(store, staged, staged.requests(), source_name="synthetic")
    local = LocalArtifactStore(tmp_path / "local")
    direct = run_ingest_store(
        local, SyntheticSource(retrieved_at=T), REQUESTS, source_name="synthetic"
    )
    assert replayed.status == direct.status == "published"
    assert replayed.logical_input_id == direct.logical_input_id


def test_both_steps_end_to_end_on_s3(
    s3_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_both_steps(s3_env, "exec-1") == 0
    first = json.loads(capsys.readouterr().out)
    assert (first["status"], first["dataset_version"], first["run_id"]) == ("published", 1,
                                                                           "exec-1")  # fmt: skip
    store = cloud.store_from_url(s3_env["ECP_STORE_URL"])
    assert load_published(store).version == 1
    assert store.list("staging/exec-1/")[-1] == "staging/exec-1/requests.json"
    # An exact replay of the same fetch is a no-op. A later fetch of the same window is new
    # evidence with the same prices: a watermark-only version (last_seen_at moves, no price).
    assert run_both_steps(s3_env, "exec-2") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_published"
    assert run_both_steps(s3_env, "exec-3", at=T.replace(day=5)) == 0
    later = json.loads(capsys.readouterr().out)
    assert (later["status"], later["dataset_version"]) == ("published", 2)
    assert later["merge"]["price_changes"] == 0 and later["merge"]["seen"] > 0


@pytest.mark.parametrize("source", ["eia", "EIA", ""])
def test_the_cloud_runtime_refuses_any_source_but_synthetic(tmp_path: Path, source: str) -> None:
    env = {"ECP_STORE_URL": f"file://{tmp_path}/store", "ECP_SOURCE": source}
    with pytest.raises(cloud.RefusedSource, match="synthetic data only"):
        cloud.lambda_handler({"run_id": "r"}, env=env, clock=lambda: T)
    with pytest.raises(cloud.RefusedSource):
        cloud.task_main(["task", "r"], env=env)
    assert not (tmp_path / "store").exists()  # nothing was written


def test_no_eia_client_is_constructed_and_no_key_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the cloud runtime constructed an EIA client")

    monkeypatch.setattr(eia.EiaClient, "__init__", refuse)
    env = {"ECP_STORE_URL": f"file://{tmp_path}/store", "EIA_API_KEY": "must-not-be-used"}
    assert run_both_steps(env, "r1") == 0
    assert "must-not-be-used" not in capsys.readouterr().out
    assert not any(b"must-not-be-used" in p.read_bytes() for p in (tmp_path / "store").rglob("*")
                   if p.is_file())  # fmt: skip


def test_a_quarantined_batch_fails_the_execution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = LocalArtifactStore(tmp_path / "store")
    bad = SyntheticSource({("RWTC", TODAY): "not-a-price"}, retrieved_at=T)
    cloud.stage(store, "r", bad, REQUESTS)
    env = {"ECP_STORE_URL": f"file://{tmp_path}/store"}
    assert cloud.task_main(["task", "r"], env=env) == cloud.EXIT_QUARANTINED
    assert json.loads(capsys.readouterr().out)["status"] == "quarantined"
    assert load_published(store).version == 0


def test_incomplete_or_tampered_staging_is_refused(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "store")
    with pytest.raises(cloud.StagingError, match="did not complete"):
        cloud.StagedSource(store, "r")
    cloud.stage(store, "r", SyntheticSource(retrieved_at=T), REQUESTS)
    page = tmp_path / "store" / "staging" / "r" / "page-0001.json"
    page.write_bytes(page.read_bytes().replace(b'"frequency"', b'"Frequency"'))
    staged = cloud.StagedSource(store, "r")
    with pytest.raises(cloud.StagingError, match="does not match"):
        list(staged.fetch("petroleum/pri/spt", list(REQUESTS[0].series_ids), REQUESTS[0].start,
                          REQUESTS[0].end))  # fmt: skip
    with pytest.raises(cloud.StagingError, match="no staged pages"):
        list(staged.fetch("petroleum/pri/spt", ["RWTC"], TODAY, TODAY))


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", "x" * 81, None])
def test_run_ids_are_plain_names(tmp_path: Path, run_id: object) -> None:
    with pytest.raises(ValueError, match="run_id"):
        cloud.stage(LocalArtifactStore(tmp_path), run_id, SyntheticSource(), REQUESTS)  # type: ignore[arg-type]


def test_store_urls(tmp_path: Path) -> None:
    assert isinstance(cloud.store_from_url(f"file://{tmp_path}"), LocalArtifactStore)
    for bad in ("http://x/y", "s3://", str(tmp_path)):
        with pytest.raises(ValueError, match="unsupported"):
            cloud.store_from_url(bad)


def test_the_daily_window_is_the_spot_series_over_the_lookback() -> None:
    assert [FetchRequest(("RBRTE", "RWTC"), date(2024, 3, 5), TODAY)] == REQUESTS


ROOT = Path(__file__).parents[1]


def test_the_image_build_context_is_an_allowlist() -> None:
    """Real EIA data (data/), .env and virtualenvs must never reach an image (ADR-0011)."""
    raw = (ROOT / ".dockerignore").read_text().splitlines()
    lines = [line.strip() for line in raw if line.strip() and not line.startswith("#")]
    assert lines[0] == "*"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == {"pyproject.toml", "uv.lock", "src/"}


def test_the_image_pins_its_base_images_and_installs_locked_hashes() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    froms = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert froms and all("@sha256:" in f for f in froms)
    assert "--locked" in dockerfile and "--require-hashes" in dockerfile
    assert "--no-dev" in dockerfile
