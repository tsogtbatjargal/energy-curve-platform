"""Read-only: classify every IAM role's trust policy (ADR-0018 risk review, session start).

    AWS_PROFILE=ecp-admin uv run python scripts/role_trust_review.py

PowerUserAccess allows sts:AssumeRole, so a role trusting `*`, this account's root or another
principal in this account would let the deploy role step outside its own limits. Lines starting
REVIEW need a look. Account IDs print as <this>/<other>; role names print as they are, so keep
this output out of public places unless it is sanitized.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import boto3


def principals(statement: dict[str, Any]) -> list[tuple[str, str]]:
    pr = statement.get("Principal", {})
    if pr == "*":
        return [("*", "*")]
    out = []
    for kind, vals in pr.items():
        out += [(kind, v) for v in (vals if isinstance(vals, list) else [vals])]
    return out


def flags(doc: dict[str, Any], account: str) -> list[str]:
    out = []
    for st in doc.get("Statement", []):
        if st.get("Effect") != "Allow":
            continue
        conditioned = "" if st.get("Condition") else "-NO-CONDITION"
        for kind, v in principals(st):
            if kind == "*" or (kind == "AWS" and v == "*"):
                out.append(f"PRINCIPAL-STAR{conditioned}")
            elif kind == "AWS" and v in (account, f"arn:aws:iam::{account}:root"):
                out.append("THIS-ACCOUNT-ROOT")
            elif kind == "AWS" and account in v:
                out.append(f"THIS-ACCOUNT:{v.split(':')[-1]}")
            elif kind == "AWS":
                out.append("OTHER-ACCOUNT")
            elif kind == "Federated":
                out.append(f"federated:{v.split('/')[-1]}{conditioned}")
            else:
                out.append(f"{kind.lower()}:{v}")
    return sorted(set(out))


def main() -> int:
    session = boto3.Session()
    account = session.client("sts").get_caller_identity()["Account"]
    iam = session.client("iam")
    rows = []
    for page in iam.get_paginator("list_roles").paginate():
        for role in page["Roles"]:
            rows.append(
                (role["Path"], role["RoleName"], flags(role["AssumeRolePolicyDocument"], account))
            )
    review = 0
    print(f"{len(rows)} roles")
    for path, name, fl in sorted(rows, key=lambda r: (r[0] != "/", r[1])):
        risky = any(
            f.startswith(("PRINCIPAL", "THIS", "OTHER")) or f.endswith("NO-CONDITION") for f in fl
        )
        review += risky
        text = re.sub(
            r"\d{12}", lambda m: "<this>" if m.group() == account else "<other>", ", ".join(fl)
        )
        print(f"{'REVIEW' if risky else 'ok    '} {path}{name}: {text or '(no allow statements)'}")
    print(f"{review} roles to review")
    return 1 if review else 0


if __name__ == "__main__":
    sys.exit(main())
