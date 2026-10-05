"""ADR-0021 phase 1a: the ownership transfer, in source and in the two saved plans.

The plans are synthetic but shaped like `terraform show -json` (Terraform 1.15.8): imports carry
`change.importing`, `removed { destroy = false }` shows as the action `forget`.
"""

import copy
from pathlib import Path
from typing import Any

import hcl2
import org_plan_check as opc
import pytest
from hcl2.utils import SerializationOptions

ROOT = Path(__file__).parents[1]
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


@pytest.fixture
def org_plan() -> dict:
    return {
        "complete": True,
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_caller_identity.current", "mode": "data", "values": {}},
        ]}}},
        "configuration": {"root_module": {"resources": [
            {"address": a, "type": a.split(".")[0]} for a in [*sorted(opc.MOVED), opc.OU]
        ]}},
        "resource_changes": [
            rc("data.aws_organizations_organization.this", ["read"], None, {}),
            *[imported(a) for a in sorted(opc.MOVED)],
            rc(opc.OU, ["create"], None, {"name": "Workloads", "parent_id": "r-ab12"}),
        ],
        "output_changes": {"workloads_ou_id": {"actions": ["create"]}},
    }  # fmt: skip


@pytest.fixture
def bootstrap_plan() -> dict:
    return {
        "complete": True,
        "resource_changes": [
            rc("aws_iam_role.gha_deploy", ["no-op"], {"name": "d"}, {"name": "d"}),
            *[rc(a, ["forget"], {"x": 1}, None) for a in sorted(opc.MOVED)],
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


def test_an_import_of_another_object_stops(org_plan: dict) -> None:
    change(org_plan, "aws_s3_bucket_policy.tfstate")["change"]["importing"]["id"] = "other-bucket"
    assert opc.check_org(org_plan)[0] == [
        "aws_s3_bucket_policy.tfstate: import ID is not the expected resource"
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


def test_a_non_fresh_org_state_stops(org_plan: dict) -> None:
    org_plan["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_s3_bucket.tfstate", "mode": "managed", "values": {}}
    )
    assert "infra/org state is not fresh: 1 managed resources" in opc.check_org(org_plan)[0]


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


def test_the_cli_exit_codes(tmp_path: Path, org_plan: dict) -> None:
    import json

    good = tmp_path / "org.json"
    good.write_text(json.dumps(org_plan))
    bad_plan = copy.deepcopy(org_plan)
    bad_plan["errored"] = True
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_plan))
    assert opc.main(["org", str(good)]) == 0
    assert opc.main(["org", str(bad)]) == 1
    assert opc.main(["bootstrap", str(good)]) == 1  # the wrong plan for the mode
    assert opc.main(["batch", str(good)]) == 2


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
    org = load("org")
    assert {target(b["to"]) for b in org["import"]} == opc.MOVED
    assert addresses(org) >= opc.MOVED


def test_bootstrap_forgets_exactly_them_without_destroying() -> None:
    boot = load("bootstrap")
    assert {target(b["from"]) for b in boot["removed"]} == opc.MOVED
    assert all(
        b["lifecycle"] == [{"destroy": False, "__is_block__": True}] for b in boot["removed"]
    )
    assert not opc.MOVED & addresses(boot)
    assert "import" not in boot


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
