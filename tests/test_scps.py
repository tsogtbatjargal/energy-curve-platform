"""ADR-0021 phase 1b: what the Workloads SCPs deny, evaluated offline (tests/iam_eval.py).

Every case runs as a member-account principal whose identity policy allows everything, under
FullAWSAccess plus the two project SCPs, so only the SCPs decide. AWS's evaluator is the authority:
after the account exists, the same cases are simulated in it (ADR-0021 phase 1b acceptance).
"""

import json
from pathlib import Path

import hcl2
import pytest
from hcl2.utils import SerializationOptions
from iam_eval import ALLOWED, EXPLICIT_DENY, decide

ROOT = Path(__file__).parents[1]
POLICIES = ROOT / "infra" / "org" / "policies"
BASELINE = json.loads((POLICIES / "scp-workloads-baseline.json").read_text())
PROTECT = json.loads((POLICIES / "scp-workloads-protect.json").read_text())
ALLOW_ALL = {"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}  # FullAWSAccess
ACCOUNT = "210987654321"
ADMIN = (f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/ca-central-1/"
         "AWSReservedSSO_AdministratorAccess_0123456789abcdef")  # fmt: skip
ADMIN_NO_REGION = (f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/"
                   "AWSReservedSSO_AdministratorAccess_0123456789abcdef")  # fmt: skip
READONLY_SSO = (f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/ca-central-1/"
                "AWSReservedSSO_ecp-readonly_0123456789abcdef")  # fmt: skip
DEPLOY = f"arn:aws:iam::{ACCOUNT}:role/ecp-gha-deploy"
BREAK_GLASS = f"arn:aws:iam::{ACCOUNT}:role/OrganizationAccountAccessRole"
ROOT_USER = f"arn:aws:iam::{ACCOUNT}:root"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/ecp-workload-boundary"


def as_principal(principal: str, action: str, resource: str = "*", region: str = "ca-central-1",
                 assumed_root: bool = False) -> str:  # fmt: skip
    ctx = {"aws:PrincipalArn": principal, "aws:RequestedRegion": region}
    if assumed_root:
        ctx["aws:AssumedRoot"] = "true"  # present only in AssumeRoot sessions
    return decide([ALLOW_ALL, BASELINE, PROTECT], action, resource, ctx)


# --- baseline: regions, the organization, the root user ---------------------------------------


@pytest.mark.parametrize("region", ["ca-central-1", "us-east-1"])
def test_the_allowed_regions_are_open(region: str) -> None:
    assert as_principal(DEPLOY, "ecs:RunTask", region=region) == ALLOWED


@pytest.mark.parametrize("region", ["us-west-2", "eu-west-1", "ca-west-1"])
def test_other_regions_are_denied(region: str) -> None:
    assert as_principal(DEPLOY, "ecs:RunTask", region=region) == EXPLICIT_DENY
    assert as_principal(ADMIN, "s3:CreateBucket", region=region) == EXPLICIT_DENY
    assert as_principal(DEPLOY, "sts:AssumeRole", region=region) == EXPLICIT_DENY


@pytest.mark.parametrize("action", ["iam:CreateRole", "cloudfront:CreateDistribution",
                                    "route53:ChangeResourceRecordSets", "support:CreateCase",
                                    "organizations:DescribeOrganization"])  # fmt: skip
def test_global_services_are_exempt_from_the_region_rule(action: str) -> None:
    assert as_principal(ADMIN, action, region="us-west-2") == ALLOWED


@pytest.mark.parametrize("principal", [ADMIN, DEPLOY, BREAK_GLASS])
def test_nobody_can_leave_the_organization(principal: str) -> None:
    assert as_principal(principal, "organizations:LeaveOrganization") == EXPLICIT_DENY


def test_the_long_term_root_user_is_denied() -> None:
    assert as_principal(ROOT_USER, "s3:GetObject") == EXPLICIT_DENY
    assert as_principal(ROOT_USER, "iam:CreateLoginProfile") == EXPLICIT_DENY


def test_an_assume_root_session_is_not_denied_by_the_root_rule() -> None:
    """Centralized root access (phase 1c) must keep working: AssumeRoot sessions carry
    aws:AssumedRoot, so AWS's pattern lets them through."""
    assert as_principal(ROOT_USER, "s3:PutBucketPolicy", assumed_root=True) == ALLOWED


def test_an_assume_root_session_still_obeys_the_region_rule() -> None:
    assert (as_principal(ROOT_USER, "s3:PutBucketPolicy", region="us-west-2",
                         assumed_root=True) == EXPLICIT_DENY)  # fmt: skip


# --- protect: the break-glass role and the R2 boundary -----------------------------------------

BREAK_GLASS_WRITES = ["iam:UpdateAssumeRolePolicy", "iam:AttachRolePolicy", "iam:PutRolePolicy",
                      "iam:DeleteRole", "iam:DetachRolePolicy", "iam:UpdateRole",
                      "iam:PutRolePermissionsBoundary", "iam:TagRole"]  # fmt: skip


@pytest.mark.parametrize("action", BREAK_GLASS_WRITES)
@pytest.mark.parametrize("principal", [DEPLOY, BREAK_GLASS, READONLY_SSO, ROOT_USER])
def test_only_the_admin_can_change_the_break_glass_role(principal: str, action: str) -> None:
    assert as_principal(principal, action, BREAK_GLASS) == EXPLICIT_DENY


@pytest.mark.parametrize("principal", [ADMIN, ADMIN_NO_REGION])
@pytest.mark.parametrize("action", BREAK_GLASS_WRITES)
def test_the_admin_can_change_the_break_glass_role(principal: str, action: str) -> None:
    assert as_principal(principal, action, BREAK_GLASS) == ALLOWED


def test_the_lock_covers_only_the_break_glass_role() -> None:
    other = f"arn:aws:iam::{ACCOUNT}:role/ecp-batch-task"
    assert as_principal(DEPLOY, "iam:PutRolePolicy", other) == ALLOWED


@pytest.mark.parametrize("principal", [ADMIN, DEPLOY, BREAK_GLASS])
def test_no_one_can_remove_a_permissions_boundary(principal: str) -> None:
    role = f"arn:aws:iam::{ACCOUNT}:role/ecp-batch-task"
    assert as_principal(principal, "iam:DeleteRolePermissionsBoundary", role) == EXPLICIT_DENY


BOUNDARY_WRITES = ["iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicy",
                   "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion",
                   "iam:TagPolicy"]  # fmt: skip


@pytest.mark.parametrize("action", BOUNDARY_WRITES)
def test_only_the_admin_can_write_the_workload_boundary(action: str) -> None:
    for principal in (DEPLOY, BREAK_GLASS, READONLY_SSO):
        assert as_principal(principal, action, BOUNDARY) == EXPLICIT_DENY, principal
    assert as_principal(ADMIN, action, BOUNDARY) == ALLOWED


def test_other_policies_are_not_frozen() -> None:
    other = f"arn:aws:iam::{ACCOUNT}:policy/ecp-batch-task"
    assert as_principal(DEPLOY, "iam:CreatePolicyVersion", other) == ALLOWED


# --- the documents Terraform deploys -----------------------------------------------------------


@pytest.mark.parametrize("doc", [BASELINE, PROTECT])
def test_each_scp_fits_the_size_limit(doc: dict) -> None:
    assert len(json.dumps(doc, separators=(",", ":"))) < 10_240  # Organizations quota for SCPs


def test_terraform_deploys_exactly_these_documents() -> None:
    with (ROOT / "infra" / "org" / "scps.tf").open() as fh:
        doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
    files = sorted(
        body["content"]
        for block in doc["resource"]
        for t, named in block.items()
        if t.strip('"') == "aws_organizations_policy"
        for body in named.values()
    )
    assert len(files) == 2
    assert "scp-workloads-baseline.json" in files[0] and "scp-workloads-protect.json" in files[1]
