"""The workflow that mirrors the two CI service images to GHCR copies only the pinned digests."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
PATH = ROOT / ".github" / "workflows" / "mirror-ci-images.yml"
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

POSTGRES = (
    "public.ecr.aws/docker/library/postgres:17.11"
    "@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f"
)
VALKEY = (
    "public.ecr.aws/valkey/valkey:9.1.2"
    "@sha256:418652cfb58ef879d4978c33553735d7147016032d5aefaa14c828e611eb9dfd"
)


def workflow() -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(PATH.read_text())
    loaded["on"] = loaded.pop(True, loaded.get("on"))  # YAML 1.1 reads the key `on` as True
    return dict(loaded)


def steps() -> list[dict[str, Any]]:
    jobs = workflow()["jobs"]
    assert len(jobs) == 1
    return list(next(iter(jobs.values()))["steps"])


def run_text() -> str:
    return "\n".join(s.get("run", "") for s in steps())


def test_it_runs_only_when_dispatched_by_hand() -> None:
    assert workflow()["on"] == {"workflow_dispatch": None}


def test_it_holds_no_permissions_beyond_reading_contents_and_writing_packages() -> None:
    wf = workflow()
    assert wf["permissions"] == {}
    (job,) = wf["jobs"].values()
    assert job["permissions"] == {"contents": "read", "packages": "write"}


def test_every_action_is_pinned_to_a_commit() -> None:
    uses = [s["uses"] for s in steps() if "uses" in s]
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}( #.*)?", ref), ref


def without_tag(ref: str) -> str:
    """skopeo refuses a reference that carries both a tag and a digest; the digest decides."""
    return re.sub(r":[\w.-]+@", "@", ref)


def test_the_sources_are_exactly_the_two_pinned_digests_of_the_ci_services() -> None:
    text = PATH.read_text()
    refs = set(re.findall(r"public\.ecr\.aws/[\w./-]+@sha256:[0-9a-f]{64}", text))
    assert refs == {without_tag(POSTGRES), without_tag(VALKEY)}
    assert POSTGRES in CI and VALKEY in CI  # the same digests the CI jobs run


def test_no_source_is_addressed_by_tag_alone() -> None:
    for ref in re.findall(r"public\.ecr\.aws/\S+", PATH.read_text()):
        assert "@sha256:" in ref, ref
    assert "docker.io" not in PATH.read_text()


def test_the_copy_keeps_every_platform_and_the_digest() -> None:
    script = run_text()
    copies = re.findall(r"skopeo copy[^\n]*", script)
    assert copies
    for line in copies:
        assert "--all" in line and "--preserve-digests" in line, line


def test_the_destination_is_ghcr_under_the_lower_cased_repository_owner() -> None:
    script = run_text()
    assert "ghcr.io/" in script
    assert "${GITHUB_REPOSITORY_OWNER,,}" in script


def test_the_copy_fails_unless_the_destination_digest_equals_the_source_digest() -> None:
    script = run_text()
    assert "skopeo inspect --raw" in script
    assert "sha256sum" in script
    assert re.search(r"!=.*\n?.*exit 1|exit 1", script)
    assert "!=" in script


def test_the_token_reaches_the_login_through_the_environment_not_the_script_text() -> None:
    for step in steps():
        assert "${{" not in step.get("run", ""), step.get("name")
    login = next(s for s in steps() if "skopeo login" in s.get("run", ""))
    token_expression = "${{ github.token }}"  # noqa: S105 (a workflow expression, not a secret)
    assert login["env"]["GITHUB_TOKEN"] == token_expression
    assert "--password-stdin" in login["run"]
