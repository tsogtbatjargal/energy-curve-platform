"""PLAN.md R2 for the batch stack (ADR-0018, ADR-0022): each role can do its job, and no more.

Each role's inline policy is the real template (infra/batch/policies) rendered with synthetic
values, evaluated by iam_eval together with the real workload boundary. Every action a role needs
must be allowed inside the boundary; the nearby actions it must not have are denied. The same
scenarios run against AWS's evaluator (`aws iam simulate-custom-policy`, read-only) before the
stage-1 apply.
"""

import json
import re
from pathlib import Path

import hcl2
import policy_templates
import pytest
import r2_policies as r2
from hcl2.utils import SerializationOptions
from iam_eval import ALLOWED, EXPLICIT_DENY, as_list, decide

ACCOUNT, REGION = r2.ACCOUNT, "ca-central-1"
STACK = Path(__file__).parents[1] / "infra" / "batch"
POLICIES = STACK / "policies"
VALUES = {"account_id": ACCOUNT, "region": REGION}
ROLES = ("stage", "task", "exec", "sfn", "scheduler")
BUCKET = f"arn:aws:s3:::ecp-data-{ACCOUNT}-{REGION}"
REPO = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/ecp-batch"
CLUSTER = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/ecp-batch"
FUNCTION = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:ecp-batch-stage"
STATE_MACHINE = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:ecp-batch"
SYNC_RULE = f"arn:aws:events:{REGION}:{ACCOUNT}:rule/StepFunctionsGetEventsForECSTaskRule"
STATE_BUCKET = r2.VALUES["state_bucket_arn"]


def policy(name: str) -> dict:
    return policy_templates.render(name, VALUES, POLICIES)


def as_role(role: str, action: str, resource: str, context: dict | None = None) -> str:
    return decide([policy(f"{role}-policy")], action, resource, context, boundary=r2.boundary())


def role_arn(name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/ecp-batch-{name}"


# --- the templates are the Terraform inputs ---------------------------------------------------


def test_the_templates_are_exactly_the_roles_and_the_repository_policy() -> None:
    names = {p.name.removesuffix(".json.tftpl") for p in POLICIES.glob("*.json.tftpl")}
    roles = {f"{r}-{kind}" for r in ROLES for kind in ("trust", "policy")}
    assert names == roles | {"ecr-repository"}


def test_terraform_passes_exactly_the_placeholders_the_templates_use() -> None:
    with (STACK / "locals.tf").open() as fh:
        doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
    values = next(b["policy_values"] for b in doc["locals"] if "policy_values" in b)
    passed = {k.strip('"') for k in values}
    used = set().union(*(policy_templates.placeholders(p.name.removesuffix(".json.tftpl"),
                                                       POLICIES)
                         for p in POLICIES.glob("*.json.tftpl")))  # fmt: skip
    assert passed == used == {"account_id", "region"}


def test_the_roles_terraform_creates_are_the_tested_roles() -> None:
    text = (STACK / "iam.tf").read_text()
    roles = re.search(r"roles = toset\(\[([^\]]*)\]\)", text)
    assert roles and set(re.findall(r'"(\w+)"', roles.group(1))) == set(ROLES)


@pytest.mark.parametrize("name", [*(f"{r}-policy" for r in ROLES), "ecr-repository"])
def test_no_policy_allows_everything_on_everything(name: str) -> None:
    for stmt in policy(name)["Statement"]:
        if stmt["Effect"] == "Allow":
            assert not ("*" in as_list(stmt["Action"]) and "*" in as_list(stmt.get("Resource")))


# --- trust: one service each, confused-deputy conditions where the service documents them -------

TRUST = {
    "stage": ("lambda.amazonaws.com", None),
    "task": ("ecs-tasks.amazonaws.com", f"arn:aws:ecs:{REGION}:{ACCOUNT}:*"),
    "exec": ("ecs-tasks.amazonaws.com", f"arn:aws:ecs:{REGION}:{ACCOUNT}:*"),
    "sfn": ("states.amazonaws.com", STATE_MACHINE),
    "scheduler": ("scheduler.amazonaws.com", None),
}


@pytest.mark.parametrize("role", ROLES)
def test_each_role_trusts_one_service_only(role: str) -> None:
    service, source_arn = TRUST[role]
    (stmt,) = policy(f"{role}-trust")["Statement"]
    assert stmt["Principal"] == {"Service": service}
    assert stmt["Action"] == "sts:AssumeRole"
    condition = stmt.get("Condition", {})
    if role != "stage":  # Lambda's documented execution-role trust has no condition
        assert condition["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    if source_arn:
        assert condition["ArnLike"] == {"aws:SourceArn": source_arn}


def test_only_the_stage_function_may_pull_through_the_repository_policy() -> None:
    (stmt,) = policy("ecr-repository")["Statement"]
    assert stmt["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert sorted(stmt["Action"]) == ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    assert stmt["Condition"] == {"ArnLike": {"aws:sourceArn": FUNCTION}}


# --- each role's job is allowed inside the boundary -------------------------------------------

NEEDED = [
    ("stage", "s3:PutObject", f"{BUCKET}/store/staging/run-1/page-0001.json", None),
    ("stage", "logs:PutLogEvents",
     f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/ecp-batch-stage:log-stream:s", None),
    ("task", "s3:GetObject", f"{BUCKET}/store/staging/run-1/requests.json", None),
    ("task", "s3:PutObject", f"{BUCKET}/store/pointer.json", None),
    ("task", "s3:ListBucket", BUCKET, None),
    ("exec", "ecr:GetAuthorizationToken", "*", None),
    ("exec", "ecr:BatchGetImage", REPO, None),
    ("exec", "ecr:GetDownloadUrlForLayer", REPO, None),
    ("exec", "logs:CreateLogStream",
     f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/ecs/ecp-batch-pipeline:log-stream:p", None),
    ("sfn", "lambda:InvokeFunction", FUNCTION, None),
    ("sfn", "ecs:RunTask", f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/ecp-batch-pipeline:3",
     {"ecs:cluster": CLUSTER}),
    ("sfn", "ecs:DescribeTasks", f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/ecp-batch/abc123", None),
    ("sfn", "events:PutRule", SYNC_RULE, None),
    ("sfn", "iam:PassRole", role_arn("exec"), {"iam:PassedToService": "ecs-tasks.amazonaws.com"}),
    ("sfn", "iam:PassRole", role_arn("task"), {"iam:PassedToService": "ecs-tasks.amazonaws.com"}),
    ("scheduler", "states:StartExecution", STATE_MACHINE, None),
]  # fmt: skip


@pytest.mark.parametrize(("role", "action", "resource", "context"), NEEDED)
def test_each_role_can_do_its_job_inside_the_boundary(
    role: str, action: str, resource: str, context: dict | None
) -> None:
    assert as_role(role, action, resource, context) == ALLOWED


# --- and nothing near it ----------------------------------------------------------------------

NOT_ALLOWED = [
    ("stage", "s3:PutObject", f"{BUCKET}/store/pointer.json", None),
    ("stage", "s3:GetObject", f"{BUCKET}/store/staging/run-1/requests.json", None),
    ("stage", "ecr:BatchGetImage", REPO, None),
    ("task", "s3:DeleteObject", f"{BUCKET}/store/pointer.json", None),
    ("task", "s3:PutObject", f"{BUCKET}/elsewhere.json", None),
    ("task", "s3:GetObject", "arn:aws:s3:::another-bucket/store/x", None),
    ("exec", "ecr:BatchGetImage", f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/other", None),
    ("exec", "ecr:PutImage", REPO, None),
    ("sfn", "ecs:RunTask", f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/ecp-batch-pipeline:3",
     {"ecs:cluster": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/other"}),
    ("sfn", "ecs:RunTask", f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/other:1",
     {"ecs:cluster": CLUSTER}),
    ("sfn", "iam:PassRole", role_arn("sfn"), {"iam:PassedToService": "ecs-tasks.amazonaws.com"}),
    ("sfn", "iam:PassRole", role_arn("exec"), {"iam:PassedToService": "lambda.amazonaws.com"}),
    ("sfn", "lambda:InvokeFunction", f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:other", None),
    ("scheduler", "states:StartExecution",
     f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:other", None),
]  # fmt: skip


@pytest.mark.parametrize(("role", "action", "resource", "context"), NOT_ALLOWED)
def test_each_role_cannot_do_more(
    role: str, action: str, resource: str, context: dict | None
) -> None:
    assert as_role(role, action, resource, context) != ALLOWED


@pytest.mark.parametrize("role", ROLES)
def test_no_role_can_touch_the_terraform_state(role: str) -> None:
    """The boundary's explicit deny holds even if a policy were widened to s3:* on *."""
    wide = {"Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}  # fmt: skip
    for action in ("s3:GetObject", "s3:PutObject"):
        verdict = decide([policy(f"{role}-policy"), wide], action, f"{STATE_BUCKET}/batch/x",
                         boundary=r2.boundary())  # fmt: skip
        assert verdict == EXPLICIT_DENY


def test_the_reviewed_documents_are_valid_json_as_terraform_renders_them() -> None:
    for p in POLICIES.glob("*.json.tftpl"):
        rendered = policy(p.name.removesuffix(".json.tftpl"))
        assert json.loads(json.dumps(rendered))["Version"] == "2012-10-17"
