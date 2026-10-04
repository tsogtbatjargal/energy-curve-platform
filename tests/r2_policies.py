"""The PLAN.md R2 policies as Terraform renders them, with synthetic account values (ADR-0018).

Rendering is scripts/policy_templates.py, shared with scripts/bootstrap_plan_check.py, so the
tests, the AWS simulation and the pre-apply plan check all read the same template files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import policy_templates

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "aws_managed"

ACCOUNT = "123456789012"
STATE_BUCKET = f"ecp-tfstate-{ACCOUNT}-ca-central-1"
BOUNDARY_ARN = f"arn:aws:iam::{ACCOUNT}:policy/ecp-workload-boundary"
DEPLOY_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/ecp-gha-deploy"
PLAN_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/ecp-gha-plan"
ADMIN = "arn:aws:iam::aws:policy/AdministratorAccess"

VALUES = {
    "account_id": ACCOUNT,
    "boundary_arn": BOUNDARY_ARN,
    "deploy_role_arn": DEPLOY_ROLE_ARN,
    "plan_role_arn": PLAN_ROLE_ARN,
    "state_bucket_arn": f"arn:aws:s3:::{STATE_BUCKET}",
}


def placeholders(name: str) -> set[str]:
    return policy_templates.placeholders(name)


def render(name: str, values: dict[str, str] = VALUES) -> dict[str, Any]:
    return policy_templates.render(name, values)


def power_user() -> dict[str, Any]:
    """AWS's PowerUserAccess, v12, copied from the AWS Managed Policy Reference."""
    return json.loads((FIXTURES / "PowerUserAccess-v12.json").read_text())


def deploy_identity() -> list[dict[str, Any]]:
    """ecp-gha-deploy's identity policies. Its terraform-state-read inline policy grants only S3
    reads on the state bucket, which PowerUserAccess already allows, so it is left out."""
    return [power_user(), render("deploy-iam")]


def boundary() -> dict[str, Any]:
    return render("workload-boundary")
