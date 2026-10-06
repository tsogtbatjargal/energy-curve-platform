"""Pre-apply check of the saved infra/org plan for ADR-0021 phase 1c.

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/phase1c_plan_check.py <plan.json>
    # run from the clean checkout the plan was made from

Exit 0 only when the plan does exactly what was reviewed. Terraform's summary is "6 to add,
1 to change, 0 to destroy", with no import:
- the AssumeRoot scoping: an inline policy on the existing AdministratorAccess permission set,
  equal to policies/admin-assume-root.json.tftpl rendered with the workload account's ID from
  state (referenced, never a literal: no 12-digit number may appear in the stack source);
- the ecp-readonly permission set (PT1H) with the AWS-managed ReadOnlyAccess, and both sets
  assigned to the configured Identity Center user for the workload account only;
- centralized root access (RootCredentialsManagement and RootSessions), which depends on the
  scoping policy; the admin assignment depends on it too, and the read-only assignment on its
  policy. The order is read from the plan's configuration;
- the $40 budget updated in place, its legacy cost_filter only: exactly LinkedAccount = the
  workload account (referenced), the project tag filter gone, and no filter_expression or metrics
  (user, 2026-10-06: the provider keeps cost_filter and cost_types next to a filter_expression,
  and the Budgets API refuses both styles together). Nothing else on it changes;
- before it: IAM trusted access enabled out-of-band (the organization's service principals are
  exactly iam and sso), and phase 1b intact in state (the account in Workloads, the SCPs, their
  documents and attachments);
- nothing else: every other resource no-op, no output change, no drift but the refresh of the
  out-of-band call (the organization's principals [sso] -> [iam, sso], an update, planned
  no-op), and the phase 1b source rules (no overrides, JSON configuration or module calls; the
  attachments as reviewed).

Plans hold sensitive values in plain text, so the report prints addresses and actions only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import policy_templates
from bootstrap_plan_check import UNCHANGED, changed_attributes
from org_plan_check import OU, already_owned, prior_values
from phase1b_plan_check import (
    ACCOUNT,
    ORG,
    PHASE_1B_APPLIED,
    check_applied_scps,
    check_drift,
    check_source,
    config_files,
    config_resource,
    references,
)

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "org"
POLICIES = STACK / "policies"

BUDGET = "aws_budgets_budget.project"
INLINE = "aws_ssoadmin_permission_set_inline_policy.admin_assume_root"
READONLY = "aws_ssoadmin_permission_set.readonly"
READONLY_POLICY = "aws_ssoadmin_managed_policy_attachment.readonly"
ADMIN_ASSIGNMENT = "aws_ssoadmin_account_assignment.admin_workloads"
READONLY_ASSIGNMENT = "aws_ssoadmin_account_assignment.readonly_workloads"
FEATURES = "aws_iam_organizations_features.root_access"
CREATES = {INLINE, READONLY, READONLY_POLICY, ADMIN_ASSIGNMENT, READONLY_ASSIGNMENT, FEATURES}

SSO = "data.aws_ssoadmin_instances.this"
ADMIN_SET = "data.aws_ssoadmin_permission_set.admin"
USER = "data.aws_identitystore_user.admin"
ORG_DATA = "data.aws_organizations_organization.this"

PHASE_1B_STATE = PHASE_1B_APPLIED | {ACCOUNT}
TRUSTED_SERVICES = ["iam.amazonaws.com", "sso.amazonaws.com"]
ROOT_FEATURES = ["RootCredentialsManagement", "RootSessions"]
READ_ONLY_ACCESS = "arn:aws:iam::aws:policy/ReadOnlyAccess"
DEPENDS_ON = {  # address -> what it must wait for (ADR-0021: scope first, then enable)
    FEATURES: INLINE,
    ADMIN_ASSIGNMENT: INLINE,
    READONLY_ASSIGNMENT: READONLY_POLICY,
}
ACCOUNT_ID = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")


def assume_root_policy(account_id: str) -> Any:
    return policy_templates.render(
        "admin-assume-root", {"workload_account_id": account_id}, POLICIES
    )


def nested_references(plan: dict[str, Any], address: str, attribute: str) -> set[str]:
    """Every reference inside a (possibly nested) block attribute's configuration expressions."""

    def walk(node: Any) -> set[str]:
        if isinstance(node, dict):
            found = set(node.get("references") or []) if "references" in node else set()
            return found.union(*(walk(v) for k, v in node.items() if k != "references"))
        if isinstance(node, list):
            return set().union(*(walk(v) for v in node))
        return set()

    return walk(config_resource(plan, address).get("expressions", {}).get(attribute))


def is_trusted_access_drift(item: dict[str, Any], planned: dict[str, list[str]]) -> bool:
    """The refresh after the approved out-of-band call (Q1): the organization ignores
    aws_service_access_principals, so it is planned no-op, but refresh still reports the change as
    drift (disposable probe, 2026-10-06: Terraform 1.15.8, provider 6.67.0, local moto). Exactly
    [sso] -> [iam, sso], as an update, and no other attribute."""
    change = item.get("change") or {}
    before, after = change.get("before") or {}, change.get("after") or {}
    differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    return (
        item.get("address") == ORG
        and change.get("actions") == ["update"]
        and planned.get(ORG) == ["no-op"]
        and differing == {"aws_service_access_principals"}
        and before.get("aws_service_access_principals") == ["sso.amazonaws.com"]
        and sorted(after.get("aws_service_access_principals") or []) == TRUSTED_SERVICES
    )


def check_literals(stack_dir: Path) -> list[str]:
    """Q4 (2026-10-06): the account ID comes from a Terraform reference, never a literal."""
    files = [*config_files(stack_dir), *sorted((stack_dir / "policies").glob("*"))]
    return [f"a 12-digit literal in the stack source: {p.name}" for p in files
            if p.is_file() and ACCOUNT_ID.search(p.read_text())]  # fmt: skip


def check_prior(plan: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Phase 1b as applied, the out-of-band trusted access, and the Identity Center values the
    new resources must use, all from the plan's refreshed prior state."""
    problems = []
    ou_id = (prior_values(plan, OU) or {}).get("id")
    problems += check_applied_scps(plan, ou_id)
    account = prior_values(plan, ACCOUNT) or {}
    account_id = account.get("id")
    if not (isinstance(account_id, str) and ACCOUNT_ID.fullmatch(account_id)):
        problems.append(f"{ACCOUNT}: no account ID in state")
        account_id = None
    if account.get("name") != "ecp-workloads" or ou_id is None or account.get("parent_id") != ou_id:
        problems.append(f"{ACCOUNT}: must be ecp-workloads in the Workloads OU")
    principals = (prior_values(plan, ORG_DATA) or {}).get("aws_service_access_principals")
    if sorted(principals or []) != TRUSTED_SERVICES:
        problems.append(
            "trusted access must be exactly iam and sso: enable IAM's first (out-of-band)"
        )
    sso = prior_values(plan, SSO) or {}
    if len(sso.get("arns") or []) != 1:
        problems.append(f"{SSO}: must be exactly one Identity Center instance")
    admin_set = prior_values(plan, ADMIN_SET) or {}
    if admin_set.get("name") != "AdministratorAccess" or not admin_set.get("arn"):
        problems.append(f"{ADMIN_SET}: must be the AdministratorAccess permission set")
    user = prior_values(plan, USER) or {}
    name = ((plan.get("variables") or {}).get("identity_center_user_name") or {}).get("value")
    if not user.get("user_id") or not name or user.get("user_name") != name:
        problems.append(f"{USER}: must be the configured identity_center_user_name")
    return problems, {
        "account_id": account_id,
        "instance_arn": (sso.get("arns") or [None])[0],
        "admin_set_arn": admin_set.get("arn"),
        "user_id": user.get("user_id"),
    }


def check_create(address: str, change: dict[str, Any], plan: dict[str, Any],
                 known: dict[str, Any]) -> list[str]:  # fmt: skip
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    problems = []
    if address != FEATURES and after.get("instance_arn") != known["instance_arn"]:
        problems.append(f"{address}: must use the Identity Center instance")
    readonly_ref = {f"{READONLY}.arn", READONLY}
    if address == INLINE:
        if after.get("permission_set_arn") != known["admin_set_arn"]:
            problems.append(f"{INLINE}: must be on the AdministratorAccess permission set")
        if f"{ACCOUNT}.id" not in references(plan, INLINE, "inline_policy"):
            problems.append(f"{INLINE}: the account ID must come from {ACCOUNT}")
        if (
            unknown.get("inline_policy")
            or known["account_id"] is None
            or json.loads(after.get("inline_policy") or "null")
            != assume_root_policy(known["account_id"])
        ):
            problems.append(f"{INLINE}: differs from policies/admin-assume-root.json.tftpl")
    elif address == READONLY:
        if (after.get("name"), after.get("session_duration")) != ("ecp-readonly", "PT1H"):
            problems.append(f"{READONLY}: must be ecp-readonly with PT1H sessions")
        if after.get("relay_state"):
            problems.append(f"{READONLY}: no relay state")
    elif address == READONLY_POLICY:
        if after.get("managed_policy_arn") != READ_ONLY_ACCESS:
            problems.append(f"{READONLY_POLICY}: must attach ReadOnlyAccess")
        if (
            not unknown.get("permission_set_arn")
            or set(references(plan, READONLY_POLICY, "permission_set_arn")) != readonly_ref
        ):
            problems.append(f"{READONLY_POLICY}: must be on {READONLY}")
    elif address in (ADMIN_ASSIGNMENT, READONLY_ASSIGNMENT):
        if address == ADMIN_ASSIGNMENT:
            if after.get("permission_set_arn") != known["admin_set_arn"]:
                problems.append(f"{address}: must assign AdministratorAccess")
        elif (
            not unknown.get("permission_set_arn")
            or set(references(plan, address, "permission_set_arn")) != readonly_ref
        ):
            problems.append(f"{address}: must assign {READONLY}")
        if (after.get("principal_type"), after.get("principal_id")) != ("USER", known["user_id"]):
            problems.append(f"{address}: must be the configured Identity Center user")
        if (
            after.get("target_type") != "AWS_ACCOUNT"
            or known["account_id"] is None
            or after.get("target_id") != known["account_id"]
            or f"{ACCOUNT}.id" not in references(plan, address, "target_id")
        ):
            problems.append(f"{address}: must target the workload account only")
    elif address == FEATURES and sorted(after.get("enabled_features") or []) != ROOT_FEATURES:
        problems.append(f"{FEATURES}: must enable exactly {' and '.join(ROOT_FEATURES)}")
    return problems


def check_budget(change: dict[str, Any], plan: dict[str, Any], account_id: str | None) -> list[str]:
    after = change.get("after") or {}
    problems = []
    if changed_attributes(change) != {"cost_filter"}:
        problems.append(f"{BUDGET}: may change only cost_filter")
    want = [{"name": "LinkedAccount", "values": [account_id]}]
    if account_id is None or after.get("cost_filter") != want:
        problems.append(
            f"{BUDGET}: the filter must be exactly LinkedAccount = the workload account"
        )
    if f"{ACCOUNT}.id" not in nested_references(plan, BUDGET, "cost_filter"):
        problems.append(f"{BUDGET}: the account ID must come from {ACCOUNT}")
    if after.get("filter_expression") or after.get("metrics"):
        problems.append(f"{BUDGET}: no filter_expression or metrics")
    return problems


def check(plan: dict[str, Any], stack_dir: Path = STACK) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    report: list[str] = []
    if plan.get("errored"):
        problems.append("the plan errored")
    if plan.get("complete") is False:
        problems.append("the plan is incomplete")
    for item in plan.get("deferred_changes") or []:
        problems.append(f"deferred_changes: {item.get('address', '?')}")
    if set(already_owned(plan)) != PHASE_1B_STATE:
        problems.append("infra/org state is not exactly phase 1b as applied")
    planned = {rc["address"]: rc["change"]["actions"] for rc in plan.get("resource_changes", [])}
    drift = plan.get("resource_drift") or []
    trusted = [d for d in drift if is_trusted_access_drift(d, planned)]
    rest = [d for d in drift if d not in trusted] + trusted[1:]  # at most once
    problems += check_drift({"resource_drift": rest}, planned, frozenset())
    problems += check_source(stack_dir)
    problems += check_literals(stack_dir)
    root_module = (plan.get("configuration") or {}).get("root_module") or {}
    for name in sorted(root_module.get("module_calls") or {}):
        problems.append(f"module calls are not allowed in the stack: {name}")
    prior_problems, known = check_prior(plan)
    problems += prior_problems
    seen = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        if change.get("importing") is not None:
            problems.append(f"unexpected import: {address}")
        if actions in UNCHANGED:
            continue
        report.append(f"{'+'.join(actions):8} {address}")
        seen.add(address)
        if address in CREATES and actions == ["create"]:
            problems += check_create(address, change, plan, known)
        elif address == BUDGET and actions == ["update"]:
            problems += check_budget(change, plan, known["account_id"])
        else:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
    for address in sorted((CREATES | {BUDGET}) - seen):
        problems.append(f"missing change: {address}")
    for address, prerequisite in sorted(DEPENDS_ON.items()):
        if prerequisite not in (config_resource(plan, address).get("depends_on") or []):
            problems.append(f"{address}: must depend on {prerequisite}")
    for name, out in (plan.get("output_changes") or {}).items():
        if out["actions"] not in UNCHANGED:
            problems.append(f"unexpected output change: {'+'.join(out['actions'])} {name}")
    report.append(
        "summary: 0 to import, 6 to add (the scoping policy, ecp-readonly and its policy, two "
        "assignments, root access), 1 to change (the budget filter), 0 to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: phase1c_plan_check.py <org-plan.json>", file=sys.stderr)
        return 2
    problems, report = check(json.loads(Path(args[0]).read_text()))
    print("\n".join(report))
    for p in problems:
        print(f"STOP  {p}")
    print("OK: exactly the reviewed phase 1c change" if not problems else "STOP: do not apply")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
