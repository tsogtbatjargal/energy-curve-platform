"""ADR-0021 phase 5: the management bootstrap's destroy plan must delete exactly the reviewed set.

The plan is synthetic but shaped like `terraform show -json` of `terraform plan -destroy`
(Terraform 1.15.8, probed offline with terraform_data): each delete has `before` values and a
null `after`, every output is deleted, `prior_state` holds the data sources and the managed
resources, and `planned_values` is empty. Account IDs are placeholders.
"""

import copy
import json
from pathlib import Path
from typing import Any

import bootstrap_plan_check as bpc
import policy_gate
import pytest

MANAGEMENT, MEMBER, REGION = "111111111111", "333333333333", "ca-central-1"
REPO_STACK = bpc.STACK

# address -> its `before` identity: the names the destroy must match.
DELETES = {
    "aws_iam_role.gha_plan": {"name": "ecp-gha-plan"},
    "aws_iam_role_policy_attachment.gha_plan_readonly": {
        "role": "ecp-gha-plan", "policy_arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"},
    "aws_iam_role_policy.gha_plan_state": {"role": "ecp-gha-plan", "name": "terraform-state-read"},
    "aws_iam_role.gha_deploy": {"name": "ecp-gha-deploy"},
    "aws_iam_role_policy_attachment.gha_deploy_poweruser": {
        "role": "ecp-gha-deploy", "policy_arn": "arn:aws:iam::aws:policy/PowerUserAccess"},
    "aws_iam_role_policy.gha_deploy_iam": {
        "role": "ecp-gha-deploy", "name": "ecp-scoped-iam-and-state"},
    "aws_iam_role_policy.gha_deploy_state_read": {
        "role": "ecp-gha-deploy", "name": "terraform-state-read"},
    "aws_iam_policy.workload_boundary": {"name": "ecp-workload-boundary", "path": "/"},
}  # fmt: skip
OUTPUTS = ("state_bucket", "gha_plan_role_arn", "gha_deploy_role_arn", "workload_boundary_arn")


@pytest.fixture(autouse=True)
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the bootstrap stack's .tf files (never its tfvars), with no override file."""
    copy_dir = tmp_path / "bootstrap"
    copy_dir.mkdir()
    for tf in REPO_STACK.glob("*.tf"):
        if tf.name != "backend_override.tf":
            (copy_dir / tf.name).write_text(tf.read_text())
    monkeypatch.setattr(bpc, "STACK", copy_dir)
    return copy_dir


def delete(address: str, before: dict[str, Any]) -> dict:
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "change": {"actions": ["delete"], "before": {"id": "x", **before}, "after": None,
                       "after_unknown": {}}}  # fmt: skip


def managed(address: str, values: dict[str, Any]) -> dict:
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "values": {"id": "x", **values}}  # fmt: skip


@pytest.fixture
def plan() -> dict:
    return {
        "complete": True,
        "errored": False,
        "applyable": True,
        "variables": {"expected_account_id": {"value": MANAGEMENT},
                      "member_instance": {"value": False}, "region": {"value": REGION}},
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_caller_identity.current", "mode": "data",
             "values": {"account_id": MANAGEMENT}},
            {"address": "data.aws_organizations_organization.this", "mode": "data",
             "values": {"master_account_id": MANAGEMENT}},
            *[managed(a, v) for a, v in DELETES.items()],
        ]}}},
        "configuration": {"root_module": {"resources": [
            {"address": a, "type": a.split(".")[0]} for a in DELETES]}},
        "planned_values": {"root_module": {}},
        "resource_changes": [delete(a, v) for a, v in DELETES.items()],
        "output_changes": {name: {"actions": ["delete"]} for name in OUTPUTS},
    }  # fmt: skip


def stops(plan: dict, needle: str) -> None:
    problems, _ = bpc.check_decommission(plan)
    assert any(needle in p for p in problems), problems


def test_the_exact_destroy_set_is_ok(plan: dict) -> None:
    problems, report = bpc.check_decommission(plan)
    assert problems == []
    assert sum(line.startswith("delete") for line in report) == 8


def test_the_policy_gate_refuses_a_destroy_only_plan_and_the_check_is_its_gate(plan: dict) -> None:
    """Pins the phase 5 design: policy_gate.py counts only resources that remain, so an all-delete
    plan is VACUOUS (exit 2) by design, and check_decommission is the gate for it instead."""
    assert policy_gate.evaluate(plan, []) == (
        2, ["VACUOUS: plan contains no managed resources to check"])  # fmt: skip
    assert bpc.check_decommission(plan)[0] == []


@pytest.mark.parametrize(
    "extra",
    ["aws_iam_openid_connect_provider.github[0]", "aws_s3_bucket.member_state[0]",
     'aws_iam_role.break_glass["OrganizationAccountAccessRole"]'],
)  # fmt: skip
def test_an_extra_delete_stops(plan: dict, extra: str) -> None:
    plan["resource_changes"].append(delete(extra, {"name": "x"}))
    stops(plan, f"unexpected change: delete {extra}")


@pytest.mark.parametrize("missing", sorted(DELETES))
def test_a_missing_delete_stops(plan: dict, missing: str) -> None:
    plan["resource_changes"] = [r for r in plan["resource_changes"] if r["address"] != missing]
    stops(plan, f"missing change: delete {missing}")


@pytest.mark.parametrize("actions", [["create"], ["update"], ["delete", "create"],
                                     ["create", "delete"]])  # fmt: skip
def test_anything_but_a_delete_stops(plan: dict, actions: list[str]) -> None:
    plan["resource_changes"][0]["change"]["actions"] = actions
    stops(plan, "unexpected change:")


def test_a_create_elsewhere_stops(plan: dict) -> None:
    plan["resource_changes"].append({"address": "aws_iam_role.other", "mode": "managed",
        "type": "aws_iam_role", "change": {"actions": ["create"], "before": None,
                                           "after": {"name": "x"}}})  # fmt: skip
    stops(plan, "unexpected change: create aws_iam_role.other")


@pytest.mark.parametrize(
    ("address", "attr", "value"),
    [("aws_iam_role.gha_plan", "name", "someone-elses-role"),
     ("aws_iam_role.gha_deploy", "name", "ecp-gha-plan"),
     ("aws_iam_role_policy_attachment.gha_deploy_poweruser", "policy_arn",
      "arn:aws:iam::aws:policy/AdministratorAccess"),
     ("aws_iam_role_policy.gha_deploy_iam", "role", "another-role"),
     ("aws_iam_policy.workload_boundary", "name", "another-policy"),
     ("aws_iam_policy.workload_boundary", "path", "/other/")],
)  # fmt: skip
def test_a_delete_of_the_wrong_object_stops(
    plan: dict, address: str, attr: str, value: str
) -> None:
    rc = next(r for r in plan["resource_changes"] if r["address"] == address)
    rc["change"]["before"][attr] = value
    stops(plan, f"{address}: not the reviewed object ({attr})")


def test_an_extra_resource_in_the_state_stops(plan: dict) -> None:
    resources = plan["prior_state"]["values"]["root_module"]["resources"]
    resources.append(managed("aws_iam_role.other", {"name": "x"}))
    stops(plan, "the state holds aws_iam_role.other")


def test_member_instance_true_stops(plan: dict) -> None:
    plan["variables"]["member_instance"]["value"] = True
    stops(plan, "member_instance must be false")


def test_a_member_account_caller_stops(plan: dict) -> None:
    resources = plan["prior_state"]["values"]["root_module"]["resources"]
    resources[0]["values"]["account_id"] = MEMBER
    plan["variables"]["expected_account_id"]["value"] = MEMBER
    stops(plan, "not the organization's management account")


def test_a_caller_other_than_expected_account_id_stops(plan: dict) -> None:
    plan["variables"]["expected_account_id"]["value"] = MEMBER
    stops(plan, "the caller is not expected_account_id")


@pytest.mark.parametrize("org_type", sorted(bpc.ORG_ONLY_TYPES))
def test_an_org_only_type_stops(plan: dict, org_type: str) -> None:
    plan["resource_changes"].append(delete(f"{org_type}.x", {"name": "x"}))
    stops(plan, f"{org_type}.x: owned by infra/org")


@pytest.mark.parametrize(
    ("key", "value", "needle"),
    [("errored", True, "the plan errored"),
     ("complete", False, "the plan is incomplete"),
     ("resource_drift", [{"address": "aws_iam_role.gha_plan"}], "resource_drift"),
     ("deferred_changes", [{"address": "aws_iam_role.gha_plan"}], "deferred_changes")],
)  # fmt: skip
def test_an_unusual_plan_stops(plan: dict, key: str, value: Any, needle: str) -> None:
    plan[key] = value
    stops(plan, needle)


def test_an_import_stops(plan: dict) -> None:
    plan["resource_changes"][0]["change"]["importing"] = {"id": "ecp-gha-plan"}
    stops(plan, "unexpected import")


@pytest.mark.parametrize(
    ("name", "actions"),
    [("state_bucket", ["update"]), ("something_new", ["create"])],
)  # fmt: skip
def test_an_unexpected_output_change_stops(plan: dict, name: str, actions: list[str]) -> None:
    plan["output_changes"][name] = {"actions": actions}
    stops(plan, f"unexpected output change: {'+'.join(actions)} {name}")


def test_a_kept_output_stops(plan: dict) -> None:
    del plan["output_changes"]["workload_boundary_arn"]
    stops(plan, "missing output change: delete workload_boundary_arn")


@pytest.mark.parametrize(
    ("name", "needle"),
    [("backend_override.tf", "override files are not allowed"),
     ("x_override.tf", "override files are not allowed"),
     ("extra.tf.json", "JSON configuration is not allowed")],
)  # fmt: skip
def test_an_override_or_json_file_stops(plan: dict, stack: Path, name: str, needle: str) -> None:
    (stack / name).write_text('terraform {\n  backend "local" {}\n}\n' if name.endswith(".tf")
                              else "{}")  # fmt: skip
    stops(plan, needle)


def test_a_module_call_in_the_source_stops(plan: dict, stack: Path) -> None:
    (stack / "extra.tf").write_text('module "m" {\n  source = "./m"\n}\n')
    stops(plan, "module calls are not allowed in the stack: m")


def test_a_module_call_in_the_plan_configuration_stops(plan: dict) -> None:
    plan["configuration"]["root_module"]["module_calls"] = {"m": {}}
    stops(plan, "module calls are not allowed in the stack: m")


def test_the_report_names_each_delete_and_the_source(plan: dict) -> None:
    _, report = bpc.check_decommission(plan)
    assert "delete   aws_iam_role.gha_plan  (ecp-gha-plan)" in report
    assert "source: no override, no JSON configuration, no module call" in report
    assert report[-1].startswith("summary: 0 to add, 0 to change, 8 to destroy")


def test_the_cli_runs_the_decommission_mode(
    plan: dict, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert bpc.main(["--change", "decommission", str(path)]) == 0
    assert "OK: exactly the reviewed decommission change" in capsys.readouterr().out
    bad = copy.deepcopy(plan)
    bad["variables"]["member_instance"]["value"] = True
    path.write_text(json.dumps(bad))
    assert bpc.main(["--change", "decommission", str(path)]) == 1
