"""ADR-0021 phase 1c: what the AssumeRoot scoping on AdministratorAccess allows, offline.

The admin's Identity Center role has AdministratorAccess plus the inline scoping policy, rendered
from infra/org/policies/admin-assume-root.json.tftpl. AWS's evaluator is the authority: after the
apply, the same cases run through `simulate-principal-policy` (ADR-0021 phase 1c acceptance).
"""

import phase1c_plan_check as p1c
import pytest
from iam_eval import ALLOWED, EXPLICIT_DENY, decide

WORKLOADS, OTHER = "210987654321", "123456789012"
ADMINISTRATOR_ACCESS = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
SCOPE = p1c.assume_root_policy(WORKLOADS)
TASKS = [
    "IAMAuditRootUserCredentials",
    "IAMCreateRootUserPassword",
    "IAMDeleteRootUserCredentials",
    "S3UnlockBucketPolicy",
    "SQSUnlockQueuePolicy",
]


def task(name: str) -> str:
    return f"arn:aws:iam::aws:policy/root-task/{name}"


def assume_root(account: str, task_arn: str | None) -> str:
    context = {} if task_arn is None else {"sts:TaskPolicyArn": task_arn}
    return decide([ADMINISTRATOR_ACCESS, SCOPE], "sts:AssumeRoot",
                  f"arn:aws:iam::{account}:root", context)  # fmt: skip


@pytest.mark.parametrize("name", TASKS)
def test_each_of_the_five_tasks_is_allowed_into_the_workload_account(name: str) -> None:
    assert assume_root(WORKLOADS, task(name)) == ALLOWED


@pytest.mark.parametrize("name", TASKS)
def test_another_account_is_denied_for_every_task(name: str) -> None:
    assert assume_root(OTHER, task(name)) == EXPLICIT_DENY


@pytest.mark.parametrize("task_arn", [task("Other"), "arn:aws:iam::aws:policy/AdministratorAccess",
                                      f"arn:aws:iam::{WORKLOADS}:policy/root-task/S3UnlockBucketPolicy",
                                      None])  # fmt: skip
def test_any_other_task_policy_is_denied(task_arn: str | None) -> None:
    assert assume_root(WORKLOADS, task_arn) == EXPLICIT_DENY


def test_a_wildcard_target_is_denied() -> None:
    assert assume_root("*", task("IAMAuditRootUserCredentials")) == EXPLICIT_DENY


@pytest.mark.parametrize(("action", "resource"), [
    ("sts:AssumeRole", f"arn:aws:iam::{WORKLOADS}:role/OrganizationAccountAccessRole"),
    ("iam:CreateRole", "*"),
    ("organizations:DescribeOrganization", "*"),
])  # fmt: skip
def test_nothing_but_assume_root_changes(action: str, resource: str) -> None:
    assert decide([ADMINISTRATOR_ACCESS, SCOPE], action, resource) == ALLOWED


def test_the_template_names_exactly_the_five_aws_task_policies() -> None:
    condition = SCOPE["Statement"][1]["Condition"]["ArnNotEquals"]["sts:TaskPolicyArn"]
    assert condition == [task(name) for name in TASKS]
    assert {s["Effect"] for s in SCOPE["Statement"]} == {"Deny"}
