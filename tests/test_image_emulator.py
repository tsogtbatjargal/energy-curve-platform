"""The batch image carries no Lambda emulator; CI mounts a checked copy for the smoke test."""

# The base image ships `aws-lambda-rie` (Go standard library CVEs, ADR-0020) for local testing
# only: Lambda sets AWS_LAMBDA_RUNTIME_API and the Fargate task overrides the entry point. The
# image removes it, so the scan needs no exceptions, and the smoke step mounts a checked copy.

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
STEPS: list[dict[str, Any]] = WORKFLOW["jobs"]["image"]["steps"]
EMULATOR = "/usr/local/bin/aws-lambda-rie"
RELEASE = "https://github.com/aws/aws-lambda-runtime-interface-emulator/releases/download/v1.37/"
SHA256 = "6b1e686e62ab2baf5759c412c4864276ef2a88b094fca53ec070637ccba9b9a5"


def step(name_part: str) -> dict[str, Any]:
    return next(s for s in STEPS if name_part in s.get("name", ""))


def test_the_image_removes_the_emulator_before_it_drops_privileges() -> None:
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    run = [i for i, line in enumerate(lines) if line.strip() == f"RUN rm -f {EMULATOR}"]
    user = [i for i, line in enumerate(lines) if line.startswith("USER ")]
    assert len(run) == 1 and user and run[0] < user[0]


def test_the_image_never_copies_the_emulator_in() -> None:
    copies = [
        line
        for line in (ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith(("COPY", "ADD")) and "rie" in line
    ]
    assert copies == []


def test_the_smoke_step_downloads_one_pinned_emulator_and_checks_its_hash_first() -> None:
    run = step("smoke test")["run"]
    assert run.count(RELEASE) == 1
    assert run.count(SHA256) == 1
    assert run.index("sha256sum -c") < run.index("chmod +x") < run.index("docker run -d")


def test_the_smoke_step_mounts_the_emulator_read_only() -> None:
    run = step("smoke test")["run"]
    assert f":{EMULATOR}:ro" in run


def test_the_scan_step_has_no_exceptions() -> None:
    run = step("scan the image")["run"]
    assert "--ignorefile" not in run and "--show-suppressed" not in run
    assert "--exit-code 1" in run and "--ignore-unfixed" in run
    assert not (ROOT / ".trivyignore.yaml").exists()


def test_the_scan_runs_before_the_emulator_is_fetched() -> None:
    names = [s.get("name", "") for s in STEPS]
    scan = next(i for i, n in enumerate(names) if "scan the image" in n)
    smoke = next(i for i, n in enumerate(names) if "smoke test" in n)
    assert scan < smoke
