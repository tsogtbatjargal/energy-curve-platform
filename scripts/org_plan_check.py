"""Pre-apply check of the two saved plans for ADR-0021 phase 1a (ownership transfer and the OU).

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/org_plan_check.py org <plan.json>        # infra/org
    python scripts/org_plan_check.py bootstrap <plan.json>  # the management infra/bootstrap

Exit 0 only when the plan does exactly what was reviewed:
- `org`: import exactly the nine resources in MOVED, each with no change (a plain import), and
  create exactly one resource, the `Workloads` OU under the organization root. Nothing else: no
  update, replacement, deletion or forget; no other import; no account, SCP, Identity Center or
  root-access resource anywhere in the configuration; a fresh state (nothing in `prior_state`
  but the resources being imported).
- `bootstrap`: forget exactly the same nine resources (`removed`, `destroy = false`) and change
  nothing else; every output unchanged.

Plan summary for `org`: "9 to import, 1 to add, 0 to change, 0 to destroy". The one addition is the
OU; the imports are not additions. For `bootstrap`: "0 to add, 0 to change, 0 to destroy", and the
nine resources are listed as no longer managed (forgotten, not destroyed).

Apply order: `org` first (both states then hold the nine resources; nothing changes them), then
`bootstrap`. Plans hold sensitive values in plain text, so the report prints addresses, actions and
attribute names only, never values (import IDs contain the account ID and are compared, not shown).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from bootstrap_plan_check import UNCHANGED, changed_attributes

BUCKET_RESOURCES = (
    "aws_s3_bucket",
    "aws_s3_bucket_versioning",
    "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_ownership_controls",
    "aws_s3_bucket_lifecycle_configuration",
    "aws_s3_bucket_policy",
)
MOVED = frozenset(
    [f"{t}.tfstate" for t in BUCKET_RESOURCES]
    + ["aws_budgets_budget.project", "aws_ce_cost_allocation_tag.project"]
)
OU = "aws_organizations_organizational_unit.workloads"
OU_NAME = "Workloads"
ROOT_ID = re.compile(r"^r-[0-9a-z]{4,32}$")
# Later phases only (1b, 1c): their presence in a phase 1a configuration is a stop.
LATER_PHASE_TYPES = re.compile(
    r"^aws_(organizations_account|organizations_policy.*|ssoadmin_.*|iam_organizations_features)$"
)


def expected_import_id(address: str, after: dict[str, Any]) -> str | None:
    if address == "aws_ce_cost_allocation_tag.project":
        return "project"
    if address == "aws_budgets_budget.project":
        return f"{after.get('account_id')}:{after.get('name')}"
    return after.get("bucket")


def common(plan: dict[str, Any]) -> list[str]:
    problems = []
    if plan.get("errored"):
        problems.append("the plan errored")
    if plan.get("complete") is False:
        problems.append("the plan is incomplete")
    for key in ("resource_drift", "deferred_changes"):
        for item in plan.get(key) or []:
            problems.append(f"{key}: {item.get('address', '?')}")
    return problems


def configured_types(plan: dict[str, Any]) -> set[str]:
    root = (plan.get("configuration") or {}).get("root_module") or {}
    return {r.get("type", "") for r in root.get("resources", [])}


def already_owned(plan: dict[str, Any]) -> list[str]:
    """Managed resources the destination state holds before this plan. Terraform also lists every
    resource it is importing in `prior_state` (with `change.importing` set), even when no state
    exists, so those are pending imports, not ownership."""
    prior = (plan.get("prior_state") or {}).get("values", {}).get("root_module", {})
    importing = {
        rc["address"]
        for rc in plan.get("resource_changes", [])
        if rc["change"].get("importing") is not None
    }
    return sorted(
        r["address"]
        for r in prior.get("resources", [])
        if r.get("mode") == "managed" and r["address"] not in importing
    )


def check_org(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    problems, report = common(plan), []
    for address in already_owned(plan):
        problems.append(f"infra/org state already owns: {address}")
    for t in sorted(configured_types(plan)):
        if LATER_PHASE_TYPES.match(t):
            problems.append(f"later-phase resource type in the configuration: {t}")
    imported, created = set(), set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions, importing = change["actions"], change.get("importing")
        if LATER_PHASE_TYPES.match(rc.get("type", "")):
            problems.append(f"later-phase resource in the plan: {address}")
        if importing is not None:
            attrs = changed_attributes(change)
            report.append(f"import+{'+'.join(actions):10} {address}  changes: {sorted(attrs)}")
            if address not in MOVED:
                problems.append(f"unexpected import: {address}")
            elif actions != ["no-op"] or attrs:
                problems.append(f"{address}: the import is not a plain import ({sorted(attrs)})")
            elif importing.get("id") != expected_import_id(address, change.get("after") or {}):
                problems.append(f"{address}: import ID is not the expected resource")
            imported.add(address)
            continue
        if actions in UNCHANGED:
            continue
        report.append(f"{'+'.join(actions):17} {address}")
        if address != OU or actions != ["create"]:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
            continue
        after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
        if after.get("name") != OU_NAME:
            problems.append(f"{OU}: name must be {OU_NAME}")
        if unknown.get("parent_id") or not ROOT_ID.match(str(after.get("parent_id"))):
            problems.append(f"{OU}: parent must be the organization root, known at plan time")
        created.add(address)
    for address in sorted(MOVED - imported):
        problems.append(f"missing import: {address}")
    if OU not in created:
        problems.append(f"missing create: {OU}")
    for name, change in (plan.get("output_changes") or {}).items():
        if change["actions"] not in UNCHANGED and (name, change["actions"]) != (
            "workloads_ou_id",
            ["create"],
        ):
            problems.append(f"unexpected output change: {'+'.join(change['actions'])} {name}")
    report.append(
        f"summary: {len(imported)} to import, {len(created)} to add, 0 to change, 0 to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def check_bootstrap(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    problems, report = common(plan), []
    forgotten = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        if change.get("importing") is not None:
            problems.append(f"unexpected import: {address}")
        if actions in UNCHANGED:
            continue
        report.append(f"{'+'.join(actions):17} {address}")
        if actions == ["forget"] and address in MOVED:
            forgotten.add(address)
        else:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
    for address in sorted(MOVED - forgotten):
        problems.append(f"missing forget: {address}")
    for name, change in (plan.get("output_changes") or {}).items():
        if change["actions"] not in UNCHANGED:
            problems.append(f"unexpected output change: {'+'.join(change['actions'])} {name}")
    report.append(
        f"summary: 0 to add, 0 to change, 0 to destroy, {len(forgotten)} forgotten"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


CHECKS = {"org": check_org, "bootstrap": check_bootstrap}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in CHECKS:
        print("usage: org_plan_check.py {org|bootstrap} <plan.json>", file=sys.stderr)
        return 2
    problems, report = CHECKS[args[0]](json.loads(Path(args[1]).read_text()))
    print("\n".join(report))
    for p in problems:
        print(f"STOP  {p}")
    print(
        f"OK: exactly the reviewed phase 1a {args[0]} change"
        if not problems
        else "STOP: do not apply this plan"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
