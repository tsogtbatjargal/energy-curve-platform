"""Pre-apply check of a saved bootstrap plan for a reviewed IAM change.

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/bootstrap_plan_check.py [--change r2|no-cross-account] <plan.json>

`no-cross-account` (ADR-0021, before phase 1b): update `aws_iam_role_policy.gha_deploy_iam` with
only `policy` changing, to the deploy-iam template, which now denies the deploy role
sts:AssumeRole, sts:TagSession and sts:SetSourceIdentity on roles outside its own account.
Nothing else changes.

`r2` (the default; ADR-0018) exits 0 only when the plan does exactly what was reviewed:
- resource changes: create `aws_iam_policy.workload_boundary`, named exactly
  `ecp-workload-boundary` at path `/` (the ARN the deploy policy and workload stacks name), and
  update `aws_iam_role_policy.gha_deploy_iam` with only `policy` changing. Nothing else: no other
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
CHANGES = {  # change -> (address -> (actions, attributes allowed to change; None = new), outputs)
    "r2": (
        {
            "aws_iam_policy.workload_boundary": (["create"], None),
            "aws_iam_role_policy.gha_deploy_iam": (["update"], {"policy"}),
        },
        {"workload_boundary_arn": ["create"]},
    ),
    "no-cross-account": ({"aws_iam_role_policy.gha_deploy_iam": (["update"], {"policy"})}, {}),
}
EXPECTED, EXPECTED_OUTPUTS = CHANGES["r2"]
BOUNDARY = "aws_iam_policy.workload_boundary"
BOUNDARY_NAME, BOUNDARY_PATH = "ecp-workload-boundary", "/"
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
    if not (identity and deploy and plan_role):
        raise ValueError("prior state lacks the caller identity or CI roles")
    account = identity["account_id"]
    if bucket is None:  # owned by infra/org since ADR-0021 phase 1a; bootstrap names it by ARN
        region = ((plan.get("variables") or {}).get("region") or {}).get("value")
        if not region:
            raise ValueError("the plan lacks the region variable")
        bucket = {"arn": f"arn:aws:s3:::ecp-tfstate-{account}-{region}"}
    return {
        "account_id": account,
        "boundary_arn": f"arn:aws:iam::{account}:policy/ecp-workload-boundary",
        "deploy_role_arn": deploy["arn"],
        "plan_role_arn": plan_role["arn"],
        "state_bucket_arn": bucket["arn"],
    }


def boundary_identity(change: dict[str, Any]) -> list[str]:
    """The boundary is named by ARN everywhere (the deploy policy, workload stacks, the Rego
    rule), and its ARN is fixed by name and path; another name or path is another policy."""
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    problems = []
    if after.get("name") != BOUNDARY_NAME or unknown.get("name") or after.get("name_prefix"):
        problems.append(f"{BOUNDARY}: boundary name must be {BOUNDARY_NAME}, known at plan time")
    if after.get("path") != BOUNDARY_PATH or unknown.get("path"):
        problems.append(f"{BOUNDARY}: boundary path must be {BOUNDARY_PATH}")
    return problems


def digest(document: Any) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:16]


def check(plan: dict[str, Any], change: str = "r2") -> tuple[list[str], list[str]]:
    """(problems, report lines). Any problem means: stop, do not apply."""
    expected, expected_outputs = CHANGES[change]
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
        if address not in expected:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
            continue
        want_actions, allowed = expected[address]
        if actions != want_actions:
            problems.append(f"{address}: {'+'.join(actions)}, expected {'+'.join(want_actions)}")
        elif allowed is not None and not (attrs and attrs <= allowed):
            problems.append(f"{address}: changes {sorted(attrs)}, only {sorted(allowed)} allowed")
    if BOUNDARY in seen and BOUNDARY in expected:
        problems += boundary_identity(seen[BOUNDARY]["change"])
    for address in sorted(set(expected) - set(seen)):
        problems.append(f"missing expected change: {address}")
    for name, out in (plan.get("output_changes") or {}).items():
        if out["actions"] not in UNCHANGED and out["actions"] != expected_outputs.get(name):
            problems.append(f"unexpected output change: {'+'.join(out['actions'])} {name}")
    try:
        values = template_values(plan)
    except ValueError as exc:
        return [*problems, str(exc)], report
    for address, name in TEMPLATES.items():
        rc = seen.get(address)
        if rc is None or address not in expected:
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


# --- ADR-0021 phase 3: the member bootstrap's first apply, into ecp-workloads ------------------

BREAK_GLASS = 'aws_iam_role.break_glass["OrganizationAccountAccessRole"]'
_S3 = "aws_s3_bucket"
FIRST_APPLY_CREATES = frozenset(
    {
        f"{_S3}.member_state[0]",
        f"{_S3}_versioning.member_state[0]",
        f"{_S3}_server_side_encryption_configuration.member_state[0]",
        f"{_S3}_public_access_block.member_state[0]",
        f"{_S3}_ownership_controls.member_state[0]",
        f"{_S3}_lifecycle_configuration.member_state[0]",
        f"{_S3}_policy.member_state[0]",
        "aws_iam_openid_connect_provider.github[0]",
        "aws_iam_role.gha_plan",
        "aws_iam_role_policy_attachment.gha_plan_readonly",
        "aws_iam_role_policy.gha_plan_state",
        "aws_iam_role.gha_deploy",
        "aws_iam_role_policy_attachment.gha_deploy_poweruser",
        "aws_iam_role_policy.gha_deploy_iam",
        "aws_iam_role_policy.gha_deploy_state_read",
        BOUNDARY,
    }
)
FIRST_APPLY_OUTPUTS = frozenset(
    {"state_bucket", "gha_plan_role_arn", "gha_deploy_role_arn", "workload_boundary_arn"}
)
ORG_ONLY_TYPES = frozenset({"aws_budgets_budget", "aws_ce_cost_allocation_tag"})
BREAK_GLASS_MAY_CHANGE = {"assume_role_policy", "tags", "tags_all"}
GITHUB_OIDC_URL = "https://token.actions.githubusercontent.com"
# (address, attribute path, required value): each create's reviewed settings.
FIRST_APPLY_SETTINGS = [
    (f"{_S3}_versioning.member_state[0]", ("versioning_configuration", 0, "status"), "Enabled"),
    (f"{_S3}_server_side_encryption_configuration.member_state[0]",
     ("rule", 0, "apply_server_side_encryption_by_default", 0, "sse_algorithm"), "AES256"),
    *[(f"{_S3}_public_access_block.member_state[0]", (flag,), True)
      for flag in ("block_public_acls", "block_public_policy", "ignore_public_acls",
                   "restrict_public_buckets")],
    (f"{_S3}_ownership_controls.member_state[0]", ("rule", 0, "object_ownership"),
     "BucketOwnerEnforced"),
    (f"{_S3}_lifecycle_configuration.member_state[0]", ("rule", 0, "id"),
     "expire-noncurrent-state"),
    (f"{_S3}_lifecycle_configuration.member_state[0]", ("rule", 0, "status"), "Enabled"),
    (f"{_S3}_lifecycle_configuration.member_state[0]",
     ("rule", 0, "noncurrent_version_expiration", 0, "noncurrent_days"), 90),
    ("aws_iam_openid_connect_provider.github[0]", ("url",), GITHUB_OIDC_URL),
    ("aws_iam_openid_connect_provider.github[0]", ("client_id_list",), ["sts.amazonaws.com"]),
    ("aws_iam_role.gha_plan", ("name",), "ecp-gha-plan"),
    ("aws_iam_role.gha_plan", ("max_session_duration",), 3600),
    ("aws_iam_role.gha_plan", ("permissions_boundary",), None),
    ("aws_iam_role_policy_attachment.gha_plan_readonly", ("policy_arn",),
     "arn:aws:iam::aws:policy/ReadOnlyAccess"),
    ("aws_iam_role_policy.gha_plan_state", ("name",), "terraform-state-read"),
    ("aws_iam_role.gha_deploy", ("name",), "ecp-gha-deploy"),
    ("aws_iam_role.gha_deploy", ("max_session_duration",), 3600),
    ("aws_iam_role.gha_deploy", ("permissions_boundary",), None),
    ("aws_iam_role_policy_attachment.gha_deploy_poweruser", ("policy_arn",),
     "arn:aws:iam::aws:policy/PowerUserAccess"),
    ("aws_iam_role_policy.gha_deploy_iam", ("name",), "ecp-scoped-iam-and-state"),
    ("aws_iam_role_policy.gha_deploy_state_read", ("name",), "terraform-state-read"),
]  # fmt: skip


def dig(value: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError):
            return KeyError
    return value


def variable(plan: dict[str, Any], name: str) -> Any:
    return ((plan.get("variables") or {}).get(name) or {}).get("value")


def check_first_apply(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    """ADR-0021 phase 3: the member instance's first apply, from an empty local state in
    ecp-workloads. Exactly the 16 creates in FIRST_APPLY_CREATES, each with its reviewed
    settings, and the import of OrganizationAccountAccessRole changing only its trust policy
    (to the break-glass template) and tags. No budget and no cost allocation tag anywhere, no
    other import, no drift. The deploy policy is unknown at plan time (it names the deploy role's
    ARN); the post-apply re-plan and r2_accept.py check it."""
    from org_plan_check import already_owned

    problems: list[str] = []
    report: list[str] = []
    if plan.get("errored"):
        problems.append("the plan errored")
    if plan.get("complete") is False:
        problems.append("the plan is incomplete")
    for item in plan.get("deferred_changes") or []:
        problems.append(f"deferred_changes: {item.get('address', '?')}")
    for item in plan.get("resource_drift") or []:
        problems.append(f"resource_drift: {item.get('address', '?')}")
    if already_owned(plan):
        problems.append("the state is not empty: a first apply starts from no managed resources")

    account = (prior(plan, "data.aws_caller_identity.current") or {}).get("account_id")
    master = (prior(plan, "data.aws_organizations_organization.this") or {}).get(
        "master_account_id"
    )
    region = variable(plan, "region")
    if not account or account != variable(plan, "expected_account_id"):
        problems.append("the caller is not expected_account_id")
    if variable(plan, "member_instance") is not True:
        problems.append("member_instance must be true")
    if not master or master == account:
        problems.append("this is the organization's management account, not a member")

    root = (plan.get("configuration") or {}).get("root_module") or {}
    for res in root.get("resources") or []:
        if res.get("type") in ORG_ONLY_TYPES:
            problems.append(
                f"{res['address']}: the member bootstrap has no budget or cost allocation tag"
            )

    seen: set[str] = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions, importing = change["actions"], change.get("importing")
        if rc.get("type") in ORG_ONLY_TYPES or address.split(".")[0] in ORG_ONLY_TYPES:
            problems.append(f"{address}: the member bootstrap has no budget or cost allocation tag")
            continue
        if importing is not None and address != BREAK_GLASS:
            problems.append(f"unexpected import: {address}")
            continue
        if actions in UNCHANGED and importing is None:
            continue
        report.append(f"{'+'.join(actions):8} {address}" + ("  (import)" if importing else ""))
        if address in FIRST_APPLY_CREATES and actions == ["create"]:
            seen.add(address)
            problems += check_member_create(address, change, account, region)
        elif address == BREAK_GLASS and importing is not None and actions == ["update"]:
            seen.add(address)
            problems += check_break_glass(change, master)
        else:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
    for address in sorted(FIRST_APPLY_CREATES - seen):
        problems.append(f"missing change: create {address}")
    if BREAK_GLASS not in seen:
        problems.append(f"missing change: import and update {BREAK_GLASS}")
    for name, out in (plan.get("output_changes") or {}).items():
        if out["actions"] not in UNCHANGED and not (
            name in FIRST_APPLY_OUTPUTS and out["actions"] == ["create"]
        ):
            problems.append(f"unexpected output change: {'+'.join(out['actions'])} {name}")
    report.append(
        "summary: 1 to import, 16 to add, 1 to change (the break-glass trust), 0 to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def check_member_create(
    address: str, change: dict[str, Any], account: str | None, region: str | None
) -> list[str]:
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    problems = []
    if address == f"{_S3}.member_state[0]":
        if not account or after.get("bucket") != f"ecp-tfstate-{account}-{region}":
            problems.append(f"{address}: bucket must be named for this account")
        if after.get("force_destroy"):
            problems.append(f"{address}: force_destroy must be false")
    for target, path, want in FIRST_APPLY_SETTINGS:
        if target == address and dig(after, path) != want:
            problems.append(f"{address}: not as reviewed ({path[0]})")
    if address == BOUNDARY:
        problems += boundary_identity(change)
        values = {"account_id": account or "", "state_bucket_arn":
                  f"arn:aws:s3:::ecp-tfstate-{account}-{region}"}  # fmt: skip
        if unknown.get("policy") or json.loads(after.get("policy") or "null") != (
            policy_templates.render("workload-boundary", values)
        ):
            problems.append(f"{BOUNDARY}: policy differs from the workload-boundary template")
    if address == "aws_iam_role_policy.gha_deploy_iam" and not unknown.get("policy"):
        problems.append(f"{address}: a known policy must equal the deploy-iam template")
    return problems


def check_break_glass(change: dict[str, Any], master: str | None) -> list[str]:
    before, after = change.get("before") or {}, change.get("after") or {}
    problems = []
    if (change.get("importing") or {}).get("id") != "OrganizationAccountAccessRole":
        problems.append(f"{BREAK_GLASS}: must import OrganizationAccountAccessRole")
    attrs = changed_attributes(change)
    if not ("assume_role_policy" in attrs and attrs <= BREAK_GLASS_MAY_CHANGE):
        problems.append(f"{BREAK_GLASS}: may change only assume_role_policy and tags")
    want = policy_templates.render("break-glass-trust", {"management_account_id": master or ""})
    if not master or json.loads(after.get("assume_role_policy") or "null") != want:
        problems.append(
            f"{BREAK_GLASS}: trust policy differs from policies/break-glass-trust.json.tftpl"
        )
    only = f"{BREAK_GLASS}: may change only assume_role_policy and tags"
    if only not in problems and (
        after.get("max_session_duration") != 3600 or before.get("name") != after.get("name")
    ):
        problems.append(only)
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    change = "r2"
    if len(args) == 3 and args[0] == "--change" and args[1] in (*CHANGES, "first-apply"):
        change, args = args[1], args[2:]
    if len(args) != 1:
        print(f"usage: bootstrap_plan_check.py [--change {'|'.join([*CHANGES, 'first-apply'])}]"
              " <plan.json>", file=sys.stderr)  # fmt: skip
        return 2
    plan = json.loads(Path(args[0]).read_text())
    problems, report = check_first_apply(plan) if change == "first-apply" else check(plan, change)
    print("\n".join(report))
    for p in problems:
        print(f"STOP  {p}")
    print(f"OK: exactly the reviewed {change} change" if not problems
          else "STOP: do not apply this plan")  # fmt: skip
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
