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
MANAGEMENT_EMAIL = "Management@Example.com"  # the organization's existing (management) account
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
             "values": {"id": ORG_ID, "master_account_email": MANAGEMENT_EMAIL,
                        "accounts": [{"email": MANAGEMENT_EMAIL, "name": "management"}],
                        "non_master_accounts": []}},
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


# --- F1 (review of 260537d): an attachment must reference its policy directly and only ---------
# Plan JSON keeps only the references of an expression, not its literals or functions, so the check
# requires exactly the direct reference (`<policy>.id` and `<policy>`) and an unknown policy_id:
# a policy created in this plan has no ID yet, so a known value means something else was attached.


def set_policy_id(plan: dict, attachment: str, references: list[str]) -> None:
    config = plan["configuration"]["root_module"]["resources"]
    next(r for r in config if r["address"] == attachment)["expressions"]["policy_id"] = {
        "references": references
    }


def test_an_attachment_referencing_both_policies_stops(plan: dict) -> None:
    """coalesce(aws_organizations_policy.workloads_baseline.id, ...protect.id) and the like."""
    set_policy_id(plan, ATTACH_PROTECT, [f"{BASELINE}.id", BASELINE, f"{PROTECT}.id", PROTECT])
    assert problems(plan) == [f"{ATTACH_PROTECT}: must attach {PROTECT}"]


def test_an_attachment_with_another_input_stops(plan: dict) -> None:
    """var.policy_id != "" ? var.policy_id : aws_organizations_policy.workloads_protect.id"""
    set_policy_id(plan, ATTACH_PROTECT, ["var.policy_id", f"{PROTECT}.id", PROTECT])
    assert problems(plan) == [f"{ATTACH_PROTECT}: must attach {PROTECT}"]


def test_an_attachment_resolving_to_a_known_policy_stops(plan: dict) -> None:
    """try("p-other", aws_organizations_policy.workloads_protect.id): the references look right,
    but the value is known at plan time, so it is not the policy this plan creates."""
    attachment = change(plan, ATTACH_PROTECT)
    attachment["after_unknown"]["policy_id"] = False
    attachment["after"]["policy_id"] = "p-other0000"
    assert problems(plan) == [f"{ATTACH_PROTECT}: must attach {PROTECT}"]


# --- F1 again (review of 6401056): the expression itself, read from the source -----------------
# replace(<policy>.id, "/^.*$/", "p-FullAWSAccess") has exactly the direct references and stays
# unknown during planning, so no plan-JSON test can tell it from <policy>.id. The check must read
# the attachment's policy_id expression from the stack source.

ATTACHMENT_SOURCE = """
resource "aws_organizations_policy_attachment" "workloads_baseline" {{
  policy_id = {baseline}
  target_id = aws_organizations_organizational_unit.workloads.id
}}

resource "aws_organizations_policy_attachment" "workloads_protect" {{
  policy_id = {protect}
  target_id = aws_organizations_organizational_unit.workloads.id
}}
"""


def stack(tmp_path: Path, baseline: str = f"{BASELINE}.id", protect: str = f"{PROTECT}.id",
          source: str = ATTACHMENT_SOURCE) -> Path:  # fmt: skip
    (tmp_path / "scps.tf").write_text(source.format(baseline=baseline, protect=protect))
    return tmp_path


def test_direct_references_in_the_source_pass(plan: dict, tmp_path: Path) -> None:
    assert p1b.check(plan, stack(tmp_path))[0] == []


def test_a_rewritten_policy_id_in_the_source_stops(plan: dict, tmp_path: Path) -> None:
    rewritten = f'replace({PROTECT}.id, "/^.*$/", "p-FullAWSAccess")'
    assert p1b.check(plan, stack(tmp_path, protect=rewritten))[0] == [
        f"{ATTACH_PROTECT}: policy_id must be exactly {PROTECT}.id in the source"
    ]


@pytest.mark.parametrize("expr", [
    f'try("p-FullAWSAccess", {PROTECT}.id)',
    f"coalesce({PROTECT}.id, {BASELINE}.id)",
    f'var.override != "" ? var.override : {PROTECT}.id',
    f"{BASELINE}.id",
    '"p-FullAWSAccess"',
    f"lower({PROTECT}.id)",
])  # fmt: skip
def test_any_other_policy_id_expression_stops(plan: dict, tmp_path: Path, expr: str) -> None:
    assert p1b.check(plan, stack(tmp_path, protect=expr))[0] == [
        f"{ATTACH_PROTECT}: policy_id must be exactly {PROTECT}.id in the source"
    ]


def test_a_rewritten_target_stops(plan: dict, tmp_path: Path) -> None:
    source = ATTACHMENT_SOURCE.replace(
        "target_id = aws_organizations_organizational_unit.workloads.id",
        'target_id = "r-ab12"', 1)  # fmt: skip
    assert p1b.check(plan, stack(tmp_path, source=source))[0] == [
        f"{ATTACH_BASELINE}: target_id must be exactly the Workloads OU id"
    ]


@pytest.mark.parametrize("extra", [
    "count = 1", 'for_each = toset(["a"])', "provider = aws.other",
    "lifecycle {{\n    create_before_destroy = true\n  }}",  # braces escaped for str.format
])  # fmt: skip
def test_an_attachment_with_extra_arguments_stops(plan: dict, tmp_path: Path, extra: str) -> None:
    source = ATTACHMENT_SOURCE.replace(
        "  policy_id = {protect}", f"  {extra}\n  policy_id = {{protect}}"
    )
    assert p1b.check(plan, stack(tmp_path, source=source))[0] == [
        f"{ATTACH_PROTECT}: may set only policy_id and target_id in the source"
    ]


def test_a_duplicate_or_missing_declaration_stops(plan: dict, tmp_path: Path) -> None:
    stack(tmp_path)
    (tmp_path / "more.tf").write_text(ATTACHMENT_SOURCE.split("\n\n")[1].format(
        protect=f"{PROTECT}.id"))  # fmt: skip
    assert p1b.check(plan, tmp_path)[0] == [
        f"{ATTACH_PROTECT}: must be declared exactly once in the source"
    ]
    (tmp_path / "more.tf").unlink()
    (tmp_path / "scps.tf").write_text("")
    assert p1b.check(plan, tmp_path)[0] == [
        f"{ATTACH_BASELINE}: must be declared exactly once in the source",
        f"{ATTACH_PROTECT}: must be declared exactly once in the source",
    ]


def test_an_extra_attachment_in_the_source_stops(plan: dict, tmp_path: Path) -> None:
    stack(tmp_path)
    (tmp_path / "root.tf").write_text(
        'resource "aws_organizations_policy_attachment" "root" {\n'
        f"  policy_id = {BASELINE}.id\n  target_id = \"r-ab12\"\n}}\n")  # fmt: skip
    assert p1b.check(plan, tmp_path)[0] == [
        "unexpected attachment in the source: aws_organizations_policy_attachment.root"
    ]


def test_an_unreadable_source_stops(plan: dict, tmp_path: Path) -> None:
    (tmp_path / "scps.tf").write_text('resource "x" {\n  policy_id = (\n')
    found = p1b.check(plan, tmp_path)[0]
    assert len(found) == 1 and found[0].startswith(f"cannot read the attachments from {tmp_path}")


def test_the_real_source_passes(plan: dict) -> None:
    assert p1b.check_source(ROOT / "infra" / "org") == []


# --- F1, third report (review of c82197a): files Terraform loads that the check did not read -----
# Terraform loads *.tf and *.tf.json, and merges override.tf(.json) and *_override.tf(.json) over
# them. The check read only *.tf, so a JSON override could replace the reviewed expression while the
# plan still showed only the direct references and an unknown value. infra/org uses neither JSON
# configuration nor overrides, so the check refuses both rather than parse and merge them.

REVIEWED_JSON_OVERRIDE = json.dumps({"resource": {"aws_organizations_policy_attachment": {
    "workloads_protect": {"policy_id":
        f'${{replace({PROTECT}.id, "/^.*$/", "p-FullAWSAccess")}}'}}}})  # fmt: skip


def test_terraform_honours_a_json_override_invisibly() -> None:
    """The real offline plan (tests/fixtures/terraform_plans/json_override): the override is in
    effect, yet plan JSON shows exactly the direct references and an unknown value."""
    real = json.loads((ROOT / "tests/fixtures/terraform_plans/json_override/synthetic-plan.json")
                      .read_text())  # fmt: skip
    config = next(r for r in real["configuration"]["root_module"]["resources"]
                  if r["address"] == "terraform_data.attachment")  # fmt: skip
    change_ = next(r for r in real["resource_changes"]
                   if r["address"] == "terraform_data.attachment")["change"]  # fmt: skip
    assert set(config["expressions"]["input"]["references"]) == {
        "terraform_data.reviewed_policy.id", "terraform_data.reviewed_policy"}  # fmt: skip
    assert change_["after_unknown"]["input"] is True


@pytest.mark.parametrize("name", ["override.tf.json", "scps_override.tf.json",
                                  "override.tf", "scps_override.tf"])  # fmt: skip
def test_an_override_file_stops(plan: dict, tmp_path: Path, name: str) -> None:
    stack(tmp_path)
    if name.endswith(".json"):
        (tmp_path / name).write_text(REVIEWED_JSON_OVERRIDE)
    else:
        (tmp_path / name).write_text(
            'resource "aws_organizations_policy_attachment" "workloads_protect" {\n'
            f'  policy_id = replace({PROTECT}.id, "/^.*$/", "p-FullAWSAccess")\n}}\n')  # fmt: skip
    assert f"override files are not allowed in the stack: {name}" in p1b.check(plan, tmp_path)[0]


def test_json_configuration_stops(plan: dict, tmp_path: Path) -> None:
    """A plain *.tf.json file could declare or reconfigure resources the check would not read."""
    stack(tmp_path)
    (tmp_path / "extra.tf.json").write_text(json.dumps({"output": {"x": {"value": "y"}}}))
    assert p1b.check(plan, tmp_path)[0] == [
        "JSON configuration is not allowed in the stack: extra.tf.json"
    ]


@pytest.mark.parametrize("name", [".override.tf.json", "override.tf.json~", "#override.tf#",
                                  "override.tf.json.bak", "notes.json"])  # fmt: skip
def test_files_terraform_ignores_are_ignored(plan: dict, tmp_path: Path, name: str) -> None:
    stack(tmp_path)
    (tmp_path / name).write_text(REVIEWED_JSON_OVERRIDE)
    assert p1b.check(plan, tmp_path)[0] == []


def test_module_calls_stop(plan: dict) -> None:
    """A module's configuration lives outside the stack directory the check reads."""
    plan["configuration"]["root_module"]["module_calls"] = {"policies": {"source": "./policies"}}
    assert "module calls are not allowed in the stack: policies" in p1b.check(plan)[0]


def test_the_real_stack_has_no_json_or_override_files() -> None:
    names = [p.name for p in (ROOT / "infra" / "org").iterdir()]
    assert not [n for n in names if n.endswith(".tf.json") or p1b.is_override(n)]


# --- the organization's own account emails (2026-10-06) ------------------------------------------
# The second phase 1b account-only apply failed EMAIL_ALREADY_EXISTS: the account email was the
# management account's own. The organization data source in the plan's prior state lists every
# account's email, so the check refuses a match, case-insensitively, and fails closed without them.


ORG_DATA = "data.aws_organizations_organization.this"


def org_data(plan: dict) -> dict:
    resources = plan["prior_state"]["values"]["root_module"]["resources"]
    return next(r for r in resources if r["address"] == ORG_DATA)["values"]


def use_email(plan: dict, email: str) -> None:
    plan["variables"]["workload_account_email"]["value"] = email
    change(plan, p1b.ACCOUNT)["after"]["email"] = email


@pytest.mark.parametrize("email", [MANAGEMENT_EMAIL, MANAGEMENT_EMAIL.lower(),
                                   MANAGEMENT_EMAIL.upper()])  # fmt: skip
def test_the_management_account_email_stops(plan: dict, email: str) -> None:
    use_email(plan, email)
    assert problems(plan) == [f"{p1b.ACCOUNT}: email is already an organization account's email"]


def test_a_member_account_email_stops(plan: dict) -> None:
    member = {"email": "Member@Example.com", "name": "member"}
    org_data(plan)["accounts"].append(member)
    org_data(plan)["non_master_accounts"].append(member)
    use_email(plan, "member@example.com")
    assert problems(plan) == [f"{p1b.ACCOUNT}: email is already an organization account's email"]


@pytest.mark.parametrize("missing", ["accounts", "master_account_email", "values", "data source"])
def test_unavailable_organization_emails_fail_closed(plan: dict, missing: str) -> None:
    resources = plan["prior_state"]["values"]["root_module"]["resources"]
    if missing == "data source":
        resources[:] = [r for r in resources if r["address"] != ORG_DATA]
    elif missing == "values":
        next(r for r in resources if r["address"] == ORG_DATA)["values"] = {}
    else:
        del org_data(plan)[missing]
    assert f"{p1b.ACCOUNT}: the organization's account emails are unavailable" in problems(plan)


@pytest.mark.parametrize("accounts", [
    [], [{"name": "no email"}], [{"email": ""}], None,
    [{"email": MANAGEMENT_EMAIL}, {"name": "no email"}]])  # fmt: skip
def test_an_empty_or_emailless_account_list_fails_closed(plan: dict, accounts: Any) -> None:
    org_data(plan)["accounts"] = accounts
    assert f"{p1b.ACCOUNT}: the organization's account emails are unavailable" in problems(plan)


def test_a_new_email_outside_the_organization_passes(plan: dict) -> None:
    use_email(plan, "brand-new@example.org")
    assert problems(plan) == []
