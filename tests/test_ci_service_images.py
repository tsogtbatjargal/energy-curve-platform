"""CI service containers come from this account's GHCR mirror, pinned by digest.

ECR Public's anonymous data limit failed runs, and so did Docker Hub's before it. The mirror
workflow (`mirror-ci-images.yml`) copies the same digests; the jobs pull them with GITHUB_TOKEN.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

CI_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
CI_TEXT = CI_PATH.read_text()
WORKFLOW = yaml.safe_load(CI_TEXT)

POSTGRES_DIGEST = "sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f"
VALKEY_DIGEST = "sha256:418652cfb58ef879d4978c33553735d7147016032d5aefaa14c828e611eb9dfd"
POSTGRES = f"ghcr.io/tsogtbatjargal/ecp-ci-postgres@{POSTGRES_DIGEST}"
VALKEY = f"ghcr.io/tsogtbatjargal/ecp-ci-valkey@{VALKEY_DIGEST}"
CREDENTIALS = {"username": "${{ github.actor }}", "password": "${{ secrets.GITHUB_TOKEN }}"}


def service_jobs() -> dict[str, dict[str, Any]]:
    return {j: b for j, b in WORKFLOW["jobs"].items() if "services" in b}


def test_service_images_are_the_pinned_ghcr_references() -> None:
    jobs = service_jobs()
    assert set(jobs) == {"python", "glue-compat"}
    for body in jobs.values():
        images = {name: svc["image"] for name, svc in body["services"].items()}
        assert images == {"postgres": POSTGRES, "valkey": VALKEY}


def test_service_containers_pull_with_the_workflow_token() -> None:
    for body in service_jobs().values():
        for svc in body["services"].values():
            assert svc["credentials"] == CREDENTIALS


@pytest.mark.parametrize("job", ["python", "glue-compat", "image"])
def test_the_jobs_that_pull_may_read_packages_and_nothing_more(job: str) -> None:
    assert WORKFLOW["jobs"][job]["permissions"] == {"contents": "read", "packages": "read"}


def test_no_other_job_gets_package_access() -> None:
    for name, body in WORKFLOW["jobs"].items():
        if name not in {"python", "glue-compat", "image"}:
            assert "packages" not in body.get("permissions", {}), name
    assert "packages" not in (WORKFLOW.get("permissions") or {})


def test_the_image_job_pulls_the_same_references_after_a_ghcr_login() -> None:
    steps = WORKFLOW["jobs"]["image"]["steps"]
    names = [s.get("name") for s in steps]
    login = next(s for s in steps if "docker login ghcr.io" in s.get("run", ""))
    assert names.index(login["name"]) < names.index("smoke test the installed API and UI")
    assert login["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}
    assert '"$GITHUB_TOKEN"' in login["run"] and "--password-stdin" in login["run"]
    smoke = next(s for s in steps if s.get("name") == "smoke test the installed API and UI")
    assert f"postgres={POSTGRES}\n" in smoke["run"]
    assert f"valkey={VALKEY}\n" in smoke["run"]
    assert "pull_with_retry" in smoke["run"]  # the retry stays


def test_every_reference_to_the_two_images_is_ghcr_by_exactly_these_digests() -> None:
    for line in CI_TEXT.splitlines():
        if "ecp-ci-" in line:
            assert "ghcr.io/tsogtbatjargal/ecp-ci-" in line and "@sha256:" in line, line
            assert POSTGRES_DIGEST in line or VALKEY_DIGEST in line, line
    assert CI_TEXT.count(POSTGRES) == 3  # python, glue-compat, image
    assert CI_TEXT.count(VALKEY) == 3


def test_no_workflow_pull_of_them_comes_from_ecr_public_or_docker_hub() -> None:
    for path in CI_PATH.parent.glob("*.yml"):
        text = path.read_text()
        if path.name == "mirror-ci-images.yml":
            continue  # the one place that reads the upstream, by digest
        assert "public.ecr.aws" not in text, path.name
        assert "docker.io" not in text, path.name
