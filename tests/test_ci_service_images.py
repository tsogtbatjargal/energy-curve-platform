"""CI service containers come from ECR Public, pinned by digest (Docker Hub limits pulls)."""

from pathlib import Path

import yaml

WORKFLOW = yaml.safe_load(
    (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text()
)

POSTGRES = (
    "public.ecr.aws/docker/library/postgres:17.11"
    "@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f"
)
VALKEY = (
    "public.ecr.aws/valkey/valkey:9.1.2"
    "@sha256:418652cfb58ef879d4978c33553735d7147016032d5aefaa14c828e611eb9dfd"
)


def test_service_images_are_the_pinned_ecr_public_references() -> None:
    seen = {
        job: {name: svc["image"] for name, svc in body["services"].items()}
        for job, body in WORKFLOW["jobs"].items()
        if "services" in body
    }
    assert set(seen) == {"python", "glue-compat"}
    for images in seen.values():
        assert images == {"postgres": POSTGRES, "valkey": VALKEY}


def test_no_workflow_image_comes_from_docker_hub() -> None:
    text = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text()
    assert "docker.io" not in text
