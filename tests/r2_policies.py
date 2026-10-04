"""The PLAN.md R2 policies as Terraform renders them, with synthetic account values (ADR-0018).

Terraform renders `infra/bootstrap/policies/*.json.tftpl` with templatefile(). These templates
use only `${name}` placeholders, so this renders the very same files and fails on anything else
(`%{` directives, unknown names), rather than diverge from Terraform.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
POLICIES = ROOT / "infra" / "bootstrap" / "policies"
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
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


def template(name: str) -> str:
    return (POLICIES / f"{name}.json.tftpl").read_text()


def placeholders(name: str) -> set[str]:
    return set(PLACEHOLDER.findall(template(name)))


def render(name: str, values: dict[str, str] = VALUES) -> dict[str, Any]:
    text = template(name)
    if "%{" in text:
        raise ValueError(f"{name}: template directives are not supported here")

    def value(m: re.Match[str]) -> str:
        if m.group(1) not in values:
            raise ValueError(f"{name}: unknown placeholder {m.group(0)}")
        return values[m.group(1)]

    return json.loads(PLACEHOLDER.sub(value, text))


def power_user() -> dict[str, Any]:
    """AWS's PowerUserAccess, v12, copied from the AWS Managed Policy Reference."""
    return json.loads((FIXTURES / "PowerUserAccess-v12.json").read_text())


def deploy_identity() -> list[dict[str, Any]]:
    """ecp-gha-deploy's identity policies. Its terraform-state-read inline policy grants only S3
    reads on the state bucket, which PowerUserAccess already allows, so it is left out."""
    return [power_user(), render("deploy-iam")]


def boundary() -> dict[str, Any]:
    return render("workload-boundary")
