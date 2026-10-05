"""Read-only PLAN.md R2 acceptance on the deployed deploy role (ADR-0018):
`iam:SimulatePrincipalPolicy` on ecp-gha-deploy, with every policy actually attached to it.

    AWS_PROFILE=ecp-admin uv run python scripts/r2_accept.py

Prints decisions and the boundary policy's name, path and version; no account ID. Exit 1 on
any difference.
"""

from __future__ import annotations

import sys

import boto3

ADMIN = "arn:aws:iam::aws:policy/AdministratorAccess"
READONLY = "arn:aws:iam::aws:policy/ReadOnlyAccess"


def cases(account: str) -> list[tuple[str, str, str, dict[str, str], str]]:
    """(label, action, resource, context, expected)."""
    role = f"arn:aws:iam::{account}:role/ecp-gha-deploy"
    boundary = f"arn:aws:iam::{account}:policy/ecp-workload-boundary"
    task = f"arn:aws:iam::{account}:role/ecp-batch-task"
    bounded = {"iam:PermissionsBoundary": boundary}
    return [
        ("PLAN R2: create a role without the boundary", "iam:CreateRole", task, {}, "explicitDeny"),
        ("PLAN R2: attach AdministratorAccess (bounded role)", "iam:AttachRolePolicy", task,
         {**bounded, "iam:PolicyARN": ADMIN}, "explicitDeny"),
        ("PLAN R2: create a role with the boundary", "iam:CreateRole", task, bounded, "allowed"),
        ("create a role with another boundary", "iam:CreateRole", task,
         {"iam:PermissionsBoundary": READONLY}, "explicitDeny"),
        ("attach ReadOnlyAccess to a bounded role", "iam:AttachRolePolicy", task,
         {**bounded, "iam:PolicyARN": READONLY}, "allowed"),
        ("remove a role's boundary", "iam:DeleteRolePermissionsBoundary", task, bounded,
         "explicitDeny"),
        ("new version of the boundary policy", "iam:CreatePolicyVersion", boundary, {},
         "explicitDeny"),
        ("delete the boundary policy", "iam:DeletePolicy", boundary, {}, "explicitDeny"),
        ("read the boundary policy", "iam:GetPolicy", boundary, {}, "allowed"),
        ("Identity Center", "sso:CreatePermissionSet", "*", {}, "explicitDeny"),
        ("change its own inline policy", "iam:PutRolePolicy", role, bounded, "explicitDeny"),
        ("destroy a bounded role: detach", "iam:DetachRolePolicy", task,
         {**bounded, "iam:PolicyARN": READONLY}, "allowed"),
        ("destroy a bounded role: delete", "iam:DeleteRole", task, bounded, "allowed"),
        ("ADR-0021: assume another account's break-glass role", "sts:AssumeRole",
         "arn:aws:iam::210987654321:role/OrganizationAccountAccessRole", {}, "explicitDeny"),
        ("ADR-0021: assume a role in its own account", "sts:AssumeRole", task, {}, "allowed"),
    ]  # fmt: skip


def main() -> int:
    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    iam = session.client("iam")
    role = f"arn:aws:iam::{account}:role/ecp-gha-deploy"
    differences = 0
    for label, action, resource, context, expected in cases(account):
        kw = {
            "PolicySourceArn": role,
            "ActionNames": [action],
            "ContextEntries": [
                {"ContextKeyName": k, "ContextKeyValues": [v], "ContextKeyType": "string"}
                for k, v in context.items()
            ],
        }
        if resource != "*":
            kw["ResourceArns"] = [resource]
        got = iam.simulate_principal_policy(**kw)["EvaluationResults"][0]["EvalDecision"]
        differences += got != expected
        print(f"{'OK  ' if got == expected else 'DIFF'} {action:34} got={got:12} "
              f"expected={expected:12} {label}")  # fmt: skip
    print(f"{len(cases(account))} cases, {differences} differences")
    pol = iam.get_policy(PolicyArn=f"arn:aws:iam::{account}:policy/ecp-workload-boundary")
    p = pol["Policy"]
    uses = p.get("PermissionsBoundaryUsageCount")
    print(f"boundary policy: name={p['PolicyName']} path={p['Path']}"
          f" default={p['DefaultVersionId']} attachments={p['AttachmentCount']}"
          f" boundary_uses={uses}")  # fmt: skip
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
