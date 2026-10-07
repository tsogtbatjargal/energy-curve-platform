"""ADR-0021 phase 1a: the ownership transfer, in source and in the two saved plans.

The plans are synthetic but shaped like `terraform show -json` (Terraform 1.15.8): imports carry
`change.importing`, `removed { destroy = false }` shows as the action `forget`.
"""

import copy
import json
from pathlib import Path
from typing import Any

import hcl2
import org_plan_check as opc
import pytest
from hcl2.utils import SerializationOptions

ROOT = Path(__file__).parents[1]
REAL_PLANS = ROOT / "tests" / "fixtures" / "terraform_plans"
ACCOUNT = "123456789012"
BUCKET = f"ecp-tfstate-{ACCOUNT}-ca-central-1"


def rc(address: str, actions: list[str], before: Any, after: Any, **extra: Any) -> dict:
    change = {"actions": actions, "before": before, "after": after, "after_unknown": {}, **extra}
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "change": change}  # fmt: skip


def imported(address: str) -> dict:
    if address == "aws_ce_cost_allocation_tag.project":
        values, import_id = {"tag_key": "project", "status": "Active"}, "project"
    elif address == "aws_budgets_budget.project":
        values = {"account_id": ACCOUNT, "name": "energy-curve-platform", "limit_amount": "40.0"}
        import_id = f"{ACCOUNT}:energy-curve-platform"
    else:
        values, import_id = {"bucket": BUCKET}, BUCKET
    return rc(address, ["no-op"], values, dict(values), importing={"id": import_id})


def real_plan(name: str) -> dict:
    return json.loads((REAL_PLANS / name / "synthetic-plan.json").read_text())


@pytest.fixture
def org_plan() -> dict:
    imports = [imported(a) for a in sorted(opc.MOVED)]
    # As Terraform 1.15.8 plans it (tests/fixtures/terraform_plans/pending_import): resources being
    # imported are listed in prior_state although the destination state is empty.
    pending = [
        {"address": i["address"], "mode": "managed", "values": i["change"]["before"]}
        for i in imports
    ]
    return {
        "complete": True,
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_caller_identity.current", "mode": "data",
             "values": {"account_id": ACCOUNT}},
            *pending,
        ]}}},
        "configuration": {"root_module": {"resources": [
            {"address": a, "type": a.split(".")[0]} for a in [*sorted(opc.MOVED), opc.OU]
        ]}},
        "resource_changes": [
            rc("data.aws_organizations_organization.this", ["read"], None, {}),
            *imports,
            rc(opc.OU, ["create"], None, {"name": "Workloads", "parent_id": "r-ab12"}),
        ],
        "output_changes": {"workloads_ou_id": {"actions": ["create"]}},
    }  # fmt: skip


@pytest.fixture
def bootstrap_plan() -> dict:
    # As tests/fixtures/terraform_plans/forget: `before` holds the object bootstrap relinquishes.
    return {
        "complete": True,
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_caller_identity.current", "mode": "data",
             "values": {"account_id": ACCOUNT}},
        ]}}},
        "resource_changes": [
            rc("aws_iam_role.gha_deploy", ["no-op"], {"name": "d"}, {"name": "d"}),
            *[rc(a, ["forget"], imported(a)["change"]["before"], None) for a in sorted(opc.MOVED)],
        ],
        "output_changes": {"state_bucket": {"actions": ["no-op"]}},
    }  # fmt: skip


def change(plan: dict, address: str) -> dict:
    return next(r for r in plan["resource_changes"] if r["address"] == address)


# --- the org plan ------------------------------------------------------------------------------


def test_the_reviewed_org_plan_passes(org_plan: dict) -> None:
    problems, report = opc.check_org(org_plan)
    assert problems == []
    assert report[-1] == "summary: 9 to import, 1 to add, 0 to change, 0 to destroy"


def test_an_import_that_would_change_the_resource_stops(org_plan: dict) -> None:
    budget = change(org_plan, "aws_budgets_budget.project")["change"]
    budget["actions"], budget["after"]["limit_amount"] = ["update"], "50.0"
    assert any("not a plain import" in p for p in opc.check_org(org_plan)[0])


def test_retagging_on_import_stops(org_plan: dict) -> None:
    bucket = change(org_plan, "aws_s3_bucket.tfstate")["change"]
    bucket["before"]["tags_all"] = {"stack": "bootstrap"}
    bucket["after"]["tags_all"] = {"stack": "org"}
    assert opc.check_org(org_plan)[0] == [
        "aws_s3_bucket.tfstate: the import is not a plain import (['tags_all'])"
    ]


def test_a_missing_or_extra_import_stops(org_plan: dict) -> None:
    tag = change(org_plan, "aws_ce_cost_allocation_tag.project")
    org_plan["resource_changes"].remove(tag)
    org_plan["resource_changes"].append(imported("aws_s3_bucket.other"))
    problems = opc.check_org(org_plan)[0]
    assert "missing import: aws_ce_cost_allocation_tag.project" in problems
    assert "unexpected import: aws_s3_bucket.other" in problems


def test_an_import_of_another_object_stops(org_plan: dict, bootstrap_plan: dict) -> None:
    change(org_plan, "aws_s3_bucket_policy.tfstate")["change"]["importing"]["id"] = "other-bucket"
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        "aws_s3_bucket_policy.tfstate: imports another object than bootstrap relinquishes"
    ]


def test_creating_a_moved_resource_instead_of_importing_it_stops(org_plan: dict) -> None:
    tag = change(org_plan, "aws_ce_cost_allocation_tag.project")["change"]
    del tag["importing"]
    tag["actions"], tag["before"] = ["create"], None
    problems = opc.check_org(org_plan)[0]
    assert "unexpected change: create aws_ce_cost_allocation_tag.project" in problems
    assert "missing import: aws_ce_cost_allocation_tag.project" in problems


@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"], ["forget"]])
def test_any_other_change_stops(org_plan: dict, actions: list[str]) -> None:
    org_plan["resource_changes"].append(rc("aws_s3_bucket.extra", actions, {}, {}))
    assert (
        f"unexpected change: {'+'.join(actions)} aws_s3_bucket.extra" in opc.check_org(org_plan)[0]
    )


def test_the_ou_must_be_workloads_under_the_root(org_plan: dict) -> None:
    ou = change(org_plan, opc.OU)["change"]
    ou["after"] = {"name": "Sandbox", "parent_id": "ou-ab12-cdefgh"}
    problems = opc.check_org(org_plan)[0]
    assert f"{opc.OU}: name must be Workloads" in problems
    assert f"{opc.OU}: parent must be the organization root, known at plan time" in problems


@pytest.mark.parametrize(
    "type_", ["aws_organizations_account", "aws_organizations_policy_attachment",
              "aws_ssoadmin_permission_set_inline_policy", "aws_iam_organizations_features"],
)  # fmt: skip
def test_later_phase_resources_stop(org_plan: dict, type_: str) -> None:
    org_plan["configuration"]["root_module"]["resources"].append({"type": type_})
    org_plan["resource_changes"].append(rc(f"{type_}.x", ["create"], None, {}))
    problems = opc.check_org(org_plan)[0]
    assert f"later-phase resource type in the configuration: {type_}" in problems
    assert f"later-phase resource in the plan: {type_}.x" in problems


def test_a_resource_the_org_state_already_owns_stops(org_plan: dict) -> None:
    org_plan["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_s3_bucket.leftover", "mode": "managed", "values": {}}
    )
    org_plan["resource_changes"].append(rc("aws_s3_bucket.leftover", ["no-op"], {}, {}))
    assert opc.check_org(org_plan)[0] == ["infra/org state already owns: aws_s3_bucket.leftover"]


def test_an_already_imported_moved_resource_stops(org_plan: dict) -> None:
    """Imported by an earlier apply: in state, so this plan has no import for it."""
    del change(org_plan, "aws_s3_bucket.tfstate")["change"]["importing"]
    problems = opc.check_org(org_plan)[0]
    assert "infra/org state already owns: aws_s3_bucket.tfstate" in problems
    assert "missing import: aws_s3_bucket.tfstate" in problems


# F1 (review of b65ab7b): real offline plans, Terraform 1.15.8.


def test_a_real_pending_import_is_not_existing_ownership() -> None:
    problems = opc.check_org(real_plan("pending_import"))[0]
    assert not [p for p in problems if "already owns" in p or "not fresh" in p]


def test_a_real_owned_resource_still_stops() -> None:
    problems = opc.check_org(real_plan("owned_and_import"))[0]
    assert "infra/org state already owns: terraform_data.existing" in problems
    assert not [p for p in problems if "terraform_data.imported" in p and "owns" in p]


@pytest.mark.parametrize("key", ["resource_drift", "deferred_changes"])
def test_drift_or_deferral_stops(org_plan: dict, key: str) -> None:
    org_plan[key] = [{"address": "aws_s3_bucket.tfstate"}]
    assert opc.check_org(org_plan)[0] == [f"{key}: aws_s3_bucket.tfstate"]


def test_an_errored_or_incomplete_plan_stops(org_plan: dict) -> None:
    org_plan["errored"], org_plan["complete"] = True, False
    assert opc.check_org(org_plan)[0][:2] == ["the plan errored", "the plan is incomplete"]


# --- the bootstrap plan ------------------------------------------------------------------------


def test_the_reviewed_bootstrap_plan_passes(bootstrap_plan: dict) -> None:
    problems, report = opc.check_bootstrap(bootstrap_plan)
    assert problems == []
    assert report[-1] == "summary: 0 to add, 0 to change, 0 to destroy, 9 forgotten"


@pytest.mark.parametrize("actions", [["delete"], ["no-op"], ["update"]])
def test_a_moved_resource_must_be_forgotten_not_destroyed_or_kept(
    bootstrap_plan: dict, actions: list[str]
) -> None:
    change(bootstrap_plan, "aws_ce_cost_allocation_tag.project")["change"]["actions"] = actions
    problems = opc.check_bootstrap(bootstrap_plan)[0]
    assert "missing forget: aws_ce_cost_allocation_tag.project" in problems


def test_any_other_bootstrap_change_stops(bootstrap_plan: dict) -> None:
    change(bootstrap_plan, "aws_iam_role.gha_deploy")["change"]["actions"] = ["update"]
    bootstrap_plan["resource_changes"].append(rc("aws_iam_policy.x", ["forget"], {}, None))
    bootstrap_plan["output_changes"]["state_bucket"]["actions"] = ["update"]
    assert opc.check_bootstrap(bootstrap_plan)[0] == [
        "unexpected change: update aws_iam_role.gha_deploy",
        "unexpected change: forget aws_iam_policy.x",
        "unexpected output change: update state_bucket",
    ]


def test_the_cli_exit_codes(tmp_path: Path, org_plan: dict, bootstrap_plan: dict) -> None:
    good, boot = tmp_path / "org.json", tmp_path / "bootstrap.json"
    good.write_text(json.dumps(org_plan))
    boot.write_text(json.dumps(bootstrap_plan))
    bad_plan = copy.deepcopy(org_plan)
    bad_plan["errored"] = True
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_plan))
    assert opc.main([str(good), str(boot)]) == 0
    assert opc.main([str(bad), str(boot)]) == 1
    assert opc.main([str(good), str(good)]) == 1  # the org plan in the bootstrap slot
    assert opc.main([str(good)]) == 2


# --- the source: one owner for each resource ---------------------------------------------------


def load(stack: str) -> dict[str, list]:
    merged: dict[str, list] = {}
    for tf in sorted((ROOT / "infra" / stack).glob("*.tf")):
        with tf.open() as fh:
            doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
        for key, blocks in doc.items():
            merged.setdefault(key, []).extend(blocks)
    return merged


def addresses(doc: dict[str, list]) -> set[str]:
    return {
        f"{t.strip(chr(34))}.{n.strip(chr(34))}"
        for block in doc.get("resource", [])
        for t, named in block.items()
        for n in named
    }


def target(ref: str) -> str:
    return ref.removeprefix("${").removesuffix("}")


def test_org_imports_exactly_the_moved_resources_and_declares_them() -> None:
    """Phase 1a's nine, plus the organization itself from phase 1b (to enable SCPs)."""
    org = load("org")
    imports = {target(b["to"]) for b in org["import"]}
    assert imports == opc.MOVED | {"aws_organizations_organization.this"}
    assert addresses(org) >= opc.MOVED


def test_bootstrap_forgets_exactly_them_without_destroying() -> None:
    boot = load("bootstrap")
    assert {target(b["from"]) for b in boot["removed"]} == opc.MOVED
    assert all(
        b["lifecycle"] == [{"destroy": False, "__is_block__": True}] for b in boot["removed"]
    )
    assert not opc.MOVED & addresses(boot)
    # Phase 3 adds exactly one import: the break-glass role, in the member instance only
    # (ADR-0021). Nothing may re-import what infra/org owns.
    imports = boot.get("import", [])
    assert [(b["to"], b["id"], b["for_each"]) for b in imports] == [
        ("${aws_iam_role.break_glass[each.key]}", "${each.key}", "${local.break_glass}")
    ]
    assert not {target(b["to"]) for b in imports} & opc.MOVED


def test_no_stack_but_org_declares_a_budget_or_cost_allocation_tag() -> None:
    org_only = {"aws_budgets_budget", "aws_ce_cost_allocation_tag"}
    for stack in sorted(p.name for p in (ROOT / "infra").iterdir() if p.is_dir()):
        types = {a.split(".")[0] for a in addresses(load(stack))}
        assert (types & org_only == org_only) if stack == "org" else not types & org_only, stack


def test_the_tag_and_budget_cannot_be_destroyed_from_org() -> None:
    for block in load("org")["resource"]:
        for t, named in block.items():
            if t.strip('"') in {
                "aws_budgets_budget",
                "aws_ce_cost_allocation_tag",
                "aws_s3_bucket",
            }:
                (body,) = named.values()
                assert body["lifecycle"][0]["prevent_destroy"] is True, t


# --- F2 (review of b65ab7b): imports must be the objects bootstrap relinquishes ---------------

OTHER_BUCKET = "unrelated-review-synthetic-bucket"
OTHER_BUDGET = "unrelated-review-synthetic-budget"
OTHER_ACCOUNT = "210987654321"
S3_MOVED = sorted(a for a in opc.MOVED if a.startswith("aws_s3_"))


def retarget(plan: dict, address: str, **values: str) -> None:
    """Point an import at another object, self-consistently: before, after and the import ID."""
    c = change(plan, address)["change"]
    c["before"].update(values)
    c["after"].update(values)
    after = c["after"]
    c["importing"]["id"] = opc.import_id(address, opc.identity(address, after))


def test_the_correct_transfer_passes(org_plan: dict, bootstrap_plan: dict) -> None:
    problems, report = opc.check_transfer(org_plan, bootstrap_plan)
    assert problems == []
    assert report[-1] == "transfer: all 9 imports are the objects bootstrap relinquishes"


def test_a_self_consistent_import_of_another_bucket_stops(
    org_plan: dict, bootstrap_plan: dict
) -> None:
    for address in S3_MOVED:
        retarget(org_plan, address, bucket=OTHER_BUCKET)
    assert opc.check_org(org_plan)[0] == []  # internally consistent, so the shape check passes
    problems = opc.check_transfer(org_plan, bootstrap_plan)[0]
    assert problems == [
        f"{a}: imports another object than bootstrap relinquishes" for a in S3_MOVED
    ]


def test_a_self_consistent_import_of_another_budget_stops(
    org_plan: dict, bootstrap_plan: dict
) -> None:
    retarget(org_plan, "aws_budgets_budget.project", name=OTHER_BUDGET)
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        "aws_budgets_budget.project: imports another object than bootstrap relinquishes"
    ]


def test_a_self_consistent_import_from_another_account_stops(
    org_plan: dict, bootstrap_plan: dict
) -> None:
    retarget(org_plan, "aws_budgets_budget.project", account_id=OTHER_ACCOUNT)
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        "aws_budgets_budget.project: imports another object than bootstrap relinquishes"
    ]


def test_plans_for_different_accounts_stop(org_plan: dict, bootstrap_plan: dict) -> None:
    identity = org_plan["prior_state"]["values"]["root_module"]["resources"][0]
    identity["values"]["account_id"] = OTHER_ACCOUNT
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        "the org and bootstrap plans are not for the same account"
    ]


def test_relinquished_objects_must_belong_to_the_account(
    org_plan: dict, bootstrap_plan: dict
) -> None:
    """Both plans agree, but on a budget and bucket of another account: still a stop."""
    for plan in (org_plan, bootstrap_plan):
        plan["prior_state"]["values"]["root_module"]["resources"][0]["values"]["account_id"] = (
            OTHER_ACCOUNT
        )
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        "aws_budgets_budget.project: the budget is not in the plans' account",
        "aws_s3_bucket.tfstate: the bucket is not the account's state bucket",
    ]


def test_an_import_bootstrap_does_not_relinquish_stops(
    org_plan: dict, bootstrap_plan: dict
) -> None:
    tag = change(bootstrap_plan, "aws_ce_cost_allocation_tag.project")
    bootstrap_plan["resource_changes"].remove(tag)
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        "aws_ce_cost_allocation_tag.project: bootstrap does not relinquish it"
    ]


def test_the_cli_needs_both_plans(tmp_path: Path, org_plan: dict, bootstrap_plan: dict) -> None:
    org, boot = tmp_path / "org.json", tmp_path / "bootstrap.json"
    org.write_text(json.dumps(org_plan))
    boot.write_text(json.dumps(bootstrap_plan))
    assert opc.main([str(org), str(boot)]) == 0
    retarget(org_plan, "aws_budgets_budget.project", name=OTHER_BUDGET)
    org.write_text(json.dumps(org_plan))
    assert opc.main([str(org), str(boot)]) == 1
    assert opc.main([str(boot), str(org)]) == 1  # swapped
    assert opc.main(["org", str(org)]) == 2  # the org plan alone is not accepted


@pytest.mark.parametrize("side", ["before", "after"])
def test_the_right_import_id_with_another_object_stops(
    org_plan: dict, bootstrap_plan: dict, side: str
) -> None:
    change(org_plan, "aws_s3_bucket_versioning.tfstate")["change"][side]["bucket"] = OTHER_BUCKET
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        "aws_s3_bucket_versioning.tfstate: imports another object than bootstrap relinquishes"
    ]


# --- the budget import's sensitivity-only update (real org plan, 2026-10-05) -------------------
# Terraform 1.15.8 plans the budget import as "update": every value is identical, but the import
# reads the alert emails from AWS unmarked while the configuration's sensitive variable marks the
# whole `notification` set. Terraform records such a mark-only update in state without calling the
# provider (internal/terraform/node_resource_abstract_instance.go). Only this exact case passes.

BUDGET = "aws_budgets_budget.project"
NOTIFICATION = {"subscriber_email_addresses": ["alerts@example.com"],
                "subscriber_sns_topic_arns": [], "threshold": 50}  # fmt: skip
UNMARKED = [{"subscriber_email_addresses": [False], "subscriber_sns_topic_arns": []}] * 4


def marks_only(plan: dict, address: str = BUDGET) -> dict:
    """Turn an import into the real mark-only update: values equal, notification now sensitive."""
    c = change(plan, address)["change"]
    c["actions"] = ["update"]
    c["before"]["notification"] = [dict(NOTIFICATION)] * 4
    c["after"]["notification"] = [dict(NOTIFICATION)] * 4
    common = {"cost_filter": [{"values": [False]}], "tags": {}, "tags_all": {}}
    c["before_sensitive"] = {**common, "notification": copy.deepcopy(UNMARKED)}
    c["after_sensitive"] = {**common, "notification": True}
    return c


def test_the_budget_sensitivity_only_update_passes_as_state_only(org_plan: dict) -> None:
    marks_only(org_plan)
    problems, report = opc.check_org(org_plan)
    assert problems == []
    assert (
        f"import+state-only {BUDGET}  sensitivity marks only: notification "
        "(values identical; recorded in state, no AWS call)"
    ) in report
    assert report[-1] == (
        "summary: 9 to import, 1 to add, 1 to change (state-only: sensitivity marks on the "
        "imported budget), 0 to destroy"
    )


def test_a_budget_value_change_with_the_marks_stops(org_plan: dict) -> None:
    marks_only(org_plan)["after"]["limit_amount"] = "50.0"
    assert opc.check_org(org_plan)[0] == [
        f"{BUDGET}: the import is not a plain import (['limit_amount'])"
    ]


def test_a_budget_email_change_with_the_marks_stops(org_plan: dict) -> None:
    c = marks_only(org_plan)
    c["after"]["notification"] = [dict(NOTIFICATION, subscriber_email_addresses=["x@example.com"])]
    assert opc.check_org(org_plan)[0] == [
        f"{BUDGET}: the import is not a plain import (['notification'])"
    ]


def test_marks_removed_from_the_emails_stop(org_plan: dict) -> None:
    c = marks_only(org_plan)
    c["before_sensitive"]["notification"], c["after_sensitive"]["notification"] = True, False
    assert opc.check_org(org_plan)[0] == [f"{BUDGET}: the import is not a plain import ([])"]


def test_a_mark_change_on_another_budget_attribute_stops(org_plan: dict) -> None:
    marks_only(org_plan)["after_sensitive"]["cost_filter"] = True
    assert opc.check_org(org_plan)[0] == [f"{BUDGET}: the import is not a plain import ([])"]


def test_a_mark_only_update_on_another_import_stops(org_plan: dict) -> None:
    c = change(org_plan, "aws_s3_bucket_policy.tfstate")["change"]
    c["actions"] = ["update"]  # the budget's exact mark pattern, on another import
    c["before_sensitive"], c["after_sensitive"] = {"notification": []}, {"notification": True}
    assert opc.check_org(org_plan)[0] == [
        "aws_s3_bucket_policy.tfstate: the import is not a plain import ([])"
    ]


def test_a_mark_only_update_with_unknown_values_stops(org_plan: dict) -> None:
    marks_only(org_plan)["after_unknown"] = {"notification": True}
    assert opc.check_org(org_plan)[0] == [
        f"{BUDGET}: the import is not a plain import (['notification'])"
    ]


def test_the_transfer_identity_check_still_applies_to_the_marked_budget(
    org_plan: dict, bootstrap_plan: dict
) -> None:
    marks_only(org_plan)
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == []
    retarget(org_plan, BUDGET, name=OTHER_BUDGET)
    assert opc.check_transfer(org_plan, bootstrap_plan)[0] == [
        f"{BUDGET}: imports another object than bootstrap relinquishes"
    ]


@pytest.mark.parametrize("actions", [["delete", "create"], ["create", "delete"]])
def test_a_budget_replacement_with_the_marks_stops(org_plan: dict, actions: list[str]) -> None:
    marks_only(org_plan)["actions"] = actions
    assert opc.check_org(org_plan)[0] == [f"{BUDGET}: the import is not a plain import ([])"]


# --- refresh drift in the bootstrap plan (real bootstrap plan, 2026-10-05) ---------------------
# Two drift entries, both planned no-op: the boundary policy's tags read back as {} where state had
# null, and the deploy role's stored copy of its inline policy predates the R2 apply; the refreshed
# copy equals aws_iam_role_policy.gha_deploy_iam. Only these exact cases pass.

ROLE, ROLE_POLICY = "aws_iam_role.gha_deploy", "aws_iam_role_policy.gha_deploy_iam"
STATE_READ, STATE_READ_NAME = "aws_iam_role_policy.gha_deploy_state_read", "terraform-state-read"
BOUNDARY_POLICY = "aws_iam_policy.workload_boundary"
INLINE = "ecp-scoped-iam-and-state"
R2_POLICY = {"Version": "2012-10-17", "Statement": [
    {"Sid": "RolesOnlyWithTheBoundary", "Effect": "Deny", "Action": "iam:CreateRole",
     "Resource": "*"}]}  # fmt: skip
OLD_POLICY = {"Version": "2012-10-17", "Statement": [
    {"Sid": "ScopedIam", "Effect": "Allow", "Action": "iam:CreateRole",
     "Resource": "*"}]}  # fmt: skip


def drift(address: str, before: dict, after: dict) -> dict:
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "change": {"actions": ["update"], "before": before, "after": after}}  # fmt: skip


STATE_READ_POLICY = {"Version": "2012-10-17", "Statement": [
    {"Sid": "ReadState", "Effect": "Allow", "Action": "s3:GetObject",
     "Resource": "*"}]}  # fmt: skip


def inline(policy: dict, name: str = INLINE, state_read: bool = True) -> list[dict]:
    """The deploy role's inline policies, as the real plan lists them: this one, then the
    unchanged terraform-state-read policy."""
    entries = [{"name": name, "policy": json.dumps(policy)}]
    if state_read:
        entries.append({"name": STATE_READ_NAME, "policy": json.dumps(STATE_READ_POLICY)})
    return entries


def with_real_drift(plan: dict) -> dict:
    """The bootstrap plan's two real drift entries, with the resources they refer to."""
    plan["prior_state"]["values"]["root_module"]["resources"] += [
        {"address": ROLE_POLICY, "mode": "managed",
         "values": {"name": INLINE, "policy": json.dumps(R2_POLICY)}},
        {"address": STATE_READ, "mode": "managed",
         "values": {"name": STATE_READ_NAME, "policy": json.dumps(STATE_READ_POLICY)}},
    ]  # fmt: skip
    plan["resource_changes"] += [
        rc(ROLE_POLICY, ["no-op"], {"name": INLINE}, {"name": INLINE}),
        rc(STATE_READ, ["no-op"], {"name": STATE_READ_NAME}, {"name": STATE_READ_NAME}),
        rc(BOUNDARY_POLICY, ["no-op"], {"name": "b"}, {"name": "b"}),
    ]
    role = {"name": "ecp-gha-deploy", "max_session_duration": 3600}
    plan["resource_drift"] = [
        drift(BOUNDARY_POLICY, {"name": "b", "tags": None}, {"name": "b", "tags": {}}),
        drift(ROLE, {**role, "inline_policy": inline(OLD_POLICY)},
              {**role, "inline_policy": inline(R2_POLICY)}),
    ]  # fmt: skip
    return plan


def drift_item(plan: dict, address: str) -> dict:
    return next(d for d in plan["resource_drift"] if d["address"] == address)["change"]


def test_the_two_exact_refresh_cases_pass(bootstrap_plan: dict) -> None:
    problems, report = opc.check_bootstrap(with_real_drift(bootstrap_plan))
    assert problems == []
    assert f"drift (refresh only, planned no-op) {BOUNDARY_POLICY}: tags null -> {{}}" in report
    assert (
        f"drift (refresh only, planned no-op) {ROLE}: inline_policy {INLINE} now equals "
        f"{ROLE_POLICY}"
    ) in report


@pytest.mark.parametrize(
    ("before", "after"),
    [(None, {"k": "v"}), ({}, None), ({"k": "v"}, {}), (None, None)],
)
def test_other_boundary_tag_drift_stops(bootstrap_plan: dict, before: Any, after: Any) -> None:
    c = drift_item(with_real_drift(bootstrap_plan), BOUNDARY_POLICY)
    c["before"]["tags"], c["after"]["tags"] = before, after
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {BOUNDARY_POLICY}"]


def test_boundary_drift_beyond_tags_stops(bootstrap_plan: dict) -> None:
    drift_item(with_real_drift(bootstrap_plan), BOUNDARY_POLICY)["after"]["name"] = "other"
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {BOUNDARY_POLICY}"]


def test_a_refreshed_inline_policy_unlike_the_role_policy_stops(bootstrap_plan: dict) -> None:
    edited = {**R2_POLICY, "Statement": [*R2_POLICY["Statement"], *OLD_POLICY["Statement"]]}
    drift_item(with_real_drift(bootstrap_plan), ROLE)["after"]["inline_policy"] = inline(edited)
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {ROLE}"]


def test_another_inline_policy_name_stops(bootstrap_plan: dict) -> None:
    c = drift_item(with_real_drift(bootstrap_plan), ROLE)
    c["before"]["inline_policy"] = inline(OLD_POLICY, "other")
    c["after"]["inline_policy"] = inline(R2_POLICY, "other")
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {ROLE}"]


def test_an_extra_inline_policy_stops(bootstrap_plan: dict) -> None:
    c = drift_item(with_real_drift(bootstrap_plan), ROLE)
    c["after"]["inline_policy"] += inline(OLD_POLICY, "added-outside", state_read=False)
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {ROLE}"]


def test_a_missing_inline_policy_stops(bootstrap_plan: dict) -> None:
    c = drift_item(with_real_drift(bootstrap_plan), ROLE)
    c["after"]["inline_policy"] = inline(R2_POLICY, state_read=False)
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {ROLE}"]


def test_a_changed_state_read_policy_stops(bootstrap_plan: dict) -> None:
    c = drift_item(with_real_drift(bootstrap_plan), ROLE)
    c["after"]["inline_policy"][1]["policy"] = json.dumps(OLD_POLICY)
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {ROLE}"]


def test_a_state_read_policy_unlike_its_resource_stops(bootstrap_plan: dict) -> None:
    """Unchanged in the drift, but not what state holds for the role policy resource."""
    plan = with_real_drift(bootstrap_plan)
    for r in plan["prior_state"]["values"]["root_module"]["resources"]:
        if r["address"] == STATE_READ:
            r["values"]["policy"] = json.dumps(OLD_POLICY)
    assert opc.check_bootstrap(plan)[0] == [f"resource_drift: {ROLE}"]


def test_role_drift_when_the_state_read_policy_changes_stops(bootstrap_plan: dict) -> None:
    change(with_real_drift(bootstrap_plan), STATE_READ)["change"]["actions"] = ["update"]
    assert opc.check_bootstrap(bootstrap_plan)[0] == [
        f"resource_drift: {ROLE}",
        f"unexpected change: update {STATE_READ}",
    ]


def test_role_drift_beyond_the_inline_policy_stops(bootstrap_plan: dict) -> None:
    drift_item(with_real_drift(bootstrap_plan), ROLE)["after"]["max_session_duration"] = 43200
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {ROLE}"]


def test_role_drift_when_the_role_policy_itself_changes_stops(bootstrap_plan: dict) -> None:
    change(with_real_drift(bootstrap_plan), ROLE_POLICY)["change"]["actions"] = ["update"]
    assert opc.check_bootstrap(bootstrap_plan)[0] == [
        f"resource_drift: {ROLE}",
        f"unexpected change: update {ROLE_POLICY}",
    ]


@pytest.mark.parametrize("address", [ROLE, BOUNDARY_POLICY])
def test_drift_on_a_resource_with_a_planned_change_stops(
    bootstrap_plan: dict, address: str
) -> None:
    change(with_real_drift(bootstrap_plan), address)["change"]["actions"] = ["update"]
    assert opc.check_bootstrap(bootstrap_plan)[0] == [
        f"resource_drift: {address}",
        f"unexpected change: update {address}",
    ]


def test_drift_on_any_other_resource_stops(bootstrap_plan: dict) -> None:
    with_real_drift(bootstrap_plan)["resource_drift"].append(
        drift("aws_iam_role.gha_plan", {"tags": None}, {"tags": {}})
    )
    assert opc.check_bootstrap(bootstrap_plan)[0] == ["resource_drift: aws_iam_role.gha_plan"]


@pytest.mark.parametrize("actions", [["delete"], ["create"], ["no-op"]])
def test_a_drift_entry_that_is_not_an_update_stops(
    bootstrap_plan: dict, actions: list[str]
) -> None:
    drift_item(with_real_drift(bootstrap_plan), BOUNDARY_POLICY)["actions"] = actions
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"resource_drift: {BOUNDARY_POLICY}"]


def test_the_same_drift_in_the_org_plan_stops(org_plan: dict) -> None:
    org_plan["resource_changes"].append(rc(BOUNDARY_POLICY, ["no-op"], {}, {}))
    org_plan["resource_drift"] = [
        drift(BOUNDARY_POLICY, {"name": "b", "tags": None}, {"name": "b", "tags": {}})
    ]
    assert opc.check_org(org_plan)[0] == [f"resource_drift: {BOUNDARY_POLICY}"]


def test_deferred_changes_still_stop_the_bootstrap_plan(bootstrap_plan: dict) -> None:
    with_real_drift(bootstrap_plan)["deferred_changes"] = [{"address": ROLE}]
    assert opc.check_bootstrap(bootstrap_plan)[0] == [f"deferred_changes: {ROLE}"]


def test_a_changed_state_read_policy_stops_even_when_state_agrees(bootstrap_plan: dict) -> None:
    """Only ecp-scoped-iam-and-state may change, whatever the role-policy resource holds."""
    plan = with_real_drift(bootstrap_plan)
    drift_item(plan, ROLE)["after"]["inline_policy"][1]["policy"] = json.dumps(OLD_POLICY)
    for r in plan["prior_state"]["values"]["root_module"]["resources"]:
        if r["address"] == STATE_READ:
            r["values"]["policy"] = json.dumps(OLD_POLICY)
    assert opc.check_bootstrap(plan)[0] == [f"resource_drift: {ROLE}"]
