"""Pre-apply check of the saved infra/org plan for ADR-0021 phase 1b (the account and its SCPs).

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/phase1b_plan_check.py <plan.json>   # from the checkout the plan was made from

Exit 0 only when the plan does exactly what was reviewed. Terraform's summary is "1 to import,
5 to add, 1 to change, 0 to destroy":
- import the organization and change only `enabled_policy_types`, from none to
  ["SERVICE_CONTROL_POLICY"]: the trusted-service principals and the feature set are unchanged;
- create the two SCPs, each equal to its reviewed document in infra/org/policies/;
- attach each to the Workloads OU, once, with policy_id exactly its own policy's id: checked in the
  plan (exact references, unknown value) and, because plan JSON drops functions and literals, in
  the stack source (`replace(<policy>.id, ...)` has the same references);
- create the account `ecp-workloads` with parent_id the Workloads OU, the configured email
  (compared, never printed), `close_on_deletion = false`, the break-glass role name, billing
  access ALLOW and no GovCloud account, after both attachments (`depends_on`). AWS creates it
  under the root and the provider then moves it into the OU (ADR-0021, the bootstrap window);
  the plan shows only the final parent;
- nothing else: every phase 1a resource unchanged; no Identity Center, root-access,
  service-access, delegated-administrator or budget change (phase 1c); no drift except the OU's
  tags reading back as {} where state had null, on the OU planned no-op.

Plans hold sensitive values in plain text, so the report prints addresses, actions and attribute
names only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from bootstrap_plan_check import UNCHANGED, changed_attributes
from org_plan_check import MOVED, OU, already_owned, prior_values

ROOT = Path(__file__).resolve().parents[1]
ORG = "aws_organizations_organization.this"
ACCOUNT = "aws_organizations_account.workloads"
SCPS = {  # policy address -> (name, document, attachment address)
    "aws_organizations_policy.workloads_baseline": (
        "ecp-workloads-baseline",
        "scp-workloads-baseline.json",
        "aws_organizations_policy_attachment.workloads_baseline",
    ),
    "aws_organizations_policy.workloads_protect": (
        "ecp-workloads-protect",
        "scp-workloads-protect.json",
        "aws_organizations_policy_attachment.workloads_protect",
    ),
}
ATTACHMENTS = {attachment: policy for policy, (_, _, attachment) in SCPS.items()}
CREATES = {*SCPS, *ATTACHMENTS, ACCOUNT}
ACCOUNT_EXPECTED = {
    "name": "ecp-workloads",
    "close_on_deletion": False,
    "role_name": "OrganizationAccountAccessRole",
    "iam_user_access_to_billing": "ALLOW",
    "create_govcloud": False,
}
PHASE_1A_STATE = MOVED | {OU}


def document(name: str) -> Any:
    return json.loads((ROOT / "infra" / "org" / "policies" / name).read_text())


def config_resource(plan: dict[str, Any], address: str) -> dict[str, Any]:
    root = (plan.get("configuration") or {}).get("root_module") or {}
    return next((r for r in root.get("resources", []) if r.get("address") == address), {})


def references(plan: dict[str, Any], address: str, attribute: str) -> list[str]:
    expr = config_resource(plan, address).get("expressions", {}).get(attribute) or {}
    return list(expr.get("references") or [])


def check_drift(plan: dict[str, Any], planned: dict[str, list[str]]) -> list[str]:
    problems = []
    for item in plan.get("resource_drift") or []:
        address, change = item.get("address"), item.get("change") or {}
        before, after = change.get("before") or {}, change.get("after") or {}
        differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        if not (
            address == OU
            and change.get("actions") == ["update"]
            and planned.get(address) == ["no-op"]
            and differing == {"tags"}
            and before.get("tags") is None
            and after.get("tags") == {}
        ):
            problems.append(f"resource_drift: {address}")
    return problems


def check_org(change: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    before, after = change.get("before") or {}, change.get("after") or {}
    org_data = prior_values(plan, "data.aws_organizations_organization.this") or {}
    problems = []
    if change.get("importing") is None or change["importing"].get("id") != org_data.get("id"):
        problems.append(f"{ORG}: must be an import of this organization")
    if change["actions"] != ["update"] or changed_attributes(change) != {"enabled_policy_types"}:
        problems.append(f"{ORG}: may change enabled_policy_types only")
    if before.get("enabled_policy_types") not in (None, []) or after.get(
        "enabled_policy_types"
    ) != ["SERVICE_CONTROL_POLICY"]:
        problems.append(f"{ORG}: must enable exactly SERVICE_CONTROL_POLICY")
    if before.get("feature_set") != "ALL" or after.get("feature_set") != "ALL":
        problems.append(f"{ORG}: feature set must stay ALL")
    return problems


def check_scp(address: str, change: dict[str, Any]) -> list[str]:
    name, doc, _ = SCPS[address]
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    problems = []
    if after.get("name") != name or after.get("type") != "SERVICE_CONTROL_POLICY":
        problems.append(f"{address}: must be the SCP {name}")
    if unknown.get("content") or json.loads(after.get("content") or "null") != document(doc):
        problems.append(f"{address}: content differs from policies/{doc}")
    return problems


def check_attachment(
    address: str, change: dict[str, Any], plan: dict[str, Any], ou_id: str | None
) -> list[str]:
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    problems = []
    if unknown.get("target_id") or ou_id is None or after.get("target_id") != ou_id:
        problems.append(f"{address}: must target the Workloads OU")
    # Exactly the direct reference: plan JSON drops literals and functions, so any other reference
    # (the other policy, a variable) could decide the value. The policy is created in this plan,
    # so its ID is unknown; a known policy_id is some other policy, whatever the references say.
    policy = ATTACHMENTS[address]
    direct = {f"{policy}.id", policy}
    if set(references(plan, address, "policy_id")) != direct or not unknown.get("policy_id"):
        problems.append(f"{address}: must attach {policy}")
    return problems


OU_ID_EXPR = "${aws_organizations_organizational_unit.workloads.id}"


def source_attachments(stack_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Every aws_organizations_policy_attachment block in the stack source, by address."""
    import hcl2
    from hcl2.utils import SerializationOptions

    found: dict[str, list[dict[str, Any]]] = {}
    for tf in sorted(stack_dir.glob("*.tf")):
        with tf.open() as fh:
            doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
        for block in doc.get("resource", []):
            for type_, named in block.items():
                if type_.strip('"') != "aws_organizations_policy_attachment":
                    continue
                for name, body in named.items():
                    address = f"aws_organizations_policy_attachment.{name.strip(chr(34))}"
                    found.setdefault(address, []).append(body)
    return found


def check_source(stack_dir: Path) -> list[str]:
    """Plan JSON keeps an expression's references but not its functions or literals, so
    replace(<policy>.id, ...) looks exactly like <policy>.id there. Read the expressions from the
    source instead: each attachment is declared once, sets only policy_id and target_id, and both
    are the plain references. Run it from the clean checkout the saved plan was made from."""
    try:
        found = source_attachments(stack_dir)
    except Exception as exc:  # noqa: BLE001 - any parse failure means the source is unverified
        return [f"cannot read the attachments from {stack_dir}: {type(exc).__name__}"]
    problems = []
    for address in sorted(set(found) - set(ATTACHMENTS)):
        problems.append(f"unexpected attachment in the source: {address}")
    for address, policy in sorted(ATTACHMENTS.items()):
        bodies = found.get(address, [])
        if len(bodies) != 1:
            problems.append(f"{address}: must be declared exactly once in the source")
            continue
        body = {k: v for k, v in bodies[0].items() if k != "__is_block__"}
        if set(body) != {"policy_id", "target_id"}:
            problems.append(f"{address}: may set only policy_id and target_id in the source")
        if body.get("policy_id") != f"${{{policy}.id}}":
            problems.append(f"{address}: policy_id must be exactly {policy}.id in the source")
        if body.get("target_id") != OU_ID_EXPR:
            problems.append(f"{address}: target_id must be exactly the Workloads OU id")
    return problems


def check_account(change: dict[str, Any], plan: dict[str, Any], ou_id: str | None) -> list[str]:
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    problems = []
    for key, want in ACCOUNT_EXPECTED.items():
        if after.get(key) != want:
            problems.append(f"{ACCOUNT}: {key} must be {want!r}")
    if unknown.get("parent_id") or ou_id is None or after.get("parent_id") != ou_id:
        problems.append(f"{ACCOUNT}: must be created in the Workloads OU")
    email = ((plan.get("variables") or {}).get("workload_account_email") or {}).get("value")
    if unknown.get("email") or not email or after.get("email") != email:
        problems.append(f"{ACCOUNT}: email must be the configured workload_account_email")
    if (change.get("after_sensitive") or {}).get("email") is not True:
        problems.append(f"{ACCOUNT}: email must stay sensitive")
    depends = set(config_resource(plan, ACCOUNT).get("depends_on") or [])
    if not set(ATTACHMENTS) <= depends:
        problems.append(f"{ACCOUNT}: must depend on both SCP attachments")
    return problems


def check(
    plan: dict[str, Any], stack_dir: Path = ROOT / "infra" / "org"
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    report: list[str] = []
    if plan.get("errored"):
        problems.append("the plan errored")
    if plan.get("complete") is False:
        problems.append("the plan is incomplete")
    for item in plan.get("deferred_changes") or []:
        problems.append(f"deferred_changes: {item.get('address', '?')}")
    owned = set(already_owned(plan))
    if owned != PHASE_1A_STATE:
        problems.append("infra/org state is not exactly the phase 1a resources")
    planned = {rc["address"]: rc["change"]["actions"] for rc in plan.get("resource_changes", [])}
    problems += check_drift(plan, planned)
    problems += check_source(stack_dir)
    ou_id = (prior_values(plan, OU) or {}).get("id")
    seen = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        if actions in UNCHANGED and change.get("importing") is None:
            continue
        report.append(
            f"{'+'.join(actions):8} {address}" + ("  (import)" if change.get("importing") else "")
        )
        seen.add(address)
        if address == ORG:
            problems += check_org(change, plan)
        elif address in CREATES and actions == ["create"]:
            if address in SCPS:
                problems += check_scp(address, change)
            elif address in ATTACHMENTS:
                problems += check_attachment(address, change, plan, ou_id)
            else:
                problems += check_account(change, plan, ou_id)
        else:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
    for address in sorted(({ORG} | CREATES) - seen):
        problems.append(f"missing change: {address}")
    for name, change in (plan.get("output_changes") or {}).items():
        if change["actions"] not in UNCHANGED and (name, change["actions"]) != (
            "workload_account_id",
            ["create"],
        ):
            problems.append(f"unexpected output change: {'+'.join(change['actions'])} {name}")
    report.append(
        "summary: 1 to import (the organization), 5 to add (2 SCPs, 2 attachments, the account), "
        "1 to change (the import enables SCPs), 0 to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: phase1b_plan_check.py <org-plan.json>", file=sys.stderr)
        return 2
    problems, report = check(json.loads(Path(args[0]).read_text()))
    print("\n".join(report))
    for p in problems:
        print(f"STOP  {p}")
    print("OK: exactly the reviewed phase 1b change" if not problems else "STOP: do not apply")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
