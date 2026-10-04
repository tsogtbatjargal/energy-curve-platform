"""Pre-apply check of a saved bootstrap plan for the R2 apply (ADR-0018).

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/bootstrap_plan_check.py <plan.json>

Exit 0 only when the plan does exactly what was reviewed:
- resource changes: create `aws_iam_policy.workload_boundary`, and update
  `aws_iam_role_policy.gha_deploy_iam` with only `policy` changing. Nothing else: no other
  creates, updates, replacements or deletions;
- output changes: only the new `workload_boundary_arn`;
- no drift, no deferred changes, not an errored plan;
- both planned policy documents equal the reviewed templates rendered for this account, and are
  known at plan time.

Plans hold sensitive values in plain text, so the report prints addresses, actions, attribute
names and SHA-256 digests of the policy documents only, never values.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import policy_templates

UNCHANGED = (["no-op"], ["read"])
EXPECTED = {  # address -> (actions, attributes allowed to change; None = new resource)
    "aws_iam_policy.workload_boundary": (["create"], None),
    "aws_iam_role_policy.gha_deploy_iam": (["update"], {"policy"}),
}
EXPECTED_OUTPUTS = {"workload_boundary_arn": ["create"]}
TEMPLATES = {
    "aws_iam_policy.workload_boundary": "workload-boundary",
    "aws_iam_role_policy.gha_deploy_iam": "deploy-iam",
}


def changed_attributes(change: dict[str, Any]) -> set[str]:
    before, after = change.get("before") or {}, change.get("after") or {}
    unknown = {k for k, v in (change.get("after_unknown") or {}).items() if v}
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)} | unknown


def prior(plan: dict[str, Any], address: str) -> dict[str, Any] | None:
    resources = (plan.get("prior_state") or {}).get("values", {}).get("root_module", {})
    return next(
        (r["values"] for r in resources.get("resources", []) if r["address"] == address), None
    )


def template_values(plan: dict[str, Any]) -> dict[str, str]:
    """The values Terraform passes to the templates, read back from the plan's prior state."""
    identity = prior(plan, "data.aws_caller_identity.current")
    deploy, plan_role = prior(plan, "aws_iam_role.gha_deploy"), prior(plan, "aws_iam_role.gha_plan")
    bucket = prior(plan, "aws_s3_bucket.tfstate")
    if not (identity and deploy and plan_role and bucket):
        raise ValueError("prior state lacks the caller identity, CI roles or state bucket")
    account = identity["account_id"]
    return {
        "account_id": account,
        "boundary_arn": f"arn:aws:iam::{account}:policy/ecp-workload-boundary",
        "deploy_role_arn": deploy["arn"],
        "plan_role_arn": plan_role["arn"],
        "state_bucket_arn": bucket["arn"],
    }


def digest(document: Any) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:16]


def check(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(problems, report lines). Any problem means: stop, do not apply."""
    problems: list[str] = []
    report: list[str] = []
    if plan.get("errored"):
        problems.append("the plan errored")
    for key in ("resource_drift", "deferred_changes"):
        for item in plan.get(key) or []:
            problems.append(f"{key}: {item.get('address', '?')}")
    seen: dict[str, dict[str, Any]] = {}
    for rc in plan.get("resource_changes", []):
        actions = rc["change"]["actions"]
        if actions in UNCHANGED:
            continue
        address, attrs = rc["address"], changed_attributes(rc["change"])
        seen[address] = rc
        report.append(f"{'+'.join(actions):14} {address}  attributes: {sorted(attrs)}")
        if address not in EXPECTED:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
            continue
        want_actions, allowed = EXPECTED[address]
        if actions != want_actions:
            problems.append(f"{address}: {'+'.join(actions)}, expected {'+'.join(want_actions)}")
        elif allowed is not None and not (attrs and attrs <= allowed):
            problems.append(f"{address}: changes {sorted(attrs)}, only {sorted(allowed)} allowed")
    for address in sorted(set(EXPECTED) - set(seen)):
        problems.append(f"missing expected change: {address}")
    for name, change in (plan.get("output_changes") or {}).items():
        if change["actions"] not in UNCHANGED and change["actions"] != EXPECTED_OUTPUTS.get(name):
            problems.append(f"unexpected output change: {'+'.join(change['actions'])} {name}")
    try:
        values = template_values(plan)
    except ValueError as exc:
        return [*problems, str(exc)], report
    for address, name in TEMPLATES.items():
        rc = seen.get(address)
        if rc is None:
            continue
        if (rc["change"].get("after_unknown") or {}).get("policy"):
            problems.append(f"{address}: policy unknown at plan time")
            continue
        planned = json.loads(rc["change"]["after"]["policy"])
        same = planned == policy_templates.render(name, values)
        report.append(f"{address}: policy sha256 {digest(planned)}, equals {name} template: {same}")
        if not same:
            problems.append(f"{address}: planned policy differs from the {name} template")
    return problems, report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: bootstrap_plan_check.py <plan.json>", file=sys.stderr)
        return 2
    problems, report = check(json.loads(Path(args[0]).read_text()))
    print("\n".join(report))
    for p in problems:
        print(f"STOP  {p}")
    print("OK: exactly the reviewed R2 change" if not problems else "STOP: do not apply this plan")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
