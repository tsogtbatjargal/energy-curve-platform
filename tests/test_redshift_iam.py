"""PLAN.md R2 for the Redshift stack (ADR-0018, ADR-0023): each role can do its job, and no more.

The templates in infra/redshift/policies are rendered with synthetic values and evaluated with the
real workload boundary, as tests/test_glue_iam.py does for Glue. The COPY role is the one Redshift
assumes to read S3; the Data API role is what the operator assumes to run statements.
"""

import json
import re
from pathlib import Path

import hcl2
import policy_templates
import pytest
import r2_policies as r2
from hcl2.utils import SerializationOptions
from iam_eval import ALLOWED, EXPLICIT_DENY, as_list, decide, matches

ACCOUNT, REGION = r2.ACCOUNT, "ca-central-1"
STACK = Path(__file__).parents[1] / "infra" / "redshift"
POLICIES = STACK / "policies"
VALUES = {"account_id": ACCOUNT, "region": REGION}
GLUE = f"arn:aws:s3:::ecp-glue-{ACCOUNT}-{REGION}"
DATA = f"arn:aws:s3:::ecp-data-{ACCOUNT}-{REGION}"
WORKGROUP = (
    f"arn:aws:redshift-serverless:{REGION}:{ACCOUNT}:workgroup/1a2b3c4d-0000-1111-2222-333344445555"
)
TAGGED = {"aws:ResourceTag/stack": "redshift"}
SHAPE_KEY = "m5/shape/jr_" + "a" * 64 + "/shape_params.csv/part-00000"
GOLD = (
    "store/runs/in-1/attempt-1/gold/current.parquet",
    "store/runs/in-1/attempt-1/gold/revisions.parquet",
)
STATE_BUCKET = r2.VALUES["state_bucket_arn"]


def policy(name: str) -> dict:
    return policy_templates.render(name, VALUES, POLICIES)


def as_copy(action: str, resource: str, context: dict | None = None) -> str:
    return decide([policy("copy-policy")], action, resource, context, boundary=r2.boundary())


def as_caller(action: str, resource: str, context: dict | None = None) -> str:
    return decide([policy("data-api-policy")], action, resource, context, boundary=r2.boundary())


# --- the templates are the Terraform inputs ------------------------------------------------------


def test_the_templates_are_exactly_two_trusts_and_two_policies() -> None:
    names = {p.name.removesuffix(".json.tftpl") for p in POLICIES.glob("*.json.tftpl")}
    assert names == {"copy-trust", "copy-policy", "data-api-trust", "data-api-policy"}


def test_terraform_passes_exactly_the_placeholders_the_templates_use() -> None:
    with (STACK / "locals.tf").open() as fh:
        doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
    values = next(b["policy_values"] for b in doc["locals"] if "policy_values" in b)
    passed = {k.strip('"') for k in values}
    used = set().union(
        *(
            policy_templates.placeholders(p.name.removesuffix(".json.tftpl"), POLICIES)
            for p in POLICIES.glob("*.json.tftpl")
        )
    )
    assert passed == used == {"account_id", "region"}


def test_the_roles_terraform_creates_are_the_tested_roles() -> None:
    assert re.search(r'copy_role\s*=\s*"ecp-redshift-copy"', (STACK / "locals.tf").read_text())
    assert re.search(
        r'caller_role\s*=\s*"ecp-redshift-data-api"', (STACK / "locals.tf").read_text()
    )
    text = (STACK / "iam.tf").read_text()
    for name in ("copy-trust", "copy-policy", "data-api-trust", "data-api-policy"):
        assert f"policies/{name}.json.tftpl" in text, name
    assert text.count("permissions_boundary = local.boundary_arn") == 2


# These three Data API actions have no resource type: IAM accepts only "*" for them.
NO_RESOURCE_TYPE = {
    "redshift-data:DescribeStatement", "redshift-data:GetStatementResult",
    "redshift-data:ListStatements",
}  # fmt: skip


@pytest.mark.parametrize("name", ["copy-policy", "data-api-policy"])
def test_no_statement_allows_everything_on_everything(name: str) -> None:
    for stmt in policy(name)["Statement"]:
        assert stmt["Effect"] == "Allow"
        assert not any(a == "*" or a.endswith(":*") for a in as_list(stmt["Action"]))
        if "*" in as_list(stmt["Resource"]):
            assert set(as_list(stmt["Action"])) <= NO_RESOURCE_TYPE, stmt["Sid"]


# --- the COPY role: read exactly the shape file and the two gold files ---------------------------


COPY_ALLOWED = [
    ("s3:GetObject", f"{GLUE}/{SHAPE_KEY}", None),
    ("s3:GetObject", f"{DATA}/{GOLD[0]}", None),
    ("s3:GetObject", f"{DATA}/{GOLD[1]}", None),
    ("s3:ListBucket", GLUE, {"s3:prefix": SHAPE_KEY}),
    ("s3:ListBucket", DATA, {"s3:prefix": GOLD[0]}),
    ("s3:ListBucket", DATA, {"s3:prefix": GOLD[1]}),
]


@pytest.mark.parametrize(("action", "resource", "context"), COPY_ALLOWED)
def test_the_copy_role_can_do_its_job_inside_the_boundary(action, resource, context) -> None:  # type: ignore[no-untyped-def]
    assert as_copy(action, resource, context) == ALLOWED


COPY_DENIED = [
    # the other Glue outputs and the legacy layout
    ("s3:GetObject", f"{GLUE}/m5/shape/jr_{'a' * 64}/shape_params.sha256/part-00000", None),
    ("s3:GetObject", f"{GLUE}/m5/shape/jr_{'a' * 64}/_complete.json", None),
    ("s3:GetObject", f"{GLUE}/m5/shape/shape_params.csv/part-00000", None),
    ("s3:GetObject", f"{GLUE}/glue/shape_job.py", None),
    # the rest of the batch store: pointer, manifests, other artifacts, other gold names
    ("s3:GetObject", f"{DATA}/store/published/current.json", None),
    ("s3:GetObject", f"{DATA}/store/runs/in-1/attempt-1/manifest.json", None),
    ("s3:GetObject", f"{DATA}/store/runs/in-1/attempt-1/silver/x.parquet", None),
    ("s3:GetObject", f"{DATA}/store/runs/in-1/attempt-1/gold/other.parquet", None),
    ("s3:GetObject", f"{DATA}/store/runs/in-1/attempt-1/gold/current.parquet.bak", None),
    # no write, no delete, no wider read
    ("s3:PutObject", f"{GLUE}/{SHAPE_KEY}", None),
    ("s3:PutObject", f"{DATA}/{GOLD[0]}", None),
    ("s3:DeleteObject", f"{DATA}/{GOLD[0]}", None),
    ("s3:PutObject", f"{GLUE}/m5/x", None),
    ("s3:ListBucketVersions", GLUE, None),
    ("s3:GetObjectVersion", f"{DATA}/{GOLD[0]}", None),
    # listing needs the prefix condition, and only the reviewed prefixes
    ("s3:ListBucket", GLUE, None),
    ("s3:ListBucket", DATA, None),
    ("s3:ListBucket", GLUE, {"s3:prefix": ""}),
    ("s3:ListBucket", GLUE, {"s3:prefix": "m5/"}),
    ("s3:ListBucket", GLUE, {"s3:prefix": "m5/shape/"}),
    ("s3:ListBucket", DATA, {"s3:prefix": "store/"}),
    ("s3:ListBucket", DATA, {"s3:prefix": "store/published/current.json"}),
    ("s3:ListBucket", f"arn:aws:s3:::ecp-tfstate-{ACCOUNT}-{REGION}", {"s3:prefix": SHAPE_KEY}),
    # nothing else
    ("kms:Decrypt", f"arn:aws:kms:{REGION}:{ACCOUNT}:key/x", None),
    ("sts:AssumeRole", f"arn:aws:iam::{ACCOUNT}:role/ecp-redshift-data-api", None),
    ("iam:PassRole", f"arn:aws:iam::{ACCOUNT}:role/ecp-redshift-copy", None),
    ("glue:StartJobRun", f"arn:aws:glue:{REGION}:{ACCOUNT}:job/ecp-glue-shape", None),
    ("redshift-serverless:GetCredentials", WORKGROUP, TAGGED),
    ("logs:PutLogEvents", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/x:log-stream:s", None),
]


@pytest.mark.parametrize(("action", "resource", "context"), COPY_DENIED)
def test_the_copy_role_cannot_do_more(action, resource, context) -> None:  # type: ignore[no-untyped-def]
    assert as_copy(action, resource, context) != ALLOWED


def test_the_copy_role_cannot_read_the_terraform_state_even_with_a_wide_policy() -> None:
    wide = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}],
    }
    verdict = decide(
        [policy("copy-policy"), wide], "s3:GetObject", f"{STATE_BUCKET}/redshift/x",
        boundary=r2.boundary(),
    )  # fmt: skip
    assert verdict == EXPLICIT_DENY


def test_the_copy_role_is_trusted_by_the_two_redshift_services_only() -> None:
    (stmt,) = policy("copy-trust")["Statement"]
    assert stmt["Effect"] == "Allow" and stmt["Action"] == "sts:AssumeRole"
    assert sorted(as_list(stmt["Principal"]["Service"])) == [
        "redshift-serverless.amazonaws.com",
        "redshift.amazonaws.com",
    ]
    assert set(stmt["Principal"]) == {"Service"} and "Condition" not in stmt


# --- the Data API role: run statements on this stack's workgroup, read its own results -----------


CALLER_ALLOWED = [
    ("redshift-data:ExecuteStatement", WORKGROUP, TAGGED),
    ("redshift-data:BatchExecuteStatement", WORKGROUP, TAGGED),
    ("redshift-serverless:GetCredentials", WORKGROUP, TAGGED),
    ("redshift-data:DescribeStatement", "*", None),
    ("redshift-data:GetStatementResult", "*", None),
    ("redshift-data:ListStatements", "*", None),
]


@pytest.mark.parametrize(("action", "resource", "context"), CALLER_ALLOWED)
def test_the_caller_can_do_its_job_inside_the_boundary(action, resource, context) -> None:  # type: ignore[no-untyped-def]
    assert as_caller(action, resource, context) == ALLOWED


CALLER_DENIED = [
    # a workgroup without this stack's tag, or another stack's
    ("redshift-data:ExecuteStatement", WORKGROUP, None),
    ("redshift-data:ExecuteStatement", WORKGROUP, {"aws:ResourceTag/stack": "demo"}),
    ("redshift-serverless:GetCredentials", WORKGROUP, None),
    ("redshift-serverless:GetCredentials", WORKGROUP, {"aws:ResourceTag/stack": "demo"}),
    # a provisioned cluster, another account's workgroup, another Region's
    ("redshift-data:ExecuteStatement", f"arn:aws:redshift:{REGION}:{ACCOUNT}:cluster:c", TAGGED),
    ("redshift-data:ExecuteStatement", WORKGROUP.replace(ACCOUNT, "999999999999"), TAGGED),
    ("redshift-data:ExecuteStatement", WORKGROUP.replace(REGION, "us-east-1"), TAGGED),
    ("redshift:GetClusterCredentials", f"arn:aws:redshift:{REGION}:{ACCOUNT}:dbuser:c/u", TAGGED),
    # no administration of the warehouse, no cancel, no other data
    ("redshift-serverless:CreateWorkgroup", WORKGROUP, TAGGED),
    ("redshift-serverless:UpdateWorkgroup", WORKGROUP, TAGGED),
    (
        "redshift-serverless:DeleteNamespace",
        f"arn:aws:redshift-serverless:{REGION}:{ACCOUNT}:namespace/x",
        TAGGED,
    ),
    ("redshift-serverless:CreateUsageLimit", WORKGROUP, TAGGED),
    ("redshift-data:CancelStatement", "*", None),
    ("s3:GetObject", f"{GLUE}/{SHAPE_KEY}", None),
    ("s3:PutObject", f"{GLUE}/m5/x", None),
    ("iam:PassRole", f"arn:aws:iam::{ACCOUNT}:role/ecp-redshift-copy", None),
    ("sts:AssumeRole", f"arn:aws:iam::{ACCOUNT}:role/ecp-redshift-copy", None),
    ("secretsmanager:GetSecretValue", f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:x", None),
]


@pytest.mark.parametrize(("action", "resource", "context"), CALLER_DENIED)
def test_the_caller_cannot_do_more(action, resource, context) -> None:  # type: ignore[no-untyped-def]
    assert as_caller(action, resource, context) != ALLOWED


# The proposed trust (ADR-0023): the member account's own AdministratorAccess Identity Center
# sessions, the same pattern as the break-glass role's trust, without the management account.
ADMIN_SESSION = (
    f"arn:aws:iam::{ACCOUNT}:role/aws-reserved/sso.amazonaws.com/ca-central-1/"
    "AWSReservedSSO_AdministratorAccess_0123456789abcdef"
)


def test_the_caller_trusts_only_this_accounts_administrator_sessions() -> None:
    (stmt,) = policy("data-api-trust")["Statement"]
    assert stmt["Effect"] == "Allow" and stmt["Action"] == "sts:AssumeRole"
    assert stmt["Principal"] == {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}
    assert set(stmt["Condition"]) == {"ArnLike"}
    (pattern,) = as_list(stmt["Condition"]["ArnLike"]["aws:PrincipalArn"])
    assert matches(pattern, ADMIN_SESSION)
    for other in (
        ADMIN_SESSION.replace("AdministratorAccess", "ecp-readonly"),
        ADMIN_SESSION.replace(ACCOUNT, "999999999999"),
        f"arn:aws:iam::{ACCOUNT}:role/ecp-redshift-copy",
        f"arn:aws:iam::{ACCOUNT}:user/someone",
        "*",
    ):
        assert not matches(pattern, other), other


def test_the_reviewed_documents_are_valid_json_as_terraform_renders_them() -> None:
    for p in POLICIES.glob("*.json.tftpl"):
        rendered = policy(p.name.removesuffix(".json.tftpl"))
        assert json.loads(json.dumps(rendered))["Version"] == "2012-10-17"
