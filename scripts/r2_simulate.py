"""Read-only evidence for R2 (ADR-0018): run the offline policy scenarios through AWS's own
evaluator, `iam:SimulateCustomPolicy`, and compare with tests/iam_eval.py.

    AWS_PROFILE=ecp-admin uv run python scripts/r2_simulate.py

The policies are the real templates rendered with synthetic values (tests/r2_policies.py), so
nothing account-specific is sent or printed. Also checks that the PowerUserAccess copy the tests
use equals AWS's live default version. Exit 1 on any difference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import boto3

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import r2_policies as r2  # noqa: E402
from iam_eval import decide  # noqa: E402
from test_r2_policies import (  # noqa: E402
    BOUNDED,
    BOUNDED_CASES,
    DEPLOY_CASES,
    EVERYTHING,
    READONLY,
    ROLE,
)

DESTROY_CASES = [
    ("iam:DetachRolePolicy", ROLE, {**BOUNDED, "iam:PolicyARN": READONLY}, "allowed"),
    ("iam:DeleteRolePolicy", ROLE, BOUNDED, "allowed"),
    ("iam:DeleteRole", ROLE, BOUNDED, "allowed"),
]


def simulate(iam: Any, identity: list, action: str, resource: str, context: dict,
             boundary: dict | None = None) -> tuple[str, list[str]]:  # fmt: skip
    kw: dict[str, Any] = {
        "PolicyInputList": [json.dumps(p) for p in identity],
        "ActionNames": [action],
        "ContextEntries": [
            {"ContextKeyName": k, "ContextKeyValues": [v], "ContextKeyType": "string"}
            for k, v in context.items()
        ],
    }
    if resource != "*":
        kw["ResourceArns"] = [resource]
    if boundary is not None:
        kw["PermissionsBoundaryPolicyInputList"] = [json.dumps(boundary)]
    result = iam.simulate_custom_policy(**kw)["EvaluationResults"][0]
    return result["EvalDecision"], result.get("MissingContextValues", [])


def main() -> int:
    iam = boto3.Session().client("iam")
    pu = iam.get_policy(PolicyArn="arn:aws:iam::aws:policy/PowerUserAccess")["Policy"]
    live = iam.get_policy_version(PolicyArn=pu["Arn"], VersionId=pu["DefaultVersionId"])
    same = live["PolicyVersion"]["Document"] == r2.power_user()
    print(f"PowerUserAccess live default {pu['DefaultVersionId']}; fixture identical: {same}")
    cases = [("deploy", *c) for c in [*DEPLOY_CASES, *DESTROY_CASES]]
    cases += [("bounded", a, res, {}, exp) for a, res, exp in BOUNDED_CASES]
    differences = 0 if same else 1
    for who, action, resource, ctx, expected in cases:
        identity, boundary = (
            (r2.deploy_identity(), None) if who == "deploy" else (EVERYTHING, r2.boundary())
        )
        got, missing = simulate(iam, identity, action, resource, ctx, boundary)
        local = decide(identity, action, resource, ctx, boundary)
        ok = got == expected == local
        differences += not ok
        keys = ",".join(sorted(ctx)) or "-"
        note = f" missing={missing}" if missing else ""
        print(f"{'OK  ' if ok else 'DIFF'} {who:7} {action:34} ctx[{keys}] aws={got} "
              f"expected={expected} local={local}{note}")  # fmt: skip
    print(f"{len(cases)} cases, {differences} differences")
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
