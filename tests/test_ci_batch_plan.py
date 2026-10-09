"""M4c-2 (ADR-0022): CI plans infra/batch with the member plan role, and prints only a summary.

The workflow runs on a public repository, so what it may print is part of its contract: the
batch plan goes to a file, only the `Plan:` / `No changes` line (and the first line of an
error, with 12-digit numbers masked) reaches the log, and the files are removed afterwards. No
secret is needed for the stack's inputs: the alert address is a placeholder, the digest and the
schedule flag are tracked. The plan role ARN and the state bucket name hold the account ID, which
the public log must not show, so they are secrets (repository and Dependabot), not variables.
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


SECRETS_USED = {"AWS_PLAN_ROLE_ARN", "TF_STATE_BUCKET"}
VARIABLES_USED = {"AWS_REGION", "TF_MEMBER_INSTANCE"}
JOB_TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
JOB_TEXT = JOB_TEXT[JOB_TEXT.index("  terraform-plan:") :]


def index_of(name: str) -> int:
    """A step by its name, or by the action it uses (the credentials step has no name)."""
    return next(
        i for i, s in enumerate(JOB["steps"]) if s.get("name") == name or name in s.get("uses", "")
    )


def test_the_alert_address_is_a_placeholder() -> None:
    assert JOB["env"]["TF_VAR_alert_email"] == "ci@example.invalid"


def test_the_job_uses_exactly_these_secrets_and_variables() -> None:
    dumped = yaml.safe_dump(JOB)
    assert set(re.findall(r"secrets\.(\w+)", dumped)) == SECRETS_USED
    assert set(re.findall(r"vars\.(\w+)", dumped)) == VARIABLES_USED


def test_the_account_id_values_are_secrets_not_variables() -> None:
    assert JOB["env"]["TF_STATE_BUCKET"] == "${{ secrets.TF_STATE_BUCKET }}"
    creds = JOB["steps"][index_of("configure-aws-credentials")]["with"]
    assert creds["role-to-assume"] == "${{ secrets.AWS_PLAN_ROLE_ARN }}"
    assert creds["mask-aws-account-id"] is True
    expected = STEPS["expected account"]["env"]
    assert expected == {"AWS_PLAN_ROLE_ARN": "${{ secrets.AWS_PLAN_ROLE_ARN }}"}


def test_the_account_id_is_derived_only_after_the_credentials_step_masks_it() -> None:
    # Derived values are not masked by GitHub; mask-aws-account-id registers the account ID.
    assert index_of("configure-aws-credentials") < index_of("expected account")
    assert index_of("expected account") < index_of("terraform init")


def test_no_step_prints_the_secrets() -> None:
    names = "|".join(SECRETS_USED)
    for step in JOB["steps"]:
        run = step.get("run", "")
        for line in run.splitlines():
            if re.search(r"(echo|printf)\b", line) and re.search(rf"\$\{{?({names})\b", line):
                # the one allowed use is the derivation that writes to GITHUB_ENV, never to the log
                assert step["name"] == "expected account", step.get("name")
                assert line.rstrip().endswith('>> "$GITHUB_ENV"'), line


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
