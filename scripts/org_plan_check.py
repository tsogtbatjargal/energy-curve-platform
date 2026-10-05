"""Pre-apply check of the two saved plans for ADR-0021 phase 1a (ownership transfer and the OU).

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/org_plan_check.py <org-plan.json> <bootstrap-plan.json>

Both plans are checked together, and exit 0 only when both do exactly what was reviewed:
- `org`: import exactly the nine resources in MOVED, each with no change (a plain import), and
  create exactly one resource, the `Workloads` OU under the organization root. Nothing else: no
  update, replacement, deletion or forget, except the budget import's state-only update when
  only its sensitivity marks differ (the alert emails become sensitive; values identical); no
  other import; no account, SCP, Identity Center or root-access resource anywhere in the
  configuration; a fresh state (nothing in `prior_state` but the resources being imported).
- `bootstrap`: forget exactly the same nine resources (`removed`, `destroy = false`) and change
  nothing else; every output unchanged. Refresh drift stops, except two reviewed cases on
  resources planned no-op: the boundary policy's tags reading back as {} instead of null, and the
  deploy role's stored inline policy catching up with `aws_iam_role_policy.gha_deploy_iam`.
- `transfer`: each import is the very object bootstrap relinquishes (bucket name; budget account
  and name; tag key), in its import ID, `before` and `after`, with the expected values taken from
  the bootstrap plan's `forget` changes. Both plans are for the same account, the budget is in it,
  and the bucket is its `ecp-tfstate-<account>-` bucket.

Terraform's summary for `org`: "9 to import, 1 to add, 1 to change, 0 to destroy". The one addition
is the OU; imports are not additions. The one change is the budget import's sensitivity marks: it
is applied to state only, and Terraform makes no AWS call for it (Terraform 1.15.8,
node_resource_abstract_instance.go). For `bootstrap`: "0 to add, 0 to change, 0 to destroy", and
the nine resources are listed as no longer managed (forgotten, not destroyed); the apply also
records the two refreshed values in state.

Apply order: `org` first (both states then hold the nine resources; no AWS object changes), then
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
BUDGET = "aws_budgets_budget.project"
OU_NAME = "Workloads"
ROOT_ID = re.compile(r"^r-[0-9a-z]{4,32}$")
# Later phases only (1b, 1c): their presence in a phase 1a configuration is a stop.
LATER_PHASE_TYPES = re.compile(
    r"^aws_(organizations_account|organizations_policy.*|ssoadmin_.*|iam_organizations_features)$"
)


def identity(address: str, values: dict[str, Any]) -> tuple[Any, ...]:
    """What makes a moved resource that object: its tag key, the budget's account and name, or
    the bucket name for each of the seven bucket resources."""
    if address == "aws_ce_cost_allocation_tag.project":
        return (values.get("tag_key"),)
    if address == "aws_budgets_budget.project":
        return (values.get("account_id"), values.get("name"))
    return (values.get("bucket"),)


def import_id(address: str, ident: tuple[Any, ...]) -> str:
    return ":".join(str(part) for part in ident)


DEPLOY_ROLE, DEPLOY_ROLE_POLICY = "aws_iam_role.gha_deploy", "aws_iam_role_policy.gha_deploy_iam"
DEPLOY_INLINE_POLICY = "ecp-scoped-iam-and-state"
WORKLOAD_BOUNDARY = "aws_iam_policy.workload_boundary"


def prior_values(plan: dict[str, Any], address: str) -> dict[str, Any] | None:
    prior = (plan.get("prior_state") or {}).get("values", {}).get("root_module", {})
    return next(
        (r.get("values") for r in prior.get("resources", []) if r["address"] == address), None
    )


def explained_drift(plan: dict[str, Any], item: dict[str, Any]) -> str | None:
    """The bootstrap plan's two reviewed refresh cases (2026-10-05), each on a resource planned
    no-op; anything else is unexplained and stops. Refresh drift changes nothing in AWS; the apply
    only records the refreshed values in state."""
    address, change = item.get("address"), item.get("change") or {}
    if change.get("actions") != ["update"]:
        return None
    planned = {rc["address"]: rc["change"]["actions"] for rc in plan.get("resource_changes", [])}
    if planned.get(address) != ["no-op"]:
        return None
    before, after = change.get("before") or {}, change.get("after") or {}
    differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    if (
        address == WORKLOAD_BOUNDARY
        and differing == {"tags"}
        and before.get("tags") is None
        and after.get("tags") == {}
    ):
        return "tags null -> {}"
    if address == DEPLOY_ROLE and differing == {"inline_policy"}:
        old, new = before.get("inline_policy") or [], after.get("inline_policy") or []
        role_policy = prior_values(plan, DEPLOY_ROLE_POLICY)
        if (
            len(old) == len(new) == 1
            and old[0].get("name") == new[0].get("name") == DEPLOY_INLINE_POLICY
            and planned.get(DEPLOY_ROLE_POLICY) == ["no-op"]
            and role_policy is not None
            and json.loads(new[0]["policy"]) == json.loads(role_policy["policy"])
        ):
            return f"inline_policy {DEPLOY_INLINE_POLICY} now equals {DEPLOY_ROLE_POLICY}"
    return None


def common(plan: dict[str, Any], allow_drift: bool = False) -> tuple[list[str], list[str]]:
    """(problems, report). Drift stops, except the reviewed bootstrap cases when allow_drift."""
    problems, report = [], []
    if plan.get("errored"):
        problems.append("the plan errored")
    if plan.get("complete") is False:
        problems.append("the plan is incomplete")
    for item in plan.get("resource_drift") or []:
        reason = explained_drift(plan, item) if allow_drift else None
        if reason is None:
            problems.append(f"resource_drift: {item.get('address', '?')}")
        else:
            report.append(f"drift (refresh only, planned no-op) {item['address']}: {reason}")
    for item in plan.get("deferred_changes") or []:
        problems.append(f"deferred_changes: {item.get('address', '?')}")
    return problems, report


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
    problems, report = common(plan)
    for address in already_owned(plan):
        problems.append(f"infra/org state already owns: {address}")
    for t in sorted(configured_types(plan)):
        if LATER_PHASE_TYPES.match(t):
            problems.append(f"later-phase resource type in the configuration: {t}")
    imported, created, state_only = set(), set(), set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions, importing = change["actions"], change.get("importing")
        if LATER_PHASE_TYPES.match(rc.get("type", "")):
            problems.append(f"later-phase resource in the plan: {address}")
        if importing is not None:
            attrs = changed_attributes(change)
            if (
                address == BUDGET
                and actions == ["update"]
                and not attrs
                and emails_newly_marked(change)
            ):
                # Values identical, marks only: Terraform 1.15.8 records the new marks in state
                # without calling the provider (node_resource_abstract_instance.go, apply).
                report.append(
                    f"import+state-only {address}  sensitivity marks only: notification "
                    "(values identical; recorded in state, no AWS call)"
                )
                state_only.add(address)
            else:
                report.append(f"import+{'+'.join(actions):10} {address}  changes: {sorted(attrs)}")
            if address not in MOVED:
                problems.append(f"unexpected import: {address}")
            elif (actions != ["no-op"] or attrs) and address not in state_only:
                problems.append(f"{address}: the import is not a plain import ({sorted(attrs)})")
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
        f"summary: {len(imported)} to import, {len(created)} to add, "
        + (
            "1 to change (state-only: sensitivity marks on the imported budget)"
            if state_only
            else "0 to change"
        )
        + ", 0 to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def check_bootstrap(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    problems, report = common(plan, allow_drift=True)
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


def emails_newly_marked(change: dict[str, Any]) -> bool:
    """The budget import's one reviewed difference: `notification` (which holds the alert emails)
    becomes sensitive, and no other mark changes. The import reads the emails from AWS unmarked;
    the configuration's sensitive variable marks the whole set."""
    before, after = change.get("before_sensitive"), change.get("after_sensitive")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False

    def rest(marks: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in marks.items() if k != "notification"}

    return (
        rest(before) == rest(after)
        and after.get("notification") is True
        and before.get("notification") is not True
    )


def caller_account(plan: dict[str, Any]) -> str | None:
    prior = (plan.get("prior_state") or {}).get("values", {}).get("root_module", {})
    for r in prior.get("resources", []):
        if r["address"] == "data.aws_caller_identity.current":
            return (r.get("values") or {}).get("account_id")
    return None


def check_transfer(org: dict[str, Any], bootstrap: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Each import must be the very object bootstrap relinquishes. The expected identities come
    from the bootstrap plan's `forget` changes, whose `before` is read from bootstrap's own state,
    not from the import being checked; an import that is merely consistent with itself (another
    bucket, budget or account throughout) stops."""
    problems: list[str] = []
    account = caller_account(org)
    if account is None or account != caller_account(bootstrap):
        return ["the org and bootstrap plans are not for the same account"], []
    relinquished = {
        rc["address"]: rc["change"].get("before") or {}
        for rc in bootstrap.get("resource_changes", [])
        if rc["address"] in MOVED and rc["change"]["actions"] == ["forget"]
    }
    imports = {
        rc["address"]: rc["change"]
        for rc in org.get("resource_changes", [])
        if rc["change"].get("importing") is not None
    }
    matched = 0
    for address in sorted(MOVED):
        if address not in relinquished:
            problems.append(f"{address}: bootstrap does not relinquish it")
            continue
        want = identity(address, relinquished[address])
        if None in want:
            problems.append(f"{address}: bootstrap's record has no identity")
            continue
        if address == "aws_budgets_budget.project" and want[0] != account:
            problems.append(f"{address}: the budget is not in the plans' account")
        if address == "aws_s3_bucket.tfstate" and not str(want[0]).startswith(
            f"ecp-tfstate-{account}-"
        ):
            problems.append(f"{address}: the bucket is not the account's state bucket")
        change = imports.get(address)
        if change is None:
            continue  # check_org reports the missing import
        if (
            change["importing"].get("id") != import_id(address, want)
            or identity(address, change.get("before") or {}) != want
            or identity(address, change.get("after") or {}) != want
        ):
            problems.append(f"{address}: imports another object than bootstrap relinquishes")
        else:
            matched += 1
    report = [
        f"transfer: all {matched} imports are the objects bootstrap relinquishes"
        if not problems and matched == len(MOVED)
        else "transfer: not the objects bootstrap relinquishes"
    ]
    return problems, report


def check(org: dict[str, Any], bootstrap: dict[str, Any]) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    report: list[str] = []
    for name, (p, r) in (
        ("org", check_org(org)),
        ("bootstrap", check_bootstrap(bootstrap)),
        ("transfer", check_transfer(org, bootstrap)),
    ):
        problems += [f"{name}: {x}" for x in p]
        report += [f"[{name}] {x}" for x in r]
    return problems, report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or not all(a.endswith(".json") for a in args):
        print("usage: org_plan_check.py <org-plan.json> <bootstrap-plan.json>", file=sys.stderr)
        return 2
    org, bootstrap = (json.loads(Path(a).read_text()) for a in args)
    problems, report = check(org, bootstrap)
    print("\n".join(report))
    for p in problems:
        print(f"STOP  {p}")
    print(
        "OK: exactly the reviewed phase 1a transfer" if not problems else "STOP: apply neither plan"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
