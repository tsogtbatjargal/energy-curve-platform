"""M4c-2 (ADR-0022): CI plans infra/batch with the member plan role, and prints only a summary.

The workflow runs on a public repository, so what it may print is part of its contract: the
batch plan goes to a file, only the `Plan:` / `No changes` line (and the first line of an
error, with 12-digit numbers masked) reaches the log, and the files are removed afterwards. No
secret is used: the alert address is a placeholder, the digest and the schedule flag are tracked.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
JOB = WORKFLOW["jobs"]["terraform-plan"]
STEPS = {s["name"]: s for s in JOB["steps"] if "name" in s}
BATCH_ONLY = "matrix.stack == 'batch'"
BOOTSTRAP_ONLY = "matrix.stack == 'bootstrap'"


def batch_plan_step() -> dict[str, Any]:
    return next(
        s for s in JOB["steps"] if s.get("if") == BATCH_ONLY and "terraform plan" in s["run"]
    )


def test_both_stacks_are_planned_by_the_same_job() -> None:
    assert JOB["strategy"]["matrix"]["stack"] == ["bootstrap", "batch"]
    assert JOB["permissions"] == {"contents": "read", "id-token": "write"}


def test_the_alert_address_is_a_placeholder_and_no_secret_is_used() -> None:
    assert JOB["env"]["TF_VAR_alert_email"] == "ci@example.invalid"
    assert "secrets." not in yaml.safe_dump(JOB)


def test_the_batch_plan_goes_to_a_file_and_only_a_summary_is_printed() -> None:
    run = batch_plan_step()["run"]
    assert re.search(r"terraform plan .*-no-color .*> plan\.txt 2>&1", run)
    assert "grep -E '^(Plan:|No changes)' plan.txt" in run
    # an error shows its first line only, with 12-digit numbers masked
    assert "grep -E '^Error' plan.txt" in run
    assert "sed -E 's/[0-9]{12}/<account>/g'" in run
    assert "cat plan" not in run


def test_the_bootstrap_plan_is_unchanged() -> None:
    step = next(
        s for s in JOB["steps"] if s.get("if") == BOOTSTRAP_ONLY and "terraform plan" in s["run"]
    )
    assert "terraform plan -input=false -lock-timeout=60s -out=tfplan" in step["run"]
    assert "terraform show -json tfplan > plan.json" in step["run"]


def test_each_stack_has_its_own_policy_gate() -> None:
    gates = [s for s in JOB["steps"] if "policy_gate.py" in s.get("run", "")]
    by_guard = {s.get("if"): s["run"] for s in gates}
    assert set(by_guard) == {BOOTSTRAP_ONLY, BATCH_ONLY}
    assert '--stack "$STACK"' in by_guard[BOOTSTRAP_ONLY]
    assert '--stack "$STACK" --stack-dir .' in by_guard[BATCH_ONLY]


def test_plan_files_are_removed_even_when_a_step_fails() -> None:
    step = JOB["steps"][-1]
    assert step["if"] == "always()"
    assert "rm -f tfplan plan.json plan.txt" in step["run"]


def test_no_step_prints_the_plan_json() -> None:
    for step in JOB["steps"]:
        run = step.get("run", "")
        assert not re.search(r"(cat|jq|head|tail)\b.*plan\.(json|txt)", run), step.get("name")


@pytest.mark.parametrize("job", sorted(WORKFLOW["jobs"]))
def test_every_action_is_pinned_to_a_commit(job: str) -> None:
    for step in WORKFLOW["jobs"][job]["steps"]:
        if "uses" in step:
            assert re.search(r"@[0-9a-f]{40}( |$)", step["uses"]), step["uses"]
