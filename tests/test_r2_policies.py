"""PLAN.md R2 (ADR-0018): what the deploy role and a bounded workload role can and cannot do.

The policies are the real templates rendered with synthetic values (r2_policies), evaluated by
iam_eval. The same scenarios are run against AWS's evaluator, `aws iam simulate-custom-policy`,
and recorded in ADR-0018.
"""

import json
import re

import hcl2
import pytest
import r2_policies as r2
from hcl2.utils import SerializationOptions
from iam_eval import ALLOWED, EXPLICIT_DENY, IMPLICIT_DENY, as_list, decide

ROLE = f"arn:aws:iam::{r2.ACCOUNT}:role/ecp-batch-task"
BOUNDED = {"iam:PermissionsBoundary": r2.BOUNDARY_ARN}
READONLY = "arn:aws:iam::aws:policy/ReadOnlyAccess"


def as_deploy(action: str, resource: str, context: dict[str, str] | None = None) -> str:
    return decide(r2.deploy_identity(), action, resource, context)


# --- the templates are the Terraform inputs ---------------------------------------------------


def templatefile_keys(tf: str, name: str) -> set[str]:
    """The variable names a .tf file passes to templatefile() for policies/<name>.json.tftpl."""
    with (r2.ROOT / "infra" / "bootstrap" / tf).open() as fh:
        doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
    call = re.search(rf'{name}\.json\.tftpl\\", \{{([^}}]*)\}}', json.dumps(doc))
    assert call, f"{tf} does not render {name}"
    return set(re.findall(r"(\w+) =", call.group(1)))


@pytest.mark.parametrize(
    ("tf", "name"), [("github_oidc.tf", "deploy-iam"), ("boundary.tf", "workload-boundary")]
)
def test_terraform_passes_exactly_the_template_placeholders(tf: str, name: str) -> None:
    assert templatefile_keys(tf, name) == r2.placeholders(name)


def test_the_renderer_refuses_what_it_cannot_render_like_terraform() -> None:
    with pytest.raises(ValueError, match="unknown placeholder"):
        r2.render("deploy-iam", {k: v for k, v in r2.VALUES.items() if k != "boundary_arn"})


@pytest.mark.parametrize("policy", [r2.render("deploy-iam"), r2.boundary()])
def test_no_policy_allows_everything_on_everything(policy: dict) -> None:
    """ADR-0006 rule 4 applies to the boundary too: it lists services."""
    for stmt in policy["Statement"]:
        if stmt["Effect"] == "Allow" and "Action" in stmt:
            assert not ("*" in as_list(stmt["Action"]) and "*" in as_list(stmt["Resource"]))


# --- the deploy role --------------------------------------------------------------------------

DEPLOY_CASES = [
    # F-3: roles only with the boundary; a missing key counts as "not the boundary"
    ("iam:CreateRole", ROLE, BOUNDED, ALLOWED),
    ("iam:CreateRole", ROLE, {}, EXPLICIT_DENY),
    ("iam:CreateRole", ROLE, {"iam:PermissionsBoundary": READONLY}, EXPLICIT_DENY),
    ("iam:CreateRole", ROLE, {"iam:PermissionsBoundary": r2.BOUNDARY_ARN + "-v2"}, EXPLICIT_DENY),
    ("iam:PutRolePermissionsBoundary", ROLE, {"iam:PermissionsBoundary": READONLY}, EXPLICIT_DENY),
    ("iam:PutRolePermissionsBoundary", ROLE, BOUNDED, ALLOWED),
    ("iam:PutRolePolicy", ROLE, {}, EXPLICIT_DENY),
    ("iam:PutRolePolicy", ROLE, BOUNDED, ALLOWED),
    ("iam:AttachRolePolicy", ROLE, {"iam:PolicyARN": READONLY}, EXPLICIT_DENY),
    ("iam:AttachRolePolicy", ROLE, {**BOUNDED, "iam:PolicyARN": READONLY}, ALLOWED),
    # F-6: never AdministratorAccess, boundary or not
    ("iam:AttachRolePolicy", ROLE, {**BOUNDED, "iam:PolicyARN": r2.ADMIN}, EXPLICIT_DENY),
    # F-4: the boundary cannot be removed
    ("iam:DeleteRolePermissionsBoundary", ROLE, BOUNDED, EXPLICIT_DENY),
    # F-5: the boundary policy is frozen, readable
    ("iam:CreatePolicyVersion", r2.BOUNDARY_ARN, {}, EXPLICIT_DENY),
    ("iam:SetDefaultPolicyVersion", r2.BOUNDARY_ARN, {}, EXPLICIT_DENY),
    ("iam:DeletePolicyVersion", r2.BOUNDARY_ARN, {}, EXPLICIT_DENY),
    ("iam:DeletePolicy", r2.BOUNDARY_ARN, {}, EXPLICIT_DENY),
    ("iam:TagPolicy", r2.BOUNDARY_ARN, {}, EXPLICIT_DENY),
    ("iam:GetPolicyVersion", r2.BOUNDARY_ARN, {}, ALLOWED),
    # F-7: no Identity Center
    ("sso:CreatePermissionSet", "*", {}, EXPLICIT_DENY),
    ("identitystore:CreateUser", "*", {}, EXPLICIT_DENY),
    # F-8: unchanged scope and self-protection
    ("iam:CreateRole", "arn:aws:iam::123456789012:role/other", BOUNDED, IMPLICIT_DENY),
    ("iam:PutRolePolicy", r2.DEPLOY_ROLE_ARN, BOUNDED, EXPLICIT_DENY),
    ("iam:UpdateAssumeRolePolicy", r2.PLAN_ROLE_ARN, {}, EXPLICIT_DENY),
    ("iam:PassRole", ROLE, {}, ALLOWED),
    ("iam:CreatePolicy", f"arn:aws:iam::{r2.ACCOUNT}:policy/ecp-batch-read", {}, ALLOWED),
    ("s3:PutObject", f"arn:aws:s3:::{r2.STATE_BUCKET}/batch/terraform.tfstate", {}, ALLOWED),
]


@pytest.mark.parametrize(("action", "resource", "context", "expected"), DEPLOY_CASES)
def test_the_deploy_role(action: str, resource: str, context: dict, expected: str) -> None:
    assert as_deploy(action, resource, context) == expected


def test_terraform_can_still_destroy_a_bounded_role() -> None:
    """terraform-provider-aws 6.67.0 deletes a role by detaching managed policies, deleting
    inline policies, then DeleteRole; it never calls DeleteRolePermissionsBoundary on destroy."""
    for action, ctx in [
        ("iam:DetachRolePolicy", {**BOUNDED, "iam:PolicyARN": READONLY}),
        ("iam:DeleteRolePolicy", BOUNDED),
        ("iam:DeleteRole", BOUNDED),
    ]:
        assert as_deploy(action, ROLE, ctx) == ALLOWED, action


# --- a role inside the boundary ---------------------------------------------------------------

EVERYTHING = [{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*",
                                                        "Resource": "*"}]}]  # fmt: skip
STATE = f"arn:aws:s3:::{r2.STATE_BUCKET}"

BOUNDED_CASES = [
    ("s3:GetObject", "arn:aws:s3:::ecp-data/silver/x.parquet", ALLOWED),
    ("states:StartExecution", "*", ALLOWED),
    ("glue:StartJobRun", "*", ALLOWED),
    ("iam:PassRole", ROLE, ALLOWED),
    ("iam:PassRole", "arn:aws:iam::123456789012:role/other", IMPLICIT_DENY),
    ("iam:CreateRole", ROLE, IMPLICIT_DENY),
    ("iam:AttachRolePolicy", ROLE, IMPLICIT_DENY),
    ("sts:AssumeRole", "*", IMPLICIT_DENY),
    ("organizations:ListAccounts", "*", IMPLICIT_DENY),
    ("sso:CreatePermissionSet", "*", IMPLICIT_DENY),
    ("s3:GetObject", f"{STATE}/bootstrap/terraform.tfstate", EXPLICIT_DENY),
    ("s3:ListBucket", STATE, EXPLICIT_DENY),
]


@pytest.mark.parametrize(("action", "resource", "expected"), BOUNDED_CASES)
def test_a_bounded_role_with_administrator_access(
    action: str, resource: str, expected: str
) -> None:
    assert decide(EVERYTHING, action, resource, boundary=r2.boundary()) == expected
