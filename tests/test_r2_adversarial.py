"""Adversarial black-box tests for PLAN.md R2 (F-2 to F-8), written from the frozen requirements.

The deploy role (ecp-gha-deploy, no boundary of its own) tries to escalate; a role bounded by
ecp-workload-boundary with AdministratorAccess as its identity policy probes the boundary itself.
All values are synthetic (account 123456789012).
"""

import iam_eval
import pytest
import r2_policies

ACCOUNT = r2_policies.ACCOUNT
BOUNDARY = r2_policies.BOUNDARY_ARN
ADMIN = r2_policies.ADMIN
STATE_BUCKET = r2_policies.STATE_BUCKET
REGION = "ca-central-1"
IAM = f"arn:aws:iam::{ACCOUNT}"
STATE = f"arn:aws:s3:::{STATE_BUCKET}"


def arn(service: str, rest: str) -> str:
    return f"arn:aws:{service}:{REGION}:{ACCOUNT}:{rest}"


PB = "iam:PermissionsBoundary"
POLICY_ARN = "iam:PolicyARN"

ECP_ROLE = f"arn:aws:iam::{ACCOUNT}:role/ecp-batch-task"
OTHER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/team-other"
S3_READ = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"

ADMIN_DOC = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
}

LOOKALIKES = [
    f"arn:aws:iam::{ACCOUNT}:policy/ecp-workload-boundary-old",
    f"arn:aws:iam::{ACCOUNT}:policy/ecp-other",
    f"arn:aws:iam::{ACCOUNT}:policy/ECP-Workload-Boundary",
    f"arn:aws:iam::{ACCOUNT}:policy/team/ecp-workload-boundary",
    f"{BOUNDARY} ",
    f" {BOUNDARY}",
    "arn:aws:iam::210987654321:policy/ecp-workload-boundary",
    "arn:aws:iam::aws:policy/ecp-workload-boundary",
    f"arn:aws-us-gov:iam::{ACCOUNT}:policy/ecp-workload-boundary",
    ADMIN,
    "",
]

GUARDED = [
    "iam:CreateRole",
    "iam:PutRolePermissionsBoundary",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
]


def deploy(action: str, resource: str, context: dict | None = None) -> str:
    return iam_eval.decide(r2_policies.deploy_identity(), action, resource, context, None)


def bounded_admin(action: str, resource: str, context: dict | None = None) -> str:
    return iam_eval.decide([ADMIN_DOC], action, resource, context, r2_policies.boundary())


def guarded_context(action: str, boundary: str | None) -> dict:
    ctx = {} if boundary is None else {PB: boundary}
    if action in {"iam:AttachRolePolicy", "iam:DetachRolePolicy"}:
        ctx[POLICY_ARN] = S3_READ
    return ctx


# --- synthetic constants (F-1, F-8) ---


def test_constants_are_synthetic_and_named_per_f1() -> None:
    assert ACCOUNT == "123456789012"
    assert BOUNDARY == "arn:aws:iam::123456789012:policy/ecp-workload-boundary"
    assert ADMIN == "arn:aws:iam::aws:policy/AdministratorAccess"
    assert r2_policies.DEPLOY_ROLE_ARN == "arn:aws:iam::123456789012:role/ecp-gha-deploy"
    assert r2_policies.PLAN_ROLE_ARN == "arn:aws:iam::123456789012:role/ecp-gha-plan"


# --- F-3: guarded role actions need exactly the boundary ---


@pytest.mark.parametrize("action", GUARDED)
def test_guarded_action_without_boundary_key_explicitly_denied(action: str) -> None:
    assert deploy(action, ECP_ROLE, guarded_context(action, None)) == "explicitDeny"


@pytest.mark.parametrize("boundary", LOOKALIKES)
@pytest.mark.parametrize("action", GUARDED)
def test_guarded_action_with_lookalike_boundary_explicitly_denied(
    action: str, boundary: str
) -> None:
    assert deploy(action, ECP_ROLE, guarded_context(action, boundary)) == "explicitDeny"


@pytest.mark.parametrize("action", GUARDED)
def test_guarded_action_with_boundary_allowed_on_ecp_role(action: str) -> None:
    assert deploy(action, ECP_ROLE, guarded_context(action, BOUNDARY)) == "allowed"


@pytest.mark.parametrize("action", GUARDED)
def test_guarded_action_with_boundary_still_needs_ecp_name(action: str) -> None:
    assert deploy(action, OTHER_ROLE, guarded_context(action, BOUNDARY)) != "allowed"


def test_replace_boundary_with_another_denied() -> None:
    other = f"arn:aws:iam::{ACCOUNT}:policy/ecp-permissive"
    assert deploy("iam:PutRolePermissionsBoundary", ECP_ROLE, {PB: other}) == "explicitDeny"


def test_inline_policy_on_unbounded_role_denied() -> None:
    assert deploy("iam:PutRolePolicy", ECP_ROLE) == "explicitDeny"
    assert deploy("iam:PutRolePolicy", ECP_ROLE, {PB: ""}) == "explicitDeny"


# --- F-4: no boundary removal, even on a bounded role ---


@pytest.mark.parametrize("context", [None, {}, {PB: BOUNDARY}, {PB: LOOKALIKES[0]}])
def test_delete_role_permissions_boundary_always_denied(context: dict | None) -> None:
    assert deploy("iam:DeleteRolePermissionsBoundary", ECP_ROLE, context) == "explicitDeny"


# --- F-5: the boundary policy is frozen except for Get*/List* ---


@pytest.mark.parametrize(
    "action",
    [
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:SetDefaultPolicyVersion",
        "iam:DeletePolicyVersion",
        "iam:DeletePolicy",
        "iam:TagPolicy",
        "iam:UntagPolicy",
    ],
)
def test_boundary_policy_mutation_denied(action: str) -> None:
    assert deploy(action, BOUNDARY) == "explicitDeny"
    assert deploy(action, BOUNDARY, {PB: BOUNDARY}) == "explicitDeny"


@pytest.mark.parametrize(
    "action",
    ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions", "iam:ListPolicyTags"],
)
def test_boundary_policy_reads_allowed(action: str) -> None:
    assert deploy(action, BOUNDARY) == "allowed"


def test_other_ecp_policy_still_editable() -> None:
    other = f"arn:aws:iam::{ACCOUNT}:policy/ecp-batch-task"
    assert deploy("iam:CreatePolicyVersion", other) == "allowed"


# --- F-6: no AdministratorAccess, bounded or not ---


@pytest.mark.parametrize("context", [{PB: BOUNDARY}, {}])
def test_attach_administrator_access_denied(context: dict) -> None:
    ctx = {**context, POLICY_ARN: ADMIN}
    assert deploy("iam:AttachRolePolicy", ECP_ROLE, ctx) == "explicitDeny"


def test_attach_non_admin_policy_to_bounded_role_allowed() -> None:
    ctx = {PB: BOUNDARY, POLICY_ARN: S3_READ}
    assert deploy("iam:AttachRolePolicy", ECP_ROLE, ctx) == "allowed"


# --- F-7: no Identity Center escalation ---


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        ("sso:CreatePermissionSet", "*"),
        ("sso:CreateAccountAssignment", "*"),
        ("sso:AttachManagedPolicyToPermissionSet", "arn:aws:sso:::permissionSet/ssoins-1/ps-1"),
        ("sso:PutInlinePolicyToPermissionSet", "*"),
        ("sso:ListInstances", "*"),
        ("sso-directory:CreateUser", "*"),
        ("identitystore:CreateUser", "*"),
        ("identitystore:CreateGroupMembership", f"arn:aws:identitystore::{ACCOUNT}:identitystore/"),
    ],
)
def test_identity_center_denied(action: str, resource: str) -> None:
    assert deploy(action, resource) == "explicitDeny"


# --- F-8: the CI roles cannot modify themselves, even by adopting the boundary ---


@pytest.mark.parametrize("target", [r2_policies.DEPLOY_ROLE_ARN, r2_policies.PLAN_ROLE_ARN])
@pytest.mark.parametrize(
    ("action", "context"),
    [
        ("iam:AttachRolePolicy", {PB: BOUNDARY, POLICY_ARN: S3_READ}),
        ("iam:PutRolePolicy", {PB: BOUNDARY}),
        ("iam:PutRolePermissionsBoundary", {PB: BOUNDARY}),
        ("iam:DetachRolePolicy", {PB: BOUNDARY, POLICY_ARN: S3_READ}),
        ("iam:UpdateAssumeRolePolicy", None),
        ("iam:DeleteRole", None),
        ("iam:TagRole", None),
        ("iam:UpdateRole", None),
    ],
)
def test_ci_roles_cannot_be_modified(target: str, action: str, context: dict | None) -> None:
    assert deploy(action, target, context) == "explicitDeny"


def test_create_bounded_ecp_role_allowed() -> None:
    assert deploy("iam:CreateRole", f"{IAM}:role/ecp-new", {PB: BOUNDARY}) == "allowed"


@pytest.mark.parametrize(
    ("action", "context"),
    [
        ("iam:GetRole", {PB: BOUNDARY}),
        ("iam:ListAttachedRolePolicies", None),
        ("iam:ListRolePolicies", None),
        ("iam:ListInstanceProfilesForRole", None),
        ("iam:DetachRolePolicy", {PB: BOUNDARY, POLICY_ARN: S3_READ}),
        ("iam:DeleteRolePolicy", {PB: BOUNDARY}),
        ("iam:DeleteRole", {PB: BOUNDARY}),
    ],
)
def test_bounded_role_destroy_sequence_allowed(action: str, context: dict | None) -> None:
    assert deploy(action, ECP_ROLE, context) == "allowed"


def test_inline_policy_put_and_delete_on_bounded_role_allowed() -> None:
    assert deploy("iam:PutRolePolicy", ECP_ROLE, {PB: BOUNDARY}) == "allowed"
    assert deploy("iam:DeleteRolePolicy", ECP_ROLE, {PB: BOUNDARY}) == "allowed"


def test_pass_ecp_role_allowed() -> None:
    assert deploy("iam:PassRole", ECP_ROLE) == "allowed"


def test_service_linked_role_creation_allowed() -> None:
    slr = f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS"
    assert deploy("iam:CreateServiceLinkedRole", slr) == "allowed"


@pytest.mark.parametrize("action", ["s3:PutObject", "s3:DeleteObject", "s3:GetObject"])
def test_deploy_role_keeps_state_access(action: str) -> None:
    assert deploy(action, f"arn:aws:s3:::{STATE_BUCKET}/batch/terraform.tfstate") == "allowed"


# --- F-2: the boundary itself, probed with AdministratorAccess as identity ---


def _as_list(x: object) -> list:
    return x if isinstance(x, list) else [x]


def test_boundary_has_no_broad_allow() -> None:
    doc = r2_policies.boundary()
    for stmt in _as_list(doc["Statement"]):
        if stmt["Effect"] != "Allow":
            continue
        assert "NotAction" not in stmt, stmt
        assert "NotResource" not in stmt, stmt
        for action in _as_list(stmt["Action"]):
            service = action.split(":", 1)[0]
            assert action != "*" and "*" not in service and "?" not in service, stmt
            if service.lower() == "iam":
                assert action.lower() == "iam:passrole", stmt


def test_boundary_iam_passrole_only_on_ecp_roles() -> None:
    doc = r2_policies.boundary()
    for stmt in _as_list(doc["Statement"]):
        actions = _as_list(stmt.get("Action", []))
        if stmt["Effect"] == "Allow" and any(a.lower().startswith("iam:") for a in actions):
            for resource in _as_list(stmt["Resource"]):
                assert resource.startswith(f"{IAM}:role/ecp-"), stmt


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        ("iam:CreateRole", ECP_ROLE),
        ("iam:AttachRolePolicy", ECP_ROLE),
        ("iam:PutRolePolicy", ECP_ROLE),
        ("iam:DeleteRolePermissionsBoundary", ECP_ROLE),
        ("iam:CreatePolicyVersion", BOUNDARY),
        ("iam:CreateUser", f"{IAM}:user/ecp-x"),
        ("iam:CreateAccessKey", f"{IAM}:user/admin"),
        ("iam:GetRole", ECP_ROLE),
        ("iam:ListRoles", "*"),
        ("iam:CreateServiceLinkedRole", f"{IAM}:role/aws-service-role/*"),
        ("iam:PassRole", OTHER_ROLE),
        ("iam:PassRole", f"{IAM}:role/OrganizationAccountAccessRole"),
        ("organizations:ListAccounts", "*"),
        ("organizations:LeaveOrganization", "*"),
        ("account:PutAlternateContact", "*"),
        ("sso:CreatePermissionSet", "*"),
        ("sso-directory:CreateUser", "*"),
        ("identitystore:CreateUser", "*"),
        ("sts:AssumeRole", r2_policies.DEPLOY_ROLE_ARN),
        ("sts:AssumeRole", f"{IAM}:role/OrganizationAccountAccessRole"),
        ("sts:AssumeRole", ECP_ROLE),
        ("secretsmanager:DeleteSecret", arn("secretsmanager", "secret:ecp/eia-AbCdEf")),
        ("secretsmanager:PutSecretValue", arn("secretsmanager", "secret:ecp/eia-AbCdEf")),
        ("ssm:PutParameter", arn("ssm", "parameter/ecp/x")),
        ("ec2:RunInstances", "*"),
        ("dynamodb:PutItem", arn("dynamodb", "table/ecp-x")),
        ("cloudformation:CreateStack", "*"),
    ],
)
def test_boundary_denies_unlisted(action: str, resource: str) -> None:
    assert bounded_admin(action, resource) != "allowed"


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        ("s3:GetObject", f"{STATE}/bootstrap/terraform.tfstate"),
        ("s3:GetObjectVersion", f"{STATE}/batch/terraform.tfstate"),
        ("s3:PutObject", f"{STATE}/batch/terraform.tfstate"),
        ("s3:DeleteObject", f"{STATE}/deep/path/x.tflock"),
        ("s3:ListBucket", STATE),
        ("s3:ListBucketVersions", STATE),
        ("s3:PutBucketPolicy", STATE),
        ("s3:DeleteBucketPolicy", STATE),
        ("s3:PutLifecycleConfiguration", STATE),
        ("s3:DeleteBucket", STATE),
    ],
)
def test_boundary_explicitly_denies_state_bucket(action: str, resource: str) -> None:
    assert bounded_admin(action, resource) == "explicitDeny"


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        ("s3:GetObject", "arn:aws:s3:::ecp-curves-123456789012/silver/x.parquet"),
        ("s3:PutObject", "arn:aws:s3:::ecp-curves-123456789012/gold/x.parquet"),
        ("s3:ListBucket", "arn:aws:s3:::ecp-curves-123456789012"),
        ("lambda:InvokeFunction", arn("lambda", "function:ecp-ingest")),
        ("ecs:RunTask", arn("ecs", "task-definition/ecp-engine:1")),
        ("ecr:GetAuthorizationToken", "*"),
        ("ecr:BatchGetImage", arn("ecr", "repository/ecp-engine")),
        ("ecr:GetDownloadUrlForLayer", arn("ecr", "repository/ecp-engine")),
        ("states:StartExecution", arn("states", "stateMachine:ecp-pipeline")),
        ("events:PutEvents", arn("events", "event-bus/default")),
        ("scheduler:GetSchedule", arn("scheduler", "schedule/default/ecp-daily")),
        ("cloudwatch:PutMetricData", "*"),
        ("logs:CreateLogStream", arn("logs", "log-group:/aws/lambda/ecp-ingest:*")),
        ("logs:PutLogEvents", arn("logs", "log-group:/aws/lambda/ecp-ingest:*")),
        ("sns:Publish", arn("sns", "ecp-alerts")),
        ("glue:StartJobRun", arn("glue", "job/ecp-silver")),
        ("glue:GetTable", arn("glue", "table/ecp/curves")),
        ("redshift-serverless:GetCredentials", arn("redshift-serverless", "workgroup/1a2b3c")),
        ("redshift-data:ExecuteStatement", "*"),
        ("secretsmanager:GetSecretValue", arn("secretsmanager", "secret:ecp/eia-AbCdEf")),
        ("ssm:GetParameter", arn("ssm", "parameter/ecp/x")),
        ("ec2:CreateNetworkInterface", "*"),
        ("ec2:DescribeNetworkInterfaces", "*"),
        ("ec2:DeleteNetworkInterface", "*"),
        ("iam:PassRole", ECP_ROLE),
    ],
)
def test_boundary_allows_workload_services(action: str, resource: str) -> None:
    assert bounded_admin(action, resource) == "allowed"


def test_boundary_limits_rather_than_grants() -> None:
    # A boundary alone grants nothing: with no identity policy every workload call is denied.
    action, resource = "sns:Publish", f"arn:aws:sns:{REGION}:{ACCOUNT}:ecp-alerts"
    assert iam_eval.decide([], action, resource, None, r2_policies.boundary()) != "allowed"


# --- the evaluator the claims above rest on (AWS policy-evaluation semantics) ---


def _doc(*stmts: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": list(stmts)}


def test_eval_explicit_deny_beats_allow() -> None:
    ident = [
        _doc({"Effect": "Allow", "Action": "s3:*", "Resource": "*"}),
        _doc({"Effect": "Deny", "Action": "s3:GetObject", "Resource": "*"}),
    ]
    assert iam_eval.decide(ident, "s3:GetObject", "arn:aws:s3:::b/k") == "explicitDeny"
    assert iam_eval.decide(ident, "s3:PutObject", "arn:aws:s3:::b/k") == "allowed"


def test_eval_no_match_is_implicit_deny() -> None:
    ident = [_doc({"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"})]
    assert iam_eval.decide(ident, "s3:PutObject", "arn:aws:s3:::b/k") == "implicitDeny"


def test_eval_action_match_is_case_insensitive() -> None:
    ident = [_doc({"Effect": "Allow", "Action": "S3:getobject", "Resource": "*"})]
    assert iam_eval.decide(ident, "s3:GetObject", "arn:aws:s3:::b/k") == "allowed"


def test_eval_not_action_and_not_resource() -> None:
    ident = [
        _doc({"Effect": "Allow", "NotAction": ["iam:*", "sso:*"], "Resource": "*"}),
        _doc({"Effect": "Deny", "Action": "s3:*", "NotResource": "arn:aws:s3:::ok/*"}),
    ]
    assert iam_eval.decide(ident, "iam:CreateRole", "*") == "implicitDeny"
    assert iam_eval.decide(ident, "s3:GetObject", "arn:aws:s3:::ok/k") == "allowed"
    assert iam_eval.decide(ident, "s3:GetObject", "arn:aws:s3:::other/k") == "explicitDeny"


def test_eval_boundary_intersects() -> None:
    bnd = _doc({"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"})
    assert iam_eval.decide([ADMIN_DOC], "s3:GetObject", "*", None, bnd) == "allowed"
    assert iam_eval.decide([ADMIN_DOC], "s3:PutObject", "*", None, bnd) == "implicitDeny"


def test_eval_not_equals_matches_missing_key() -> None:
    deny = {
        "Effect": "Deny",
        "Action": "iam:CreateRole",
        "Resource": "*",
        "Condition": {"ArnNotEquals": {PB: BOUNDARY}},
    }
    ident = [ADMIN_DOC, _doc(deny)]
    assert iam_eval.decide(ident, "iam:CreateRole", ECP_ROLE) == "explicitDeny"
    assert iam_eval.decide(ident, "iam:CreateRole", ECP_ROLE, {}) == "explicitDeny"
    assert iam_eval.decide(ident, "iam:CreateRole", ECP_ROLE, {PB: BOUNDARY}) == "allowed"


def test_eval_equals_does_not_match_missing_key() -> None:
    deny = {
        "Effect": "Deny",
        "Action": "iam:AttachRolePolicy",
        "Resource": "*",
        "Condition": {"ArnEquals": {POLICY_ARN: ADMIN}},
    }
    ident = [ADMIN_DOC, _doc(deny)]
    assert iam_eval.decide(ident, "iam:AttachRolePolicy", ECP_ROLE) == "allowed"
    ctx = {POLICY_ARN: ADMIN}
    assert iam_eval.decide(ident, "iam:AttachRolePolicy", ECP_ROLE, ctx) == "explicitDeny"


def test_eval_condition_key_name_is_case_insensitive() -> None:
    deny = {
        "Effect": "Deny",
        "Action": "iam:CreateRole",
        "Resource": "*",
        "Condition": {"ArnNotEquals": {"IAM:permissionsboundary": BOUNDARY}},
    }
    ident = [ADMIN_DOC, _doc(deny)]
    assert iam_eval.decide(ident, "iam:CreateRole", ECP_ROLE, {PB: BOUNDARY}) == "allowed"


def test_eval_arn_equals_is_case_sensitive() -> None:
    deny = {
        "Effect": "Deny",
        "Action": "iam:CreateRole",
        "Resource": "*",
        "Condition": {"ArnNotEquals": {PB: BOUNDARY}},
    }
    ident = [ADMIN_DOC, _doc(deny)]
    ctx = {PB: BOUNDARY.upper()}
    assert iam_eval.decide(ident, "iam:CreateRole", ECP_ROLE, ctx) == "explicitDeny"


def test_eval_resource_wildcards() -> None:
    ident = [_doc({"Effect": "Allow", "Action": "iam:PassRole", "Resource": f"{IAM}:role/ecp-*"})]
    assert iam_eval.decide(ident, "iam:PassRole", ECP_ROLE) == "allowed"
    assert iam_eval.decide(ident, "iam:PassRole", OTHER_ROLE) == "implicitDeny"
    assert iam_eval.decide(ident, "iam:PassRole", f"{IAM}:role/ECP-x") == "implicitDeny"


@pytest.mark.parametrize(
    "condition",
    [
        {"Bool": {"aws:SecureTransport": "true"}},
        {"IpAddress": {"aws:SourceIp": "10.0.0.0/8"}},
        {"NumericLessThan": {"s3:max-keys": "10"}},
        {"StringEqualsIgnoreCase": {"aws:PrincipalTag/team": "ecp"}},
        {"StringEqualsIfExists": {PB: BOUNDARY}},
        {"ForAnyValue:StringEquals": {"aws:TagKeys": "x"}},
        {"Null": {PB: "true"}},
    ],
)
def test_eval_unsupported_condition_raises(condition: dict) -> None:
    deny = {"Effect": "Deny", "Action": "*", "Resource": "*", "Condition": condition}
    ident = [_doc(deny), ADMIN_DOC]
    with pytest.raises(ValueError):
        iam_eval.decide(ident, "s3:GetObject", "*", {PB: BOUNDARY})
