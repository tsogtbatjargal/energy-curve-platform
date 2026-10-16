"""Pre-apply check of a saved infra/redshift plan (M5c-1, ADR-0023).

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/redshift_plan_check.py [--change create|update|destroy] <plan.json>

Run it from the clean checkout the saved plan was made from: it reads infra/redshift's source and
its policy templates. It runs after policy_gate.py --stack redshift (ADR-0006 rules, PLAN.md R1 and
R2), not instead of it. One stage, create mode only.

The plan must be exactly the 18 creates of the reviewed stack, from an empty state:
- the network: a VPC (10.43.0.0/16), the default security group emptied, three private subnets in
  the three Availability Zones the account reports, a route table with no route and its three
  associations, and a security group with no rule. Nothing that leads out of the VPC.
- two roles, each with the workload boundary and a trust and an inline policy equal to the reviewed
  templates for this account, known at plan time; and the Redshift service-linked role.
- the namespace (IAM only: no admin password of any kind, no managed secret, no key, no admin name
  chosen by us), the workgroup (base and maximum capacity 8, not public, enhanced VPC routing off,
  tagged stack=redshift, which the Data API role's policy conditions on), and the usage limit
  (serverless-compute, daily, 4 RPU-hours, deactivate: a backstop, not a cap).

The wiring is checked too: each value that ties one resource to another is either exactly the
reviewed value or, when unknown at plan time, comes from an exact reference to this stack's own
resource in the plan's configuration. Terraform lists the attribute reference, with or without the
resource itself; anything else (a foreign resource, a variable, no expression) fails. Both
warehouse resources must declare that they wait for the service-linked role.

`--change update` is for the applied stack after the refresh: 18 no-ops and 0 drift, every
resource still the reviewed one. The provider's read-back is accepted where AWS fills it in, and
nothing else: tags as {}, empty rules and routes, each role's own reviewed policy as its only inline
policy, and on the namespace the service's default admin name "admin" (Security Hub control
RedshiftServerless.5; no password or secret was set, and there is no network path to a password
login, ADR-0023) and the AWS-owned key "AWS_OWNED_KMS_KEY", which is what the service reports when
no key is configured. Any other admin name or key fails. The wiring is checked by reference in the
configuration even though the ids are known by then.

`--change destroy` is the teardown: exactly the 17 deletes of every resource but the Redshift
service-linked role, which is kept (it is account-wide and shared). Terraform 1.15.8 does not honour
`removed { lifecycle { destroy = false } }` under `plan -destroy` (probed with terraform_data), so
the role is taken out of state first; the state must hold exactly the other 17 and the plan may
never mention the role. Nothing from any other stack passes.

Nothing else: no module, gateway, NAT, endpoint, address, route, rule resource, snapshot, endpoint
access, resource policy, secret, key or any other service. Also: a sound plan (not errored,
complete, no deferred changes, no drift, no output changes), ca-central-1, the caller is
expected_account_id and not the management account, and the source has no module block, override
file or JSON configuration.

Plans hold sensitive values in plain text, so the report prints addresses, actions and attribute
names only, never values.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import policy_templates
from bootstrap_plan_check import UNCHANGED, dig, prior, variable
from glue_plan_check import config_references, known_document

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "redshift"
POLICIES = STACK / "policies"
REGION = "ca-central-1"
VARIABLES = frozenset({"region", "owner", "expected_account_id"})
NAME = "ecp-redshift"
SUBNET_CIDRS = {"a": "10.43.0.0/24", "b": "10.43.1.0/24", "c": "10.43.2.0/24"}

VPC, DSG = "aws_vpc.redshift", "aws_default_security_group.redshift"
SG, RT = "aws_security_group.redshift", "aws_route_table.private"
COPY, CALLER = "aws_iam_role.copy", "aws_iam_role.caller"
COPY_POLICY, CALLER_POLICY = "aws_iam_role_policy.copy", "aws_iam_role_policy.caller"
SLR = "aws_iam_service_linked_role.redshift"
NS = "aws_redshiftserverless_namespace.this"
WG = "aws_redshiftserverless_workgroup.this"
LIMIT = "aws_redshiftserverless_usage_limit.this"
SUBNET = "aws_subnet.private"
ASSOCIATION = "aws_route_table_association.private"
SUBNETS = [f'{SUBNET}["{key}"]' for key in SUBNET_CIDRS]
ASSOCIATIONS = [f'{ASSOCIATION}["{key}"]' for key in SUBNET_CIDRS]
CREATES = frozenset(
    {VPC, DSG, SG, RT, COPY, CALLER, COPY_POLICY, CALLER_POLICY, SLR, NS, WG, LIMIT}
    | set(SUBNETS)
    | set(ASSOCIATIONS)
)
# `destroy` removes everything but the service-linked role, which is shared account-wide and kept.
DESTROYS = CREATES - {SLR}
DATA = frozenset(
    {
        "data.aws_caller_identity.current",
        "data.aws_organizations_organization.this",
        "data.aws_availability_zones.available",
    }
)
ZONES_DATA = "data.aws_availability_zones.available"
TRUST = {COPY: "copy-trust", CALLER: "data-api-trust"}
POLICY = {COPY_POLICY: "copy-policy", CALLER_POLICY: "data-api-policy"}

SETTINGS = [
    (VPC, ("cidr_block",), "10.43.0.0/16"),
    (VPC, ("enable_dns_support",), True),
    (VPC, ("enable_dns_hostnames",), True),
    (SG, ("name",), NAME),
    (COPY, ("name",), "ecp-redshift-copy"),
    (CALLER, ("name",), "ecp-redshift-data-api"),
    (COPY_POLICY, ("name",), "ecp-redshift-copy"),
    (CALLER_POLICY, ("name",), "ecp-redshift-data-api"),
    (SLR, ("aws_service_name",), "redshift.amazonaws.com"),
    (NS, ("namespace_name",), NAME),
    (NS, ("db_name",), "ecp"),
    (WG, ("workgroup_name",), NAME),
    (WG, ("base_capacity",), 8),
    (WG, ("max_capacity",), 8),
    (WG, ("publicly_accessible",), False),
    (WG, ("enhanced_vpc_routing",), False),
    (WG, ("tags_all", "stack"), "redshift"),
    (LIMIT, ("usage_type",), "serverless-compute"),
    (LIMIT, ("period",), "daily"),
    (LIMIT, ("amount",), 4),
    (LIMIT, ("breach_action",), "deactivate"),
    *[(a, ("cidr_block",), c) for a, c in zip(SUBNETS, SUBNET_CIDRS.values(), strict=True)],
    *[(address, ("map_public_ip_on_launch",), False) for address in SUBNETS],
]  # fmt: skip
# What the service reports for the attributes it fills in (update mode only).
READ_BACK_ADMIN_NAME = "admin"
READ_BACK_KEY = "AWS_OWNED_KMS_KEY"
ROLE_OWN_POLICY = {COPY: ("ecp-redshift-copy", "copy-policy"),
                   CALLER: ("ecp-redshift-data-api", "data-api-policy")}  # fmt: skip
# Attributes that must hold no rule or route, or be left to the provider (unknown at plan time).
EMPTY_OR_UNKNOWN = [
    (RT, "route"),
    (SG, "ingress"),
    (SG, "egress"),
    (DSG, "ingress"),
    (DSG, "egress"),
]
# IAM only: none of these may carry a value. admin_username is the service's to choose (it is
# computed), never ours; the others are credentials, a managed secret, a key or exports.
NAMESPACE_MUST_BE_UNSET = (
    "admin_username",
    "admin_user_password",
    "admin_user_password_wo",
    "admin_user_password_wo_version",
    "admin_password_secret_kms_key_id",
    "default_iam_role_arn",
    "kms_key_id",
    "log_exports",
)


EACH = [frozenset({"each.value.id", "each.value"}), frozenset({"each.value.id"})]


def own(resource: str, attribute: str) -> list[frozenset[str]]:
    """The reference sets Terraform lists for `resource.attribute`: with or without the resource."""
    return [
        frozenset({f"{resource}.{attribute}", resource}),
        frozenset({f"{resource}.{attribute}"}),
    ]


def wiring(account: str) -> dict[str, list[tuple[str, Any, list[frozenset[str]]]]]:
    """Config address -> (attribute, the exact known value or None, the reference sets that are
    this stack's own resource). A known value must equal `want`; None means it can never be known
    at plan time (an id or ARN of a resource created in the same plan), so a known one fails."""
    vpc = own(VPC, "id")
    return {
        "aws_default_security_group.redshift": [("vpc_id", None, vpc)],
        SUBNET: [("vpc_id", None, vpc)],
        RT: [("vpc_id", None, vpc)],
        SG: [("vpc_id", None, vpc)],
        ASSOCIATION: [
            ("subnet_id", None, EACH),
            ("route_table_id", None, own(RT, "id")),
        ],
        COPY_POLICY: [("role", "ecp-redshift-copy", own(COPY, "id"))],
        CALLER_POLICY: [("role", "ecp-redshift-data-api", own(CALLER, "id"))],
        NS: [("iam_roles", [f"arn:aws:iam::{account}:role/ecp-redshift-copy"], own(COPY, "arn"))],
        WG: [
            ("namespace_name", NAME, own(NS, "namespace_name")),
            ("subnet_ids", None, [frozenset({SUBNET})]),
            ("security_group_ids", None, own(SG, "id")),
        ],
        LIMIT: [("resource_arn", None, own(WG, "arn"))],
    }  # fmt: skip


def render(name: str, account: str) -> Any:
    return policy_templates.render(name, {"account_id": account, "region": REGION}, POLICIES)


def _json_or_none(text: Any) -> Any:
    try:
        return json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        return None


def config_address(address: str) -> str:
    """`aws_subnet.private["a"]` -> `aws_subnet.private`: the configuration has no instances."""
    return address.split("[", 1)[0]


def config_resource(plan: dict[str, Any], address: str) -> dict[str, Any]:
    root = (plan.get("configuration") or {}).get("root_module") or {}
    return next((r for r in root.get("resources", []) if r.get("address") == address), {})


def is_unknown(value: Any) -> bool:
    if isinstance(value, list):
        return any(is_unknown(v) for v in value)
    return value is True


def check_wiring(
    plan: dict[str, Any], address: str, change: dict[str, Any], account: str, mode: str = "create"
) -> list[str]:
    """Create mode: a known value must be exactly the reviewed one, and an unknown one must come
    from this stack's own resource. Update mode: the ids are known by now, so the configuration's
    references are checked always, and a value the plan can name (a role or namespace name) too."""
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    problems = []
    for attribute, want, allowed in wiring(account).get(config_address(address), []):
        if mode == "update" or is_unknown(unknown.get(attribute)):
            refs = config_references(plan, config_address(address), attribute)
            if refs is None or frozenset(refs) not in allowed:
                problems.append(
                    f"{address}: {attribute} is unknown and not from this stack's own resource"
                    if mode == "create"
                    else f"{address}: {attribute} is not from this stack's own resource"
                )
            if mode == "update" and want is not None and after.get(attribute) != want:
                problems.append(f"{address}: {attribute} is not the reviewed value")
        elif want is None or after.get(attribute) != want:
            problems.append(f"{address}: {attribute} is not the reviewed value")
    return problems


def check_create(
    address: str, change: dict[str, Any], account: str, plan: dict[str, Any], mode: str = "create"
) -> list[str]:
    after = change.get("after") or {}
    problems = [
        f"{address}: {'.'.join(map(str, path))} is not the reviewed value"
        for a, path, want in SETTINGS
        if a == address and dig(after, path) != want
    ]
    problems += check_wiring(plan, address, change, account, mode)
    for a, attribute in EMPTY_OR_UNKNOWN:  # absent (unknown at plan time) or empty, never a rule
        if a == address and after.get(attribute) not in (None, []):
            problems.append(f"{address}: {attribute} must hold no rule or route")
    if address in TRUST:
        if (
            after.get("permissions_boundary")
            != f"arn:aws:iam::{account}:policy/ecp-workload-boundary"
        ):
            problems.append(f"{address}: permissions_boundary is not the workload boundary")
        if known_document(change, "assume_role_policy") != render(TRUST[address], account):
            problems.append(
                f"{address}: assume_role_policy is not the reviewed template, known at plan time"
            )
    if address in POLICY and known_document(change, "policy") != render(POLICY[address], account):
        problems.append(f"{address}: policy is not the reviewed template, known at plan time")
    if address == NS:
        read_back = (
            {"admin_username": READ_BACK_ADMIN_NAME, "kms_key_id": READ_BACK_KEY}
            if mode == "update"
            else {}
        )
        for attribute in NAMESPACE_MUST_BE_UNSET:
            value = after.get(attribute)
            if value not in (None, [], "") and value != read_back.get(attribute, object()):
                problems.append(f"{address}: {attribute} must not be set (IAM only)")
        if after.get("manage_admin_password") not in (None, False):
            problems.append(f"{address}: manage_admin_password must not be set (IAM only)")
    if mode == "update" and address in ROLE_OWN_POLICY:
        name, template_name = ROLE_OWN_POLICY[address]
        inline = after.get("inline_policy") or []
        if not (
            len(inline) == 1
            and inline[0].get("name") == name
            and _json_or_none(inline[0].get("policy")) == render(template_name, account)
        ):
            problems.append(
                f"{address}: inline_policy is not exactly the role's own reviewed policy"
            )
    waits_for = config_resource(plan, config_address(address)).get("depends_on") or []
    if address in (NS, WG) and SLR not in waits_for:
        problems.append(f"{address}: does not wait for the service-linked role")
    if address == ASSOCIATIONS[0]:
        refs = (config_resource(plan, ASSOCIATION).get("for_each_expression") or {}).get(
            "references"
        )
        if frozenset(refs or []) != {SUBNET}:
            problems.append(f"{ASSOCIATION}: for_each is not exactly {SUBNET}")
    return problems


def check_zones(plan: dict[str, Any], changes: dict[str, dict[str, Any]]) -> list[str]:
    """Three different zones, and the first three the account reports, one for each key."""
    reported = (prior(plan, ZONES_DATA) or {}).get("names") or []
    want = dict(zip(SUBNET_CIDRS, sorted(reported)[:3], strict=False))  # fewer: fails below
    problems = []
    got = {}
    for key, address in zip(SUBNET_CIDRS, SUBNETS, strict=True):
        zone = ((changes.get(address) or {}).get("after") or {}).get("availability_zone")
        got[key] = zone
        if zone != want.get(key):
            problems.append(f"{address}: availability_zone is not the reviewed value")
    if len(set(got.values())) != 3:
        problems.append("the subnets are not in three different Availability Zones")
    if len(want) != 3 or set(got.values()) != set(want.values()):
        problems.append("the subnets are not in the Availability Zones the account reports")
    return problems


def check_common(plan: dict[str, Any], mode: str = "create") -> tuple[list[str], str | None]:
    problems: list[str] = []
    if plan.get("errored"):
        problems.append("the plan errored")
    if plan.get("complete") is False:
        problems.append("the plan is incomplete")
    for key in ("deferred_changes", "resource_drift"):
        for item in plan.get(key) or []:
            problems.append(f"{key}: {item.get('address', '?')}")
    if variable(plan, "region") != REGION:
        problems.append(f"region must be {REGION}")
    for name in sorted(set(plan.get("variables") or {}) - VARIABLES):
        problems.append(f"unexpected variable: {name}")
    account = (prior(plan, "data.aws_caller_identity.current") or {}).get("account_id")
    master = (prior(plan, "data.aws_organizations_organization.this") or {}).get(
        "master_account_id"
    )
    if not account or account != variable(plan, "expected_account_id"):
        problems.append("the caller is not expected_account_id")
    if not master or master == account:
        problems.append("this is the organization's management account, not a member")
    resources = (plan.get("prior_state") or {}).get("values", {}).get("root_module", {})
    owned = {r["address"] for r in resources.get("resources", [])} - DATA
    if mode == "create" and owned:
        problems.append("the state is not empty: this stack starts from no managed resources")
    if mode == "update" and owned != CREATES:
        problems.append(
            "the state is not the applied stack: it must hold exactly the reviewed ones"
        )
    if mode == "destroy" and owned != DESTROYS:
        problems.append(
            "the state is not the applied stack without the kept service-linked role: it must "
            f"hold exactly the other {len(DESTROYS)} reviewed resources ({SLR} is removed from "
            "state first)"
        )
    for name, out in (plan.get("output_changes") or {}).items():
        if out["actions"] not in UNCHANGED:
            problems.append(f"unexpected output change: {'+'.join(out['actions'])} {name}")
    root = (plan.get("configuration") or {}).get("root_module") or {}
    for call_name in sorted(root.get("module_calls") or {}):
        problems.append(f"module call not allowed: {call_name}")
    return problems, account


def check_source(stack_dir: Path) -> list[str]:
    """Over exactly the files Terraform loads: no override file, no JSON configuration and no
    module block. Run from the clean checkout of the saved plan."""
    import hcl2
    from hcl2.utils import SerializationOptions
    from phase1b_plan_check import config_ext, config_files, is_override

    problems = []
    for p in config_files(stack_dir):
        if is_override(p.name):
            problems.append(f"override files are not allowed in the stack: {p.name}")
            continue
        if config_ext(p.name) == ".tf.json":
            problems.append(f"JSON configuration is not allowed in the stack: {p.name}")
            continue
        try:
            with p.open() as fh:
                doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
        except Exception:  # noqa: BLE001 - any parse failure means the source is unverified
            problems.append(f"cannot read the stack source: {p.name}")
            continue
        for block in doc.get("module", []):
            for call in block:
                problems.append(f"module block not allowed: {call.strip(chr(34))} ({p.name})")
    return problems


def check(
    plan: dict[str, Any], stack_dir: Path | None = None, mode: str = "create"
) -> tuple[list[str], list[str]]:
    """(problems, report) for a saved plan of infra/redshift: `create` from an empty state, or
    `update` (18 no-ops, the provider's read-back accepted) on the applied stack, or `destroy`
    (the 17 deletes of everything but the kept service-linked role)."""
    if mode not in ("create", "update", "destroy"):
        return [f"unknown change mode {mode}"], []
    problems, account = check_common(plan, mode)
    problems += check_source(stack_dir or STACK)
    if mode == "destroy":
        return check_destroy(plan, problems)
    report: list[str] = []
    want = "create" if mode == "create" else "no-op"
    seen: set[str] = set()
    changes: dict[str, dict[str, Any]] = {}
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        report.append(f"{'+'.join(actions):8} {address}")
        if address not in CREATES or actions != [want] or change.get("importing") is not None:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
            continue
        seen.add(address)
        changes[address] = change
        if account:
            problems += check_create(address, change, account, plan, mode)
    for address in sorted(CREATES - seen):
        problems.append(f"missing change: {want} {address}")
    problems += check_zones(plan, changes)
    adds = len(CREATES) if mode == "create" else 0
    report.append(
        f"summary: {adds} to add, 0 to change, 0 to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def check_destroy(plan: dict[str, Any], problems: list[str]) -> tuple[list[str], list[str]]:
    """Exactly the DESTROYS addresses as plain deletes, nothing from another stack, and never the
    kept service-linked role (a delete of it, or any other action, is refused)."""
    report: list[str] = []
    seen: set[str] = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        report.append(f"{'+'.join(actions):8} {address}")
        if address == SLR:
            problems.append(
                f"{address}: the service-linked role is kept; remove it from state before "
                "planning the destroy"
            )
        elif address not in DESTROYS or actions != ["delete"] or change.get("importing"):
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
        else:
            seen.add(address)
    for address in sorted(DESTROYS - seen):
        problems.append(f"missing change: delete {address}")
    report.append(
        f"summary: 0 to add, 0 to change, {len(DESTROYS)} to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redshift_plan_check.py")
    parser.add_argument("--stack-dir", type=Path, default=STACK)
    parser.add_argument("--change", choices=("create", "update", "destroy"), default="create")
    parser.add_argument("plan", type=Path)
    args = parser.parse_args(argv)
    problems, report = check(json.loads(args.plan.read_text()), args.stack_dir, args.change)
    print("\n".join([*report, *(f"FAIL  {p}" for p in problems)] or ["(empty plan)"]))
    if not problems:
        print("OK    the reviewed shape")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
