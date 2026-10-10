"""PLAN.md R2 for the Glue stack (ADR-0018, ADR-0023): the job role can do its job, and no more.

The role's inline policy is the real template (infra/glue/policies) rendered with synthetic values
and evaluated together with the real workload boundary, as tests/test_batch_iam.py does for batch.
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
STACK = Path(__file__).parents[1] / "infra" / "glue"
POLICIES = STACK / "policies"
VALUES = {"account_id": ACCOUNT, "region": REGION}
BUCKET = f"arn:aws:s3:::ecp-glue-{ACCOUNT}-{REGION}"
LOGS = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group"
STATE_BUCKET = r2.VALUES["state_bucket_arn"]


def policy(name: str) -> dict:
    return policy_templates.render(name, VALUES, POLICIES)


def as_role(action: str, resource: str, context: dict | None = None) -> str:
    return decide([policy("glue-policy")], action, resource, context, boundary=r2.boundary())


def test_the_templates_are_exactly_the_trust_and_the_policy() -> None:
    names = {p.name.removesuffix(".json.tftpl") for p in POLICIES.glob("*.json.tftpl")}
    assert names == {"glue-trust", "glue-policy"}


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


def test_the_role_terraform_creates_is_the_tested_role() -> None:
    assert re.search(r'name\s*=\s*"ecp-glue-shape"', (STACK / "locals.tf").read_text())
    text = (STACK / "iam.tf").read_text()
    assert text.count("name                 = local.name") == 1
    assert "policies/glue-trust.json.tftpl" in text and "policies/glue-policy.json.tftpl" in text


def test_no_statement_allows_everything_on_everything() -> None:
    for stmt in policy("glue-policy")["Statement"]:
        assert stmt["Effect"] == "Allow"
        assert "*" not in as_list(stmt["Action"]) and not any(
            a.endswith(":*") for a in as_list(stmt["Action"])
        )
        assert "*" not in as_list(stmt["Resource"])


def test_the_role_trusts_the_glue_service_only() -> None:
    (stmt,) = policy("glue-trust")["Statement"]
    assert stmt == {
        "Effect": "Allow",
        "Principal": {"Service": "glue.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }  # no confused-deputy condition: none is documented for Glue, and a wrong one breaks the run


NEEDED = [
    ("s3:GetObject", f"{BUCKET}/glue/shape_job.py"),
    ("s3:GetObject", f"{BUCKET}/glue/energy_curves_m5.zip"),
    ("s3:PutObject", f"{BUCKET}/m5/shape/shape_params.csv/part-00000"),
    ("s3:PutObject", f"{BUCKET}/m5/shape/shape_params.csv/_temporary/0/x"),
    ("s3:GetObject", f"{BUCKET}/m5/shape/shape_params.csv/_SUCCESS"),
    ("s3:DeleteObject", f"{BUCKET}/m5/shape/shape_params.csv/_temporary/0/x"),
    ("s3:AbortMultipartUpload", f"{BUCKET}/m5/shape/x"),
    ("s3:ListBucket", BUCKET),
    ("s3:ListBucketMultipartUploads", BUCKET),
    ("logs:CreateLogGroup", f"{LOGS}:/aws-glue/ecp-shape"),
    ("logs:CreateLogStream", f"{LOGS}:/aws-glue/ecp-shape:log-stream:jr_x-driver"),
    ("logs:PutLogEvents", f"{LOGS}:/aws-glue/ecp-shape:log-stream:jr_x-driver"),
    ("logs:PutLogEvents", f"{LOGS}:/aws-glue/jobs/output:log-stream:jr_x"),
]


@pytest.mark.parametrize(("action", "resource"), NEEDED)
def test_the_role_can_do_its_job_inside_the_boundary(action: str, resource: str) -> None:
    assert as_role(action, resource) == ALLOWED


NOT_ALLOWED = [
    ("s3:PutObject", f"{BUCKET}/glue/shape_job.py"),  # the job cannot rewrite its own code
    ("s3:DeleteObject", f"{BUCKET}/glue/shape_job.py"),
    ("s3:GetObject", f"{BUCKET}/elsewhere/x"),
    ("s3:PutObject", f"{BUCKET}/elsewhere/x"),
    ("s3:GetObject", f"arn:aws:s3:::ecp-data-{ACCOUNT}-{REGION}/store/pointer.json"),
    ("s3:PutObject", f"arn:aws:s3:::ecp-data-{ACCOUNT}-{REGION}/store/pointer.json"),
    ("s3:ListBucket", f"arn:aws:s3:::ecp-data-{ACCOUNT}-{REGION}"),
    ("s3:PutBucketPolicy", BUCKET),
    ("logs:CreateLogGroup", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/ecs/other"),
    ("logs:PutLogEvents", f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/x:log-stream:s"),
    ("glue:StartJobRun", f"arn:aws:glue:{REGION}:{ACCOUNT}:job/ecp-glue-shape"),
    ("glue:GetTable", f"arn:aws:glue:{REGION}:{ACCOUNT}:table/db/t"),
    ("iam:PassRole", f"arn:aws:iam::{ACCOUNT}:role/ecp-glue-shape"),
    ("kms:Decrypt", f"arn:aws:kms:{REGION}:{ACCOUNT}:key/x"),
    ("secretsmanager:GetSecretValue", f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:x"),
    (
        "redshift-data:ExecuteStatement",
        f"arn:aws:redshift-serverless:{REGION}:{ACCOUNT}:workgroup/w",
    ),
]


@pytest.mark.parametrize(("action", "resource"), NOT_ALLOWED)
def test_the_role_cannot_do_more(action: str, resource: str) -> None:
    assert as_role(action, resource) != ALLOWED


def test_the_role_cannot_touch_the_terraform_state() -> None:
    wide = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}],
    }
    for action in ("s3:GetObject", "s3:PutObject"):
        verdict = decide(
            [policy("glue-policy"), wide],
            action,
            f"{STATE_BUCKET}/glue/x",
            boundary=r2.boundary(),
        )
        assert verdict == EXPLICIT_DENY


def test_the_reviewed_documents_are_valid_json_as_terraform_renders_them() -> None:
    for p in POLICIES.glob("*.json.tftpl"):
        rendered = policy(p.name.removesuffix(".json.tftpl"))
        assert json.loads(json.dumps(rendered))["Version"] == "2012-10-17"
