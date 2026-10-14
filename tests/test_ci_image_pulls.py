"""Exercise dependency-pull backoff without Docker, network access or real waits."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text())
PULLS = next(
    step["run"]
    for step in WORKFLOW["jobs"]["image"]["steps"]
    if step.get("name") == "smoke test the installed API and UI"
).split('cli="', 1)[0]
IMAGES = WORKFLOW["jobs"]["python"]["services"]

STUBS = r"""
declare -A counts
docker() {
    [[ "$#" == 2 && "$1" == pull ]] || return 99
    local image=$2 failures
    counts[$image]=$(( ${counts[$image]:-0} + 1 ))
    printf 'pull %s %s\n' "$image" "${counts[$image]}"
    case "$image" in
        *postgres*) failures=$POSTGRES_FAILURES ;;
        *valkey*) failures=$VALKEY_FAILURES ;;
        *) return 99 ;;
    esac
    [[ ${counts[$image]} -gt $failures ]]
}
sleep() { printf 'wait %s\n' "$1"; }
"""


@pytest.mark.parametrize("postgres_failures,valkey_failures", [(0, 0), (2, 1), (4, 4)])
def test_pulls_recover_with_bounded_backoff(postgres_failures, valkey_failures):
    result = run_pulls(postgres_failures, valkey_failures)
    assert result.returncode == 0, result.stderr
    expected = []
    for name, failures in [("postgres", postgres_failures), ("valkey", valkey_failures)]:
        image = IMAGES[name]["image"]
        for attempt in range(1, failures + 2):
            expected.append(f"pull {image} {attempt}")
            if attempt <= failures:
                expected.append(f"wait {attempt * 15}")
    assert result.stdout.splitlines() == expected


@pytest.mark.parametrize("failing_image", ["postgres", "valkey"])
def test_pulls_stop_after_five_failures_without_a_final_wait(failing_image):
    result = run_pulls(5 if failing_image == "postgres" else 0, 5)
    assert result.returncode != 0
    image = IMAGES[failing_image]["image"]
    assert f"Failed to pull {image} after 5 attempts" in result.stderr
    lines = result.stdout.splitlines()
    assert [line for line in lines if line.startswith("wait ")] == [
        "wait 15",
        "wait 30",
        "wait 45",
        "wait 60",
    ]
    assert lines[-1] == f"pull {image} 5"
    if failing_image == "postgres":
        assert not any(IMAGES["valkey"]["image"] in line for line in lines)


def run_pulls(postgres_failures, valkey_failures):
    bash = shutil.which("bash")
    assert bash is not None
    # Execute only the checked-in workflow prefix, with Docker and sleep replaced by stubs.
    return subprocess.run(  # noqa: S603
        [bash, "-e", "-c", STUBS + PULLS],
        env={
            **os.environ,
            "POSTGRES_FAILURES": str(postgres_failures),
            "VALKEY_FAILURES": str(valkey_failures),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
