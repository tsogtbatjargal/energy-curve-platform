"""ADR-0021 phase 1b: the saved infra/org plan must be exactly the reviewed change.

The fixture copies the shape of the real read-only plan of 2026-10-05 (Terraform 1.15.8): the
organization imported with only enabled_policy_types changing, two SCPs and their attachments,
the account, every phase 1a resource unchanged, and the OU's tags reading back as {}.
"""

import copy
import json
from pathlib import Path
from typing import Any

import org_plan_check as opc
import phase1b_plan_check as p1b
import pytest

ROOT = Path(__file__).parents[1]
ORG_ID, OU_ID, ROOT_ID = "o-ab12cd34ef", "ou-ab12-cdefgh34", "r-ab12"
EMAIL = "workloads@example.com"
BASELINE, PROTECT = (f"aws_organizations_policy.workloads_{n}" for n in ("baseline", "protect"))
ATTACH_BASELINE, ATTACH_PROTECT = (f"aws_organizations_policy_attachment.workloads_{n}"
                                   for n in ("baseline", "protect"))  # fmt: skip


def rc(address: str, actions: list[str], before: Any, after: Any, **extra: Any) -> dict:
    change = {"actions": actions, "before": before, "after": after, "after_unknown": {}, **extra}
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "change": change}  # fmt: skip


def document(name: str) -> str:
    return json.dumps(json.loads((ROOT / "infra" / "org" / "policies" / name).read_text()))


@pytest.fixture
def plan() -> dict:
    org_before = {"id": ORG_ID, "feature_set": "ALL", "enabled_policy_types": [],
                  "aws_service_access_principals": ["sso.amazonaws.com"]}  # fmt: skip
    org_after = {**org_before, "enabled_policy_types": ["SERVICE_CONTROL_POLICY"]}
    phase_1a = sorted(opc.MOVED | {opc.OU})
    ou_values = {"id": OU_ID, "name": "Workloads", "parent_id": ROOT_ID, "tags": {}}
    return {
        "complete": True,
        "variables": {"workload_account_email": {"value": EMAIL}},
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_organizations_organization.this", "mode": "data",
             "values": {"id": ORG_ID}},
            *[{"address": a, "mode": "managed", "values": ou_values if a == opc.OU else {}}
              for a in phase_1a],
            {"address": p1b.ORG, "mode": "managed", "values": org_before},  # pending import
        ]}}},
        "configuration": {"root_module": {"resources": [
            {"address": p1b.ACCOUNT, "depends_on": [ATTACH_BASELINE, ATTACH_PROTECT]},
            {"address": ATTACH_BASELINE, "expressions": {"policy_id": {
                "references": [f"{BASELINE}.id", BASELINE]}}},
            {"address": ATTACH_PROTECT, "expressions": {"policy_id": {
                "references": [f"{PROTECT}.id", PROTECT]}}},
        ]}},
        "resource_changes": [
            *[rc(a, ["no-op"], {}, {}) for a in phase_1a],
            rc(p1b.ORG, ["update"], org_before, org_after, importing={"id": ORG_ID}),
            rc(BASELINE, ["create"], None, {"name": "ecp-workloads-baseline",
               "type": "SERVICE_CONTROL_POLICY",
               "content": document("scp-workloads-baseline.json")},
               after_unknown={"id": True, "arn": True}),
            rc(PROTECT, ["create"], None, {"name": "ecp-workloads-protect",
               "type": "SERVICE_CONTROL_POLICY",
               "content": document("scp-workloads-protect.json")},
               after_unknown={"id": True, "arn": True}),
            rc(ATTACH_BASELINE, ["create"], None, {"target_id": OU_ID},
               after_unknown={"id": True, "policy_id": True}),
            rc(ATTACH_PROTECT, ["create"], None, {"target_id": OU_ID},
               after_unknown={"id": True, "policy_id": True}),
            rc(p1b.ACCOUNT, ["create"], None,
               {**p1b.ACCOUNT_EXPECTED, "parent_id": OU_ID, "email": EMAIL},
               after_unknown={"id": True, "arn": True, "state": True},
               after_sensitive={"email": True}),
        ],
        "resource_drift": [
            {"address": opc.OU, "change": {"actions": ["update"],
             "before": {**ou_values, "tags": None}, "after": ou_values}},
        ],
        "output_changes": {"workloads_ou_id": {"actions": ["no-op"]},
                           "workload_account_id": {"actions": ["create"]}},
    }  # fmt: skip


def change(plan: dict, address: str) -> dict:
    return next(r for r in plan["resource_changes"] if r["address"] == address)["change"]


def problems(plan: dict) -> list[str]:
    return p1b.check(plan)[0]


def test_the_reviewed_plan_passes(plan: dict) -> None:
    found, report = p1b.check(plan)
    assert found == []
    assert report[-1] == (
        "summary: 1 to import (the organization), 5 to add (2 SCPs, 2 attachments, the account), "
        "1 to change (the import enables SCPs), 0 to destroy"
    )
    assert not any(EMAIL in line or OU_ID in line or ORG_ID in line for line in report)


# --- the organization: enable SCPs, nothing else ----------------------------------------------


def test_the_organization_must_be_imported(plan: dict) -> None:
    del change(plan, p1b.ORG)["importing"]
    assert f"{p1b.ORG}: must be an import of this organization" in problems(plan)


def test_an_import_of_another_organization_stops(plan: dict) -> None:
    change(plan, p1b.ORG)["importing"]["id"] = "o-other00000"
    assert problems(plan) == [f"{p1b.ORG}: must be an import of this organization"]


def test_removing_trusted_service_access_stops(plan: dict) -> None:
    change(plan, p1b.ORG)["after"]["aws_service_access_principals"] = []
    assert problems(plan) == [f"{p1b.ORG}: may change enabled_policy_types only"]


@pytest.mark.parametrize("types", [["SERVICE_CONTROL_POLICY", "TAG_POLICY"], ["TAG_POLICY"], []])
def test_enabling_anything_but_scps_stops(plan: dict, types: list[str]) -> None:
    change(plan, p1b.ORG)["after"]["enabled_policy_types"] = types
    assert f"{p1b.ORG}: must enable exactly SERVICE_CONTROL_POLICY" in problems(plan)


def test_changing_the_feature_set_stops(plan: dict) -> None:
    change(plan, p1b.ORG)["after"]["feature_set"] = "CONSOLIDATED_BILLING"
    found = problems(plan)
    assert f"{p1b.ORG}: feature set must stay ALL" in found
    assert f"{p1b.ORG}: may change enabled_policy_types only" in found


# --- the SCPs and their attachments ------------------------------------------------------------


@pytest.mark.parametrize("address", [BASELINE, PROTECT])
def test_an_scp_other_than_the_reviewed_document_stops(plan: dict, address: str) -> None:
    after = change(plan, address)["after"]
    doc = json.loads(after["content"])
    doc["Statement"] = doc["Statement"][:-1]
    after["content"] = json.dumps(doc)
    assert problems(plan) == [f"{address}: content differs from policies/{p1b.SCPS[address][1]}"]


def test_an_scp_unknown_at_plan_time_stops(plan: dict) -> None:
    change(plan, BASELINE)["after_unknown"]["content"] = True
    assert problems(plan) == [
        f"{BASELINE}: content differs from policies/scp-workloads-baseline.json"
    ]


@pytest.mark.parametrize(("key", "value"), [("name", "other"), ("type", "TAG_POLICY")])
def test_an_scp_with_another_name_or_type_stops(plan: dict, key: str, value: str) -> None:
    change(plan, PROTECT)["after"][key] = value
    assert problems(plan) == [f"{PROTECT}: must be the SCP ecp-workloads-protect"]


@pytest.mark.parametrize("target", [ROOT_ID, "ou-ab12-other000", None])
def test_an_attachment_elsewhere_stops(plan: dict, target: str | None) -> None:
    change(plan, ATTACH_PROTECT)["after"]["target_id"] = target
    assert problems(plan) == [f"{ATTACH_PROTECT}: must target the Workloads OU"]


def test_an_attachment_of_the_other_policy_stops(plan: dict) -> None:
    config = plan["configuration"]["root_module"]["resources"]
    next(r for r in config if r["address"] == ATTACH_PROTECT)["expressions"]["policy_id"] = {
        "references": [f"{BASELINE}.id", BASELINE]
    }
    assert problems(plan) == [f"{ATTACH_PROTECT}: must attach {PROTECT}"]


# --- the account -------------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "value"), [
    ("name", "ecp-other"), ("close_on_deletion", True), ("role_name", "Admin"),
    ("iam_user_access_to_billing", "DENY"), ("create_govcloud", True)])  # fmt: skip
def test_an_account_unlike_the_reviewed_one_stops(plan: dict, key: str, value: Any) -> None:
    change(plan, p1b.ACCOUNT)["after"][key] = value
    assert problems(plan) == [f"{p1b.ACCOUNT}: {key} must be {p1b.ACCOUNT_EXPECTED[key]!r}"]


@pytest.mark.parametrize("parent", [ROOT_ID, "ou-ab12-other000"])
def test_an_account_outside_the_workloads_ou_stops(plan: dict, parent: str) -> None:
    change(plan, p1b.ACCOUNT)["after"]["parent_id"] = parent
    assert problems(plan) == [f"{p1b.ACCOUNT}: must be created in the Workloads OU"]


def test_an_email_other_than_the_configured_one_stops(plan: dict) -> None:
    change(plan, p1b.ACCOUNT)["after"]["email"] = "other@example.com"
    assert problems(plan) == [f"{p1b.ACCOUNT}: email must be the configured workload_account_email"]


def test_an_unset_email_variable_stops(plan: dict) -> None:
    plan["variables"] = {}
    assert problems(plan) == [f"{p1b.ACCOUNT}: email must be the configured workload_account_email"]


def test_an_email_that_is_not_sensitive_stops(plan: dict) -> None:
    change(plan, p1b.ACCOUNT)["after_sensitive"] = {}
    assert problems(plan) == [f"{p1b.ACCOUNT}: email must stay sensitive"]


def test_the_account_must_wait_for_both_attachments(plan: dict) -> None:
    config = plan["configuration"]["root_module"]["resources"]
    next(r for r in config if r["address"] == p1b.ACCOUNT)["depends_on"] = [ATTACH_BASELINE]
    assert problems(plan) == [f"{p1b.ACCOUNT}: must depend on both SCP attachments"]


# --- nothing else ------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", [p1b.ACCOUNT, BASELINE, ATTACH_PROTECT, p1b.ORG])
def test_a_missing_change_stops(plan: dict, missing: str) -> None:
    plan["resource_changes"] = [r for r in plan["resource_changes"] if r["address"] != missing]
    assert f"missing change: {missing}" in problems(plan)


@pytest.mark.parametrize("address", [
    "aws_ssoadmin_permission_set_inline_policy.admin", "aws_iam_organizations_features.this",
    "aws_organizations_aws_service_access.iam", "aws_organizations_policy_attachment.root",
    "aws_organizations_account.second"])  # fmt: skip
def test_any_other_create_stops(plan: dict, address: str) -> None:
    plan["resource_changes"].append(rc(address, ["create"], None, {}))
    assert problems(plan) == [f"unexpected change: create {address}"]


@pytest.mark.parametrize("address", [opc.BUDGET, "aws_s3_bucket_policy.tfstate", opc.OU])
@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"], ["forget"]])
def test_any_change_to_a_phase_1a_resource_stops(
    plan: dict, address: str, actions: list[str]
) -> None:
    change(plan, address)["actions"] = actions
    found = problems(plan)
    assert f"unexpected change: {'+'.join(actions)} {address}" in found


def test_the_state_must_be_exactly_phase_1a(plan: dict) -> None:
    plan["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_organizations_policy.leftover", "mode": "managed", "values": {}}
    )
    plan["resource_changes"].append(rc("aws_organizations_policy.leftover", ["no-op"], {}, {}))
    assert problems(plan) == ["infra/org state is not exactly the phase 1a resources"]


# --- drift -------------------------------------------------------------------------------------


def test_other_ou_drift_stops(plan: dict) -> None:
    plan["resource_drift"][0]["change"]["after"] = {**plan["resource_drift"][0]["change"]["after"],
                                                     "name": "Renamed"}  # fmt: skip
    assert problems(plan) == [f"resource_drift: {opc.OU}"]


@pytest.mark.parametrize(("before", "after"), [(None, {"k": "v"}), ({}, None), (None, None)])
def test_other_ou_tag_drift_stops(plan: dict, before: Any, after: Any) -> None:
    drift = plan["resource_drift"][0]["change"]
    drift["before"]["tags"], drift["after"] = before, {**drift["after"], "tags": after}
    assert problems(plan) == [f"resource_drift: {opc.OU}"]


def test_drift_on_another_resource_stops(plan: dict) -> None:
    plan["resource_drift"][0]["address"] = "aws_s3_bucket.tfstate"
    assert problems(plan) == ["resource_drift: aws_s3_bucket.tfstate"]


def test_ou_drift_with_a_planned_ou_change_stops(plan: dict) -> None:
    change(plan, opc.OU)["actions"] = ["update"]
    assert problems(plan) == [f"resource_drift: {opc.OU}", f"unexpected change: update {opc.OU}"]


def test_a_non_update_drift_entry_stops(plan: dict) -> None:
    plan["resource_drift"][0]["change"]["actions"] = ["delete"]
    assert problems(plan) == [f"resource_drift: {opc.OU}"]


# --- plan status and outputs -------------------------------------------------------------------


def test_errored_incomplete_or_deferred_plans_stop(plan: dict) -> None:
    plan["errored"], plan["complete"] = True, False
    plan["deferred_changes"] = [{"address": p1b.ACCOUNT}]
    assert problems(plan)[:3] == [
        "the plan errored",
        "the plan is incomplete",
        f"deferred_changes: {p1b.ACCOUNT}",
    ]


def test_an_unexpected_output_change_stops(plan: dict) -> None:
    plan["output_changes"]["workloads_ou_id"]["actions"] = ["update"]
    assert problems(plan) == ["unexpected output change: update workloads_ou_id"]


def test_the_cli(tmp_path: Path, plan: dict) -> None:
    good = tmp_path / "plan.json"
    good.write_text(json.dumps(plan))
    bad_plan = copy.deepcopy(plan)
    bad_plan["errored"] = True
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_plan))
    assert p1b.main([str(good)]) == 0
    assert p1b.main([str(bad)]) == 1
    assert p1b.main([]) == 2


@pytest.mark.parametrize("address", [p1b.ACCOUNT, BASELINE, ATTACH_BASELINE])
@pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"], ["update"]])
def test_the_new_resources_must_be_plain_creates(
    plan: dict, address: str, actions: list[str]
) -> None:
    change(plan, address)["actions"] = actions
    assert f"unexpected change: {'+'.join(actions)} {address}" in problems(plan)


# --- the source: safety settings that never show in a plan --------------------------------------


def resource_body(type_: str, name: str) -> dict:
    from test_org_plan_check import load

    for block in load("org")["resource"]:
        for t, named in block.items():
            if t.strip('"') == type_ and f'"{name}"' in named:
                return named[f'"{name}"']
    raise AssertionError(f"{type_}.{name} not declared")


def test_the_organization_cannot_be_destroyed_or_lose_service_access() -> None:
    lifecycle = resource_body("aws_organizations_organization", "this")["lifecycle"][0]
    assert lifecycle["prevent_destroy"] is True
    assert "aws_service_access_principals" in lifecycle["ignore_changes"]


def test_the_account_cannot_be_destroyed_or_closed_by_terraform() -> None:
    body = resource_body("aws_organizations_account", "workloads")
    assert body["lifecycle"][0]["prevent_destroy"] is True
    assert body["close_on_deletion"] is False
