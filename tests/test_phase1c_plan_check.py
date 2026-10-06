"""ADR-0021 phase 1c: the saved infra/org plan must be exactly the reviewed change.

The fixture is phase 1b as applied on 2026-10-06 (the account in Workloads, the SCPs attached),
with IAM trusted access enabled out-of-band, and the 1c plan: the AssumeRoot scoping policy on
AdministratorAccess, ecp-readonly with ReadOnlyAccess, two assignments, root access, and the
budget's filter. Plan JSON spells out every block, empty ones included, as Terraform 1.15.8 does.
"""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import phase1c_plan_check as p1c
import pytest
import test_phase1b_account_only as recovered
import test_phase1b_plan_check as full

ACCOUNT_ID = "210987654321"
OTHER_ACCOUNT_ID = "123456789012"
INSTANCE = "arn:aws:sso:::instance/ssoins-0123456789abcdef"
ADMIN_SET_ARN = f"{INSTANCE}/ps-0123456789abcdef".replace(":::instance/", ":::permissionSet/")
USER_ID, USER_NAME = "9067-user-0001", "admin-user"


def leaf(**kind: list[dict]) -> dict:
    """One filter_expression block as the plan shows it: every nested block present."""
    blocks = {"and": [], "cost_categories": [], "dimensions": [], "not": [], "or": [], "tags": []}
    return {**blocks, **kind}


def budget_filter(account_id: str = ACCOUNT_ID) -> list[dict]:
    dims = {"key": "LINKED_ACCOUNT", "values": [account_id], "match_options": None}
    tag = {"key": "user:project", "values": ["energy-curve-platform"], "match_options": None}
    return [leaf(**{"or": [leaf(dimensions=[dims]), leaf(tags=[tag])]})]


BUDGET_BEFORE = {
    "name": "energy-curve-platform", "limit_amount": "40.0", "time_unit": "MONTHLY",
    "cost_filter": [{"name": "TagKeyValue", "values": ["user:project$energy-curve-platform"]}],
    "cost_types": [{"include_tax": True, "use_blended": False}],
    "filter_expression": [], "metrics": None,
}  # fmt: skip
BUDGET_AFTER = {**BUDGET_BEFORE, "cost_filter": [], "cost_types": [],
                "filter_expression": budget_filter(), "metrics": ["UnblendedCost"]}  # fmt: skip


IAM_SSO = ["iam.amazonaws.com", "sso.amazonaws.com"]


def principals_drift(before: list[str], after: list[str], actions: list[str] | None = None,
                     address: str = recovered.p1b.ORG, **other: Any) -> dict:  # fmt: skip
    """Refresh drift on the organization, shaped like the disposable probe of 2026-10-06
    (Terraform 1.15.8, provider 6.67.0, local moto): an ignore_changes attribute changed
    out-of-band is still reported, as an update, while the resource is planned no-op."""
    org = {"id": full.ORG_ID, "feature_set": "ALL",
           "enabled_policy_types": ["SERVICE_CONTROL_POLICY"]}  # fmt: skip
    return {"address": address, "change": {
        "actions": actions or ["update"],
        "before": {**org, "aws_service_access_principals": before},
        "after": {**org, "aws_service_access_principals": after, **other}}}  # fmt: skip


def scoping(account_id: str = ACCOUNT_ID) -> str:
    return json.dumps(p1c.assume_root_policy(account_id))


@pytest.fixture
def plan() -> dict:
    p = recovered.plan.__wrapped__()
    account = {"id": ACCOUNT_ID, "name": "ecp-workloads", "parent_id": full.OU_ID,
               "status": "ACTIVE"}  # fmt: skip
    resources = p["prior_state"]["values"]["root_module"]["resources"]
    # After the out-of-band call, refresh reads [iam, sso] into the data source and the
    # organization; the organization ignores the attribute, so it is planned no-op, with drift.
    for r in resources:
        if r["address"] in (p1c.ORG_DATA, recovered.p1b.ORG):
            r["values"]["aws_service_access_principals"] = list(IAM_SSO)
    org_change = full.change(p, recovered.p1b.ORG)
    for side in ("before", "after"):
        org_change[side] = {**org_change[side], "aws_service_access_principals": list(IAM_SSO)}
    p["resource_drift"] = [principals_drift(["sso.amazonaws.com"], list(IAM_SSO))]
    resources += [
        {"address": p1c.ACCOUNT, "mode": "managed", "values": account},
        {"address": p1c.BUDGET, "mode": "managed", "values": BUDGET_BEFORE},
        {"address": p1c.SSO, "mode": "data",
         "values": {"arns": [INSTANCE], "identity_store_ids": ["d-0123456789"]}},
        {"address": p1c.ADMIN_SET, "mode": "data",
         "values": {"arn": ADMIN_SET_ARN, "name": "AdministratorAccess"}},
        {"address": p1c.USER, "mode": "data",
         "values": {"user_id": USER_ID, "user_name": USER_NAME}},
    ]  # fmt: skip
    p["resource_changes"] = [r for r in p["resource_changes"]
                             if r["address"] not in (p1c.ACCOUNT, p1c.BUDGET)]  # fmt: skip
    assignment = {"instance_arn": INSTANCE, "principal_id": USER_ID, "principal_type": "USER",
                  "target_id": ACCOUNT_ID, "target_type": "AWS_ACCOUNT"}  # fmt: skip
    p["resource_changes"] += [
        full.rc(p1c.ACCOUNT, ["no-op"], account, account),
        full.rc(p1c.BUDGET, ["update"], BUDGET_BEFORE, copy.deepcopy(BUDGET_AFTER)),
        full.rc(p1c.INLINE, ["create"], None,
                {"instance_arn": INSTANCE, "permission_set_arn": ADMIN_SET_ARN,
                 "inline_policy": scoping()}, after_unknown={"id": True}),
        full.rc(p1c.READONLY, ["create"], None,
                {"instance_arn": INSTANCE, "name": "ecp-readonly", "session_duration": "PT1H",
                 "relay_state": None}, after_unknown={"arn": True, "id": True}),
        full.rc(p1c.READONLY_POLICY, ["create"], None,
                {"instance_arn": INSTANCE, "managed_policy_arn": p1c.READ_ONLY_ACCESS},
                after_unknown={"id": True, "permission_set_arn": True}),
        full.rc(p1c.ADMIN_ASSIGNMENT, ["create"], None,
                {**assignment, "permission_set_arn": ADMIN_SET_ARN}, after_unknown={"id": True}),
        full.rc(p1c.READONLY_ASSIGNMENT, ["create"], None, dict(assignment),
                after_unknown={"id": True, "permission_set_arn": True}),
        full.rc(p1c.FEATURES, ["create"], None,
                {"enabled_features": ["RootSessions", "RootCredentialsManagement"]},
                after_unknown={"id": True}),
    ]  # fmt: skip
    account_ref = [f"{p1c.ACCOUNT}.id", p1c.ACCOUNT]
    readonly_ref = [f"{p1c.READONLY}.arn", p1c.READONLY]
    p["configuration"]["root_module"]["resources"] += [
        {"address": p1c.INLINE, "expressions": {"inline_policy": {"references": account_ref}}},
        {"address": p1c.READONLY_POLICY,
         "expressions": {"permission_set_arn": {"references": readonly_ref}}},
        {"address": p1c.ADMIN_ASSIGNMENT, "depends_on": [p1c.INLINE],
         "expressions": {"target_id": {"references": account_ref}}},
        {"address": p1c.READONLY_ASSIGNMENT, "depends_on": [p1c.READONLY_POLICY],
         "expressions": {"target_id": {"references": account_ref},
                         "permission_set_arn": {"references": readonly_ref}}},
        {"address": p1c.FEATURES, "depends_on": [p1c.INLINE]},
        {"address": p1c.BUDGET, "expressions": {"filter_expression": [
            {"or": [{"dimensions": [{"values": {"references": account_ref}}]}]}]}},
    ]  # fmt: skip
    p["variables"]["identity_center_user_name"] = {"value": USER_NAME}
    p["output_changes"] = {"workloads_ou_id": {"actions": ["no-op"]},
                           "workload_account_id": {"actions": ["no-op"]}}  # fmt: skip
    return p


def change(plan: dict, address: str) -> dict:
    return full.change(plan, address)


def after(plan: dict, address: str) -> dict:
    return change(plan, address)["after"]


def config(plan: dict, address: str) -> dict:
    return next(r for r in plan["configuration"]["root_module"]["resources"]
                if r["address"] == address)  # fmt: skip


def problems(plan: dict, stack_dir: Path | None = None) -> list[str]:
    return p1c.check(*((plan,) if stack_dir is None else (plan, stack_dir)))[0]


def test_the_reviewed_plan_passes(plan: dict) -> None:
    found, report = p1c.check(plan)
    assert found == []
    assert sorted(report[:-1]) == sorted(
        [f"update   {p1c.BUDGET}", *[f"create   {a}" for a in p1c.CREATES]]
    )
    assert report[-1].startswith("summary: 0 to import, 6 to add")
    assert not any(ACCOUNT_ID in line or USER_ID in line for line in report)


def test_the_phase_1b_checks_refuse_this_plan(plan: dict) -> None:
    assert recovered.problems(plan)


# --- the AssumeRoot scoping policy -------------------------------------------------------------


@pytest.mark.parametrize("edit", ["other account", "a sixth task", "one statement", "allow"])
def test_a_scoping_policy_unlike_the_template_stops(plan: dict, edit: str) -> None:
    doc = p1c.assume_root_policy(ACCOUNT_ID)
    if edit == "other account":
        doc = p1c.assume_root_policy(OTHER_ACCOUNT_ID)
    elif edit == "a sixth task":
        doc["Statement"][1]["Condition"]["ArnNotEquals"]["sts:TaskPolicyArn"].append(
            "arn:aws:iam::aws:policy/root-task/Other"
        )
    elif edit == "one statement":
        doc["Statement"] = doc["Statement"][:1]
    else:
        doc["Statement"][0]["Effect"] = "Allow"
    after(plan, p1c.INLINE)["inline_policy"] = json.dumps(doc)
    assert problems(plan) == [f"{p1c.INLINE}: differs from policies/admin-assume-root.json.tftpl"]


def test_a_scoping_policy_unknown_at_plan_time_stops(plan: dict) -> None:
    after(plan, p1c.INLINE)["inline_policy"] = None
    change(plan, p1c.INLINE)["after_unknown"]["inline_policy"] = True
    assert problems(plan) == [f"{p1c.INLINE}: differs from policies/admin-assume-root.json.tftpl"]


def test_the_account_id_must_come_from_the_account_resource(plan: dict) -> None:
    config(plan, p1c.INLINE)["expressions"]["inline_policy"]["references"] = []
    assert problems(plan) == [f"{p1c.INLINE}: the account ID must come from {p1c.ACCOUNT}"]


def test_the_scoping_policy_on_another_permission_set_stops(plan: dict) -> None:
    after(plan, p1c.INLINE)["permission_set_arn"] = ADMIN_SET_ARN + "x"
    assert problems(plan) == [f"{p1c.INLINE}: must be on the AdministratorAccess permission set"]


def test_another_identity_center_instance_stops(plan: dict) -> None:
    after(plan, p1c.INLINE)["instance_arn"] = INSTANCE + "x"
    assert problems(plan) == [f"{p1c.INLINE}: must use the Identity Center instance"]


@pytest.mark.parametrize("name", ["main.tf", "policies/other.json"])
def test_a_12_digit_literal_in_the_source_stops(plan: dict, tmp_path: Path, name: str) -> None:
    stack = tmp_path / "org"
    shutil.copytree(p1c.STACK, stack, ignore=shutil.ignore_patterns(".terraform", "*.tfvars",
                                                                   "backend.hcl"))  # fmt: skip
    (stack / name).write_text(f'locals {{ id = "{OTHER_ACCOUNT_ID}" }}\n')
    assert problems(plan, stack) == [f"a 12-digit literal in the stack source: {Path(name).name}"]


def test_the_stack_source_has_no_account_id_literal() -> None:
    assert p1c.check_literals(p1c.STACK) == []


# --- ecp-readonly and the assignments ----------------------------------------------------------


@pytest.mark.parametrize(("key", "value"), [("name", "ecp-admin"), ("session_duration", "PT12H")])
def test_a_readonly_set_unlike_the_reviewed_one_stops(plan: dict, key: str, value: str) -> None:
    after(plan, p1c.READONLY)[key] = value
    assert problems(plan) == [f"{p1c.READONLY}: must be ecp-readonly with PT1H sessions"]


def test_a_relay_state_stops(plan: dict) -> None:
    after(plan, p1c.READONLY)["relay_state"] = "https://example.com"
    assert problems(plan) == [f"{p1c.READONLY}: no relay state"]


def test_a_managed_policy_other_than_read_only_access_stops(plan: dict) -> None:
    after(plan, p1c.READONLY_POLICY)["managed_policy_arn"] = (
        "arn:aws:iam::aws:policy/AdministratorAccess"
    )
    assert problems(plan) == [f"{p1c.READONLY_POLICY}: must attach ReadOnlyAccess"]


def test_read_only_access_on_the_admin_set_stops(plan: dict) -> None:
    after(plan, p1c.READONLY_POLICY)["permission_set_arn"] = ADMIN_SET_ARN
    change(plan, p1c.READONLY_POLICY)["after_unknown"]["permission_set_arn"] = False
    assert problems(plan) == [f"{p1c.READONLY_POLICY}: must be on {p1c.READONLY}"]


@pytest.mark.parametrize("assignment", [p1c.ADMIN_ASSIGNMENT, p1c.READONLY_ASSIGNMENT])
@pytest.mark.parametrize(("key", "value"), [("principal_type", "GROUP"),
                                            ("principal_id", "9067-user-0002")])  # fmt: skip
def test_an_assignment_to_another_principal_stops(plan: dict, assignment: str, key: str,
                                                  value: str) -> None:  # fmt: skip
    after(plan, assignment)[key] = value
    assert problems(plan) == [f"{assignment}: must be the configured Identity Center user"]


@pytest.mark.parametrize("assignment", [p1c.ADMIN_ASSIGNMENT, p1c.READONLY_ASSIGNMENT])
@pytest.mark.parametrize(("key", "value"), [("target_id", OTHER_ACCOUNT_ID),
                                            ("target_type", "OU")])  # fmt: skip
def test_an_assignment_to_another_target_stops(plan: dict, assignment: str, key: str,
                                               value: str) -> None:  # fmt: skip
    after(plan, assignment)[key] = value
    assert problems(plan) == [f"{assignment}: must target the workload account only"]


def test_an_assignment_target_that_is_not_the_account_reference_stops(plan: dict) -> None:
    config(plan, p1c.ADMIN_ASSIGNMENT)["expressions"]["target_id"]["references"] = []
    assert problems(plan) == [f"{p1c.ADMIN_ASSIGNMENT}: must target the workload account only"]


def test_the_admin_assignment_must_be_administrator_access(plan: dict) -> None:
    after(plan, p1c.ADMIN_ASSIGNMENT)["permission_set_arn"] = ADMIN_SET_ARN + "x"
    assert problems(plan) == [f"{p1c.ADMIN_ASSIGNMENT}: must assign AdministratorAccess"]


def test_the_readonly_assignment_must_be_ecp_readonly(plan: dict) -> None:
    after(plan, p1c.READONLY_ASSIGNMENT)["permission_set_arn"] = ADMIN_SET_ARN
    change(plan, p1c.READONLY_ASSIGNMENT)["after_unknown"]["permission_set_arn"] = False
    assert problems(plan) == [f"{p1c.READONLY_ASSIGNMENT}: must assign {p1c.READONLY}"]


# --- root access and the order -----------------------------------------------------------------


@pytest.mark.parametrize("features", [["RootSessions"], ["RootCredentialsManagement"], []])
def test_root_access_must_enable_exactly_both_features(plan: dict, features: list[str]) -> None:
    after(plan, p1c.FEATURES)["enabled_features"] = features
    assert problems(plan) == [
        f"{p1c.FEATURES}: must enable exactly RootCredentialsManagement and RootSessions"
    ]


@pytest.mark.parametrize(("address", "prerequisite"), sorted(p1c.DEPENDS_ON.items()))
def test_each_dependency_is_required(plan: dict, address: str, prerequisite: str) -> None:
    config(plan, address)["depends_on"] = []
    assert problems(plan) == [f"{address}: must depend on {prerequisite}"]


def test_root_access_depending_on_something_else_stops(plan: dict) -> None:
    config(plan, p1c.FEATURES)["depends_on"] = [p1c.READONLY]
    assert problems(plan) == [f"{p1c.FEATURES}: must depend on {p1c.INLINE}"]


def test_the_source_declares_the_order() -> None:
    import hcl2

    deps = {}
    for tf in (p1c.STACK / "identity_center.tf", p1c.STACK / "root_access.tf"):
        for block in hcl2.loads(tf.read_text())["resource"]:
            for type_, named in block.items():
                for name, body in named.items():
                    address = f"{type_.strip(chr(34))}.{name.strip(chr(34))}"
                    deps[address] = [d.strip("${}") for d in body.get("depends_on", [])]
    for address, prerequisite in p1c.DEPENDS_ON.items():
        assert deps[address] == [prerequisite]


# --- the budget --------------------------------------------------------------------------------


def test_another_budget_change_stops(plan: dict) -> None:
    after(plan, p1c.BUDGET)["limit_amount"] = "400.0"
    assert problems(plan) == [f"{p1c.BUDGET}: may change only {sorted(p1c.BUDGET_CHANGES)}"]


@pytest.mark.parametrize("key", ["cost_filter", "cost_types"])
def test_the_deprecated_filters_must_be_gone(plan: dict, key: str) -> None:
    after(plan, p1c.BUDGET)[key] = BUDGET_BEFORE[key]
    assert problems(plan) == [
        f"{p1c.BUDGET}: the deprecated cost_filter and cost_types must be gone"
    ]


def test_the_provider_s_real_switch_plan_stops(plan: dict) -> None:
    """Disposable probe, 2026-10-06 (provider 6.67.0, plan -refresh=false on a state holding the
    legacy filter): cost_filter and cost_types are Optional+Computed, so the planned update keeps
    them next to filter_expression and metrics. UpdateBudget would then get both filter styles,
    which the Budgets API refuses ("Either FilterExpression and Metrics or CostFilters and
    CostTypes, not both")."""
    for key in ("cost_filter", "cost_types"):
        after(plan, p1c.BUDGET)[key] = BUDGET_BEFORE[key]
    assert problems(plan) == [
        f"{p1c.BUDGET}: the deprecated cost_filter and cost_types must be gone"
    ]


@pytest.mark.parametrize("metrics", [["BlendedCost"], ["AmortizedCost"], None])
def test_metrics_must_be_unblended(plan: dict, metrics: Any) -> None:
    after(plan, p1c.BUDGET)["metrics"] = metrics
    expected = [f"{p1c.BUDGET}: metrics must be UnblendedCost"]
    if metrics is None:  # then metrics is not a change at all
        expected.insert(0, f"{p1c.BUDGET}: may change only {sorted(p1c.BUDGET_CHANGES)}")
    assert problems(plan) == expected


def and_filter() -> list[dict]:
    expression = budget_filter()
    expression[0]["and"], expression[0]["or"] = expression[0]["or"], []
    return expression


def account_only_filter() -> list[dict]:
    expression = budget_filter()
    expression[0]["or"] = expression[0]["or"][:1]
    return expression


def tag_without_prefix() -> list[dict]:
    expression = budget_filter()
    expression[0]["or"][1]["tags"][0]["key"] = "project"
    return expression


@pytest.mark.parametrize("expression", [and_filter(), account_only_filter(), tag_without_prefix(),
                                        budget_filter(OTHER_ACCOUNT_ID), []])  # fmt: skip
def test_a_filter_other_than_the_reviewed_or_stops(plan: dict, expression: list) -> None:
    after(plan, p1c.BUDGET)["filter_expression"] = expression
    assert f"{p1c.BUDGET}: filter must be OR(the workload account, the project tag)" in problems(
        plan
    )


def test_a_filter_whose_account_is_not_the_reference_stops(plan: dict) -> None:
    config(plan, p1c.BUDGET)["expressions"]["filter_expression"] = []
    assert problems(plan) == [
        f"{p1c.BUDGET}: filter must be OR(the workload account, the project tag)"
    ]


def test_the_budget_must_be_updated_in_place(plan: dict) -> None:
    change(plan, p1c.BUDGET)["actions"] = ["delete", "create"]
    assert problems(plan) == [f"unexpected change: delete+create {p1c.BUDGET}"]


# --- the state before the plan -----------------------------------------------------------------


@pytest.mark.parametrize("principals", [["sso.amazonaws.com"],
                                        ["iam.amazonaws.com"],
                                        ["iam.amazonaws.com", "sso.amazonaws.com",
                                         "config.amazonaws.com"]])  # fmt: skip
def test_trusted_access_must_be_exactly_iam_and_sso(plan: dict, principals: list[str]) -> None:
    recovered.state_values(plan, p1c.ORG_DATA)["aws_service_access_principals"] = principals
    assert problems(plan) == [
        "trusted access must be exactly iam and sso: enable IAM's first (out-of-band)"
    ]


def test_an_account_outside_workloads_stops(plan: dict) -> None:
    recovered.state_values(plan, p1c.ACCOUNT)["parent_id"] = full.ROOT_ID
    assert problems(plan) == [f"{p1c.ACCOUNT}: must be ecp-workloads in the Workloads OU"]


def test_an_account_without_an_id_stops(plan: dict) -> None:
    recovered.state_values(plan, p1c.ACCOUNT)["id"] = None
    found = problems(plan)
    assert found[0] == f"{p1c.ACCOUNT}: no account ID in state"
    assert f"{p1c.INLINE}: differs from policies/admin-assume-root.json.tftpl" in found


def test_an_applied_scp_unlike_its_document_still_stops(plan: dict) -> None:
    state = recovered.state_values(plan, recovered.BASELINE)
    state["content"] = json.dumps({"Version": "2012-10-17", "Statement": []})
    assert problems(plan) == [
        f"{recovered.BASELINE}: applied content differs from policies/scp-workloads-baseline.json"
    ]


def test_two_identity_center_instances_stop(plan: dict) -> None:
    recovered.state_values(plan, p1c.SSO)["arns"] = [INSTANCE, INSTANCE + "2"]
    assert problems(plan) == [f"{p1c.SSO}: must be exactly one Identity Center instance"]


def test_another_permission_set_named_as_admin_stops(plan: dict) -> None:
    recovered.state_values(plan, p1c.ADMIN_SET)["name"] = "PowerUserAccess"
    assert problems(plan) == [f"{p1c.ADMIN_SET}: must be the AdministratorAccess permission set"]


def test_another_user_than_configured_stops(plan: dict) -> None:
    recovered.state_values(plan, p1c.USER)["user_name"] = "someone-else"
    assert problems(plan) == [f"{p1c.USER}: must be the configured identity_center_user_name"]


def test_the_state_must_be_exactly_phase_1b(plan: dict) -> None:
    resources = plan["prior_state"]["values"]["root_module"]["resources"]
    resources[:] = [r for r in resources if r["address"] != p1c.FEATURES]
    resources.append({"address": p1c.FEATURES, "mode": "managed", "values": {}})
    assert "infra/org state is not exactly phase 1b as applied" in problems(plan)


# --- nothing else ------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", sorted(p1c.CREATES | {p1c.BUDGET}))
def test_a_missing_change_stops(plan: dict, missing: str) -> None:
    plan["resource_changes"] = [r for r in plan["resource_changes"] if r["address"] != missing]
    assert f"missing change: {missing}" in problems(plan)


@pytest.mark.parametrize("address", [p1c.ACCOUNT, recovered.BASELINE, recovered.ATTACH_PROTECT])
def test_a_change_to_phase_1b_stops(plan: dict, address: str) -> None:
    change(plan, address)["actions"] = ["update"]
    assert problems(plan) == [f"unexpected change: update {address}"]


def test_an_import_stops(plan: dict) -> None:
    change(plan, p1c.FEATURES)["importing"] = {"id": "o-ab12cd34ef"}
    assert problems(plan) == [f"unexpected import: {p1c.FEATURES}"]


def test_any_drift_stops_even_tags(plan: dict) -> None:
    plan["resource_drift"] = [recovered.tags_drift(p1c.ACCOUNT)]
    assert problems(plan) == [f"resource_drift: {p1c.ACCOUNT}"]


def test_the_reviewed_plan_without_the_trusted_access_drift_passes(plan: dict) -> None:
    plan["resource_drift"] = []  # e.g. a re-plan once the drift is recorded in state
    assert problems(plan) == []


@pytest.mark.parametrize(("before", "after"), [
    (["sso.amazonaws.com"], ["iam.amazonaws.com"]),  # sso removed
    (["sso.amazonaws.com"], [*IAM_SSO, "config.amazonaws.com"]),  # one more than iam
    (["sso.amazonaws.com"], ["config.amazonaws.com", "sso.amazonaws.com"]),  # not iam
    ([], IAM_SSO),
    (["config.amazonaws.com", "sso.amazonaws.com"], IAM_SSO),
    (IAM_SSO, ["sso.amazonaws.com"]),  # iam removed
])  # fmt: skip
def test_any_other_principals_change_stops(plan: dict, before: list, after: list) -> None:
    plan["resource_drift"] = [principals_drift(before, after)]
    assert f"resource_drift: {recovered.p1b.ORG}" in problems(plan)


def test_the_principals_drift_with_another_attribute_stops(plan: dict) -> None:
    plan["resource_drift"] = [principals_drift(["sso.amazonaws.com"], IAM_SSO,
                                               enabled_policy_types=[])]  # fmt: skip
    assert problems(plan) == [f"resource_drift: {recovered.p1b.ORG}"]


@pytest.mark.parametrize("actions", [["delete"], ["create"], ["no-op"]])
def test_the_principals_drift_with_another_action_stops(plan: dict, actions: list) -> None:
    plan["resource_drift"] = [principals_drift(["sso.amazonaws.com"], IAM_SSO, actions)]
    assert problems(plan) == [f"resource_drift: {recovered.p1b.ORG}"]


def test_the_principals_drift_on_a_planned_organization_change_stops(plan: dict) -> None:
    change(plan, recovered.p1b.ORG)["actions"] = ["update"]
    assert problems(plan) == [
        f"resource_drift: {recovered.p1b.ORG}",
        f"unexpected change: update {recovered.p1b.ORG}",
    ]


def test_the_same_drift_on_another_address_stops(plan: dict) -> None:
    plan["resource_drift"] = [principals_drift(["sso.amazonaws.com"], IAM_SSO,
                                               address=p1c.ACCOUNT)]  # fmt: skip
    assert problems(plan) == [f"resource_drift: {p1c.ACCOUNT}"]


def test_the_principals_drift_does_not_excuse_other_drift(plan: dict) -> None:
    plan["resource_drift"].append(recovered.tags_drift(p1c.ACCOUNT))
    assert problems(plan) == [f"resource_drift: {p1c.ACCOUNT}"]


def test_the_principals_drift_must_appear_at_most_once(plan: dict) -> None:
    plan["resource_drift"] *= 2
    assert problems(plan) == [f"resource_drift: {recovered.p1b.ORG}"]


def test_an_output_change_stops(plan: dict) -> None:
    plan["output_changes"]["workload_account_id"]["actions"] = ["update"]
    assert problems(plan) == ["unexpected output change: update workload_account_id"]


def test_errored_incomplete_or_deferred_plans_stop(plan: dict) -> None:
    plan.update(errored=True, complete=False, deferred_changes=[{"address": p1c.FEATURES}])
    assert problems(plan)[:3] == ["the plan errored", "the plan is incomplete",
                                  f"deferred_changes: {p1c.FEATURES}"]  # fmt: skip


def test_the_cli(tmp_path: Path, plan: dict) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert p1c.main([str(path)]) == 0
    change(plan, p1c.FEATURES)["after"]["enabled_features"] = ["RootSessions"]
    path.write_text(json.dumps(plan))
    assert p1c.main([str(path)]) == 1
    assert p1c.main([]) == 2
