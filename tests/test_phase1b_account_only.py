"""ADR-0021 phase 1b recovery: the account-only plan after the 2026-10-06 apply.

That apply imported the organization, enabled SCPs, created both SCPs and attached them to the
Workloads OU, then CreateAccount failed (EMAIL_ALREADY_EXISTS) and nothing else was created. The
recovery plan may only create the account; the applied part is checked in the prior state.
"""

import copy
import json
from pathlib import Path
from typing import Any

import org_plan_check as opc
import phase1b_plan_check as p1b
import pytest
import test_phase1b_plan_check as full

ATTACH_BASELINE, ATTACH_PROTECT = full.ATTACH_BASELINE, full.ATTACH_PROTECT
BASELINE, PROTECT = full.BASELINE, full.PROTECT
POLICY_IDS = {BASELINE: "p-base0001", PROTECT: "p-prot0001"}
ALERT = "alerts@example.com"


def prior(plan: dict) -> list[dict]:
    return plan["prior_state"]["values"]["root_module"]["resources"]


def state_values(plan: dict, address: str) -> dict:
    return next(r for r in prior(plan) if r["address"] == address)["values"]


@pytest.fixture
def plan() -> dict:
    """The full phase 1b fixture, turned into the state the failed apply left behind."""
    p = full.plan.__wrapped__()
    applied = {
        p1b.ORG: {"id": full.ORG_ID, "feature_set": "ALL",
                  "enabled_policy_types": ["SERVICE_CONTROL_POLICY"],
                  "aws_service_access_principals": ["sso.amazonaws.com"]},
        BASELINE: {"id": POLICY_IDS[BASELINE], "name": "ecp-workloads-baseline",
                   "type": "SERVICE_CONTROL_POLICY",
                   "content": full.document("scp-workloads-baseline.json")},
        PROTECT: {"id": POLICY_IDS[PROTECT], "name": "ecp-workloads-protect",
                  "type": "SERVICE_CONTROL_POLICY",
                  "content": full.document("scp-workloads-protect.json")},
        ATTACH_BASELINE: {"target_id": full.OU_ID, "policy_id": POLICY_IDS[BASELINE]},
        ATTACH_PROTECT: {"target_id": full.OU_ID, "policy_id": POLICY_IDS[PROTECT]},
    }  # fmt: skip
    p["prior_state"]["values"]["root_module"]["resources"] = [
        r for r in prior(p) if r["address"] != p1b.ORG
    ] + [{"address": a, "mode": "managed", "values": v} for a, v in applied.items()]
    p["resource_changes"] = [r for r in p["resource_changes"]
                             if r["address"] not in applied]  # fmt: skip
    p["resource_changes"] += [full.rc(a, ["no-op"], v, v) for a, v in applied.items()]
    p["resource_drift"] = []
    p["variables"]["budget_alert_emails"] = {"value": [ALERT]}
    return p


def problems(plan: dict, stack_dir: Path | None = None) -> list[str]:
    args = (plan,) if stack_dir is None else (plan, stack_dir)
    return p1b.check_account_only(*args)[0]


def test_the_account_only_plan_passes(plan: dict) -> None:
    found, report = p1b.check_account_only(plan)
    assert found == []
    assert report == [
        f"create   {p1b.ACCOUNT}",
        "summary: 0 to import, 1 to add (the account), 0 to change, 0 to destroy; the applied "
        "SCPs, attachments and SCP enablement verified unchanged",
    ]
    assert not any(full.EMAIL in line or full.OU_ID in line for line in report)


def test_the_modes_do_not_accept_each_others_plans(plan: dict) -> None:
    assert p1b.check(plan)[0]  # the full mode wants the import and five creates
    assert problems(full.plan.__wrapped__())  # the recovery mode refuses the full plan


# --- the applied part, checked in the prior state ----------------------------------------------


@pytest.mark.parametrize("address", [BASELINE, PROTECT])
def test_an_applied_scp_unlike_its_document_stops(plan: dict, address: str) -> None:
    doc = json.loads(state_values(plan, address)["content"])
    doc["Statement"] = doc["Statement"][:-1]
    state_values(plan, address)["content"] = json.dumps(doc)
    assert problems(plan) == [
        f"{address}: applied content differs from policies/{p1b.SCPS[address][1]}"
    ]


@pytest.mark.parametrize(("key", "value"), [("name", "other"), ("type", "TAG_POLICY")])
def test_an_applied_scp_with_another_name_or_type_stops(plan: dict, key: str, value: str) -> None:
    state_values(plan, BASELINE)[key] = value
    assert problems(plan) == [f"{BASELINE}: must be the applied SCP ecp-workloads-baseline"]


@pytest.mark.parametrize("target", [full.ROOT_ID, "ou-ab12-other000"])
def test_an_attachment_elsewhere_stops(plan: dict, target: str) -> None:
    state_values(plan, ATTACH_PROTECT)["target_id"] = target
    assert problems(plan) == [f"{ATTACH_PROTECT}: must be attached to the Workloads OU"]


def test_an_attachment_of_the_other_policy_stops(plan: dict) -> None:
    state_values(plan, ATTACH_PROTECT)["policy_id"] = POLICY_IDS[BASELINE]
    assert problems(plan) == [f"{ATTACH_PROTECT}: must attach {PROTECT}"]


def test_an_attachment_of_a_policy_without_an_id_stops(plan: dict) -> None:
    state_values(plan, PROTECT)["id"] = None
    state_values(plan, ATTACH_PROTECT)["policy_id"] = None
    assert problems(plan) == [f"{ATTACH_PROTECT}: must attach {PROTECT}"]


@pytest.mark.parametrize("types", [[], ["SERVICE_CONTROL_POLICY", "TAG_POLICY"], None])
def test_scps_must_be_enabled_and_nothing_else(plan: dict, types: Any) -> None:
    state_values(plan, p1b.ORG)["enabled_policy_types"] = types
    assert problems(plan) == [f"{p1b.ORG}: SCPs must be enabled, and nothing else"]


def test_the_feature_set_must_stay_all(plan: dict) -> None:
    state_values(plan, p1b.ORG)["feature_set"] = "CONSOLIDATED_BILLING"
    assert problems(plan) == [f"{p1b.ORG}: feature set must be ALL"]


@pytest.mark.parametrize("missing", [ATTACH_PROTECT, BASELINE, p1b.ORG])
def test_a_missing_applied_resource_stops(plan: dict, missing: str) -> None:
    plan["prior_state"]["values"]["root_module"]["resources"] = [
        r for r in prior(plan) if r["address"] != missing
    ]
    assert "infra/org state is not exactly phase 1a plus the applied part of 1b" in problems(plan)


def test_an_extra_resource_in_state_stops(plan: dict) -> None:
    prior(plan).append({"address": "aws_organizations_policy.leftover", "mode": "managed",
                        "values": {}})  # fmt: skip
    plan["resource_changes"].append(full.rc("aws_organizations_policy.leftover", ["no-op"], {}, {}))
    assert problems(plan) == ["infra/org state is not exactly phase 1a plus the applied part of 1b"]


# --- nothing but the account ---------------------------------------------------------------------


@pytest.mark.parametrize("address", [p1b.ORG, BASELINE, ATTACH_PROTECT, opc.OU, opc.BUDGET,
                                     "aws_s3_bucket_policy.tfstate"])  # fmt: skip
@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"], ["forget"]])
def test_any_other_change_stops(plan: dict, address: str, actions: list[str]) -> None:
    full.change(plan, address)["actions"] = actions
    assert f"unexpected change: {'+'.join(actions)} {address}" in problems(plan)


def test_an_import_stops(plan: dict) -> None:
    full.change(plan, p1b.ORG)["importing"] = {"id": full.ORG_ID}
    assert f"unexpected import: {p1b.ORG}" in problems(plan)


@pytest.mark.parametrize("address", [
    "aws_organizations_account.second", "aws_organizations_policy_attachment.root",
    "aws_ssoadmin_permission_set_inline_policy.admin"])  # fmt: skip
def test_any_other_create_stops(plan: dict, address: str) -> None:
    plan["resource_changes"].append(full.rc(address, ["create"], None, {}))
    assert problems(plan) == [f"unexpected change: create {address}"]


def test_a_missing_account_stops(plan: dict) -> None:
    plan["resource_changes"] = [r for r in plan["resource_changes"]
                                if r["address"] != p1b.ACCOUNT]  # fmt: skip
    assert problems(plan) == [f"missing change: {p1b.ACCOUNT}"]


# --- the account: every full-mode rule still applies ---------------------------------------------


@pytest.mark.parametrize(("key", "value"), [
    ("name", "ecp-other"), ("close_on_deletion", True), ("role_name", "Admin"),
    ("iam_user_access_to_billing", "DENY"), ("create_govcloud", True)])  # fmt: skip
def test_an_account_unlike_the_reviewed_one_stops(plan: dict, key: str, value: Any) -> None:
    full.change(plan, p1b.ACCOUNT)["after"][key] = value
    assert problems(plan) == [f"{p1b.ACCOUNT}: {key} must be {p1b.ACCOUNT_EXPECTED[key]!r}"]


def test_an_account_outside_the_workloads_ou_stops(plan: dict) -> None:
    full.change(plan, p1b.ACCOUNT)["after"]["parent_id"] = full.ROOT_ID
    assert problems(plan) == [f"{p1b.ACCOUNT}: must be created in the Workloads OU"]


@pytest.mark.parametrize("email", [ALERT, ALERT.upper()])
def test_the_rejected_alert_address_stops(plan: dict, email: str) -> None:
    """2026-10-06: CreateAccount failed EMAIL_ALREADY_EXISTS with the budget alert address."""
    plan["variables"]["workload_account_email"]["value"] = email
    full.change(plan, p1b.ACCOUNT)["after"]["email"] = email
    assert problems(plan) == [f"{p1b.ACCOUNT}: email must not be the budget alert address"]


def test_the_rejected_alert_address_stops_the_full_mode_too() -> None:
    plan = full.plan.__wrapped__()
    plan["variables"]["budget_alert_emails"] = {"value": [full.EMAIL]}
    assert p1b.check(plan)[0] == [f"{p1b.ACCOUNT}: email must not be the budget alert address"]


def test_the_email_must_stay_sensitive_and_configured(plan: dict) -> None:
    account = full.change(plan, p1b.ACCOUNT)
    account["after_sensitive"] = {}
    account["after"]["email"] = "other@example.com"
    assert problems(plan) == [
        f"{p1b.ACCOUNT}: email must be the configured workload_account_email",
        f"{p1b.ACCOUNT}: email must stay sensitive",
    ]


def test_the_account_must_wait_for_both_attachments(plan: dict) -> None:
    config = plan["configuration"]["root_module"]["resources"]
    next(r for r in config if r["address"] == p1b.ACCOUNT)["depends_on"] = [ATTACH_BASELINE]
    assert problems(plan) == [f"{p1b.ACCOUNT}: must depend on both SCP attachments"]


# --- the source and the plan's status: the full-mode safeguards ----------------------------------


def test_an_override_file_in_the_source_stops(plan: dict, tmp_path: Path) -> None:
    full.stack(tmp_path)
    (tmp_path / "scps_override.tf.json").write_text(full.REVIEWED_JSON_OVERRIDE)
    assert problems(plan, tmp_path) == [
        "override files are not allowed in the stack: scps_override.tf.json"
    ]


def test_a_rewritten_attachment_in_the_source_stops(plan: dict, tmp_path: Path) -> None:
    rewritten = f'replace({PROTECT}.id, "/^.*$/", "p-FullAWSAccess")'
    assert problems(plan, full.stack(tmp_path, protect=rewritten)) == [
        f"{ATTACH_PROTECT}: policy_id must be exactly {PROTECT}.id in the source"
    ]


def test_module_calls_stop(plan: dict) -> None:
    plan["configuration"]["root_module"]["module_calls"] = {"m": {"source": "./m"}}
    assert problems(plan) == ["module calls are not allowed in the stack: m"]


def test_drift_other_than_the_ou_tags_stops(plan: dict) -> None:
    plan["resource_drift"] = [{"address": BASELINE, "change": {
        "actions": ["update"], "before": {"content": "a"}, "after": {"content": "b"}}}]  # fmt: skip
    assert problems(plan) == [f"resource_drift: {BASELINE}"]


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


def test_the_cli_selects_the_mode(tmp_path: Path, plan: dict) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert p1b.main(["--mode", "account-only", str(path)]) == 0
    assert p1b.main([str(path)]) == 1  # the default full mode refuses it
    assert p1b.main(["--mode", "other", str(path)]) == 2
    bad = copy.deepcopy(plan)
    bad["errored"] = True
    path.write_text(json.dumps(bad))
    assert p1b.main(["--mode", "account-only", str(path)]) == 1


@pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"], ["update"]])
def test_the_account_must_be_a_plain_create(plan: dict, actions: list[str]) -> None:
    full.change(plan, p1b.ACCOUNT)["actions"] = actions
    found = problems(plan)
    assert f"unexpected change: {'+'.join(actions)} {p1b.ACCOUNT}" in found
    assert f"missing change: {p1b.ACCOUNT}" in found


# --- refresh drift on the applied SCPs (the real account-only plan, 2026-10-06) ------------------
# Terraform's refresh read both SCPs' tags back as {} where state had null, the same normalization
# as the OU's. Both are planned no-op. Account-only mode accepts exactly that, on exactly those two
# addresses; every other drift still stops, and the full mode is unchanged.


def tags_drift(address: str, before: Any = None, after: Any = None,
               actions: list[str] | None = None, **other: Any) -> dict:  # fmt: skip
    b = {"name": "n", "content": "c", "tags": before}
    a = {"name": "n", "content": "c", "tags": {} if after is None and not other else after}
    a.update(other)
    return {"address": address, "change": {"actions": actions or ["update"], "before": b,
                                           "after": a}}  # fmt: skip


def real_scp_drift(plan: dict) -> dict:
    plan["resource_drift"] = [tags_drift(BASELINE), tags_drift(PROTECT)]
    return plan


def test_the_two_real_scp_tags_drifts_pass(plan: dict) -> None:
    assert problems(real_scp_drift(plan)) == []


@pytest.mark.parametrize("address", [BASELINE, PROTECT])
def test_each_real_scp_tags_drift_passes_alone(plan: dict, address: str) -> None:
    plan["resource_drift"] = [tags_drift(address)]
    assert problems(plan) == []


@pytest.mark.parametrize(("before", "after"), [(None, {"k": "v"}), ({}, None), ({"a": "b"}, {}),
                                               (None, None), ({}, {"k": "v"})])  # fmt: skip
def test_other_scp_tag_changes_stop(plan: dict, before: Any, after: Any) -> None:
    item = tags_drift(PROTECT, before)
    item["change"]["after"]["tags"] = after
    plan["resource_drift"] = [item]
    assert problems(plan) == [f"resource_drift: {PROTECT}"]


@pytest.mark.parametrize("other", [{"content": "edited"}, {"name": "other"},
                                   {"type": "TAG_POLICY"}, {"description": "x"}])  # fmt: skip
def test_tags_drift_with_a_policy_change_stops(plan: dict, other: dict) -> None:
    item = tags_drift(BASELINE)
    item["change"]["after"].update(other)
    plan["resource_drift"] = [item]
    assert problems(plan) == [f"resource_drift: {BASELINE}"]


@pytest.mark.parametrize("address", [
    ATTACH_BASELINE, ATTACH_PROTECT, p1b.ORG, opc.BUDGET, "aws_organizations_policy.other",
    "aws_s3_bucket.tfstate"])  # fmt: skip
def test_the_same_tags_drift_on_another_address_stops(plan: dict, address: str) -> None:
    plan["resource_drift"] = [tags_drift(address)]
    assert problems(plan) == [f"resource_drift: {address}"]


@pytest.mark.parametrize("actions", [["delete"], ["create"], ["no-op"], ["delete", "create"]])
def test_a_drift_action_other_than_update_stops(plan: dict, actions: list[str]) -> None:
    plan["resource_drift"] = [tags_drift(BASELINE, actions=actions)]
    assert problems(plan) == [f"resource_drift: {BASELINE}"]


@pytest.mark.parametrize("planned", [["update"], ["delete", "create"], ["delete"]])
def test_scp_tags_drift_with_a_planned_change_stops(plan: dict, planned: list[str]) -> None:
    real_scp_drift(plan)
    full.change(plan, PROTECT)["actions"] = planned
    found = problems(plan)
    assert f"resource_drift: {PROTECT}" in found
    assert f"unexpected change: {'+'.join(planned)} {PROTECT}" in found


def test_scp_tags_drift_with_content_unlike_the_document_still_stops(plan: dict) -> None:
    """The content check reads the refreshed state, so the drift exception cannot hide it."""
    real_scp_drift(plan)
    state_values(plan, BASELINE)["content"] = json.dumps({"Statement": []})
    assert problems(plan) == [
        f"{BASELINE}: applied content differs from policies/scp-workloads-baseline.json"
    ]


def test_the_full_mode_still_refuses_scp_drift() -> None:
    plan = full.plan.__wrapped__()
    plan["resource_drift"].append(tags_drift(BASELINE))
    assert f"resource_drift: {BASELINE}" in p1b.check(plan)[0]
