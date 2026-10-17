"""CI plans the one live stack, bootstrap, with the member plan role.

The batch, Glue and Redshift stacks were torn down at the project's close (2026-10-17), so their
informational plan jobs are gone; the matrix keeps the required `terraform-plan (bootstrap)`
check. The workflow runs on a public repository, so what it may print is part of its contract:
the plan role ARN and the state bucket name hold the account ID, which the public log must not
show, so they are secrets (repository and Dependabot), not variables, and plan files are removed
afterwards.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
JOB = WORKFLOW["jobs"]["terraform-plan"]
STEPS = {s["name"]: s for s in JOB["steps"] if "name" in s}
BOOTSTRAP_ONLY = "matrix.stack == 'bootstrap'"


def test_only_the_bootstrap_stack_is_planned() -> None:
    assert JOB["strategy"]["matrix"]["stack"] == ["bootstrap"]
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


def test_the_bootstrap_plan_is_unchanged() -> None:
    step = next(
        s for s in JOB["steps"] if s.get("if") == BOOTSTRAP_ONLY and "terraform plan" in s["run"]
    )
    assert "terraform plan -input=false -lock-timeout=60s -out=tfplan" in step["run"]
    assert "terraform show -json tfplan > plan.json" in step["run"]


def test_the_only_policy_gate_is_the_bootstraps() -> None:
    gates = [s for s in JOB["steps"] if "policy_gate.py" in s.get("run", "")]
    assert [g.get("if") for g in gates] == [BOOTSTRAP_ONLY]
    assert '--stack "$STACK"' in gates[0]["run"]


def test_no_step_plans_a_torn_down_stack() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    job = text[text.index("  terraform-plan:") :]
    for stack in ("batch", "glue", "redshift"):
        assert f"matrix.stack == '{stack}'" not in job
    assert "TF_VAR_alert_email" not in job
    assert "build_glue_bundle" not in job


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


def test_the_glue_stack_is_tested_offline_in_the_static_job() -> None:
    steps = WORKFLOW["jobs"]["iac-static"]["steps"]
    step = next(s for s in steps if s.get("name") == "terraform test (glue, mocked provider)")
    run = step["run"]
    assert run.index("build_glue_bundle.py") < run.index("init -backend=false") < run.index("test")
    assert "terraform -chdir=infra/glue test" in run


def test_the_redshift_stack_is_tested_offline_in_the_static_job() -> None:
    steps = WORKFLOW["jobs"]["iac-static"]["steps"]
    step = next(s for s in steps if s.get("name") == "terraform test (redshift, mocked provider)")
    run = step["run"]
    assert run.index("init -backend=false") < run.index("test")
    assert "terraform -chdir=infra/redshift test" in run


def test_the_plan_role_needs_no_change_for_the_redshift_plan() -> None:
    """The plan reads only: the role is ReadOnlyAccess (infra/bootstrap), which covers
    ec2:DescribeAvailabilityZones, organizations:DescribeOrganization and sts:GetCallerIdentity.
    Nothing in the stack is a data source beyond those three."""
    stack = ROOT / "infra" / "redshift"
    text = "\n".join(p.read_text() for p in stack.glob("*.tf"))
    assert sorted(re.findall(r'^data "(\w+)"', text, re.MULTILINE)) == [
        "aws_availability_zones", "aws_caller_identity", "aws_organizations_organization",
    ]  # fmt: skip
