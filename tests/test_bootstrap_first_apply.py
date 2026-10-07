"""ADR-0021 phase 3: the member bootstrap's first apply must be exactly the reviewed resource set.

The plan is synthetic but shaped like `terraform show -json` (Terraform 1.15.8) for a fresh local
state in ecp-workloads: the caller identity and organization data sources, 16 creates, and the
import of OrganizationAccountAccessRole with only its trust policy (and tags) changing. Account
IDs are placeholders.
"""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import bootstrap_plan_check as bpc
import policy_templates
import pytest

MEMBER, MANAGEMENT, REGION = "333333333333", "111111111111", "ca-central-1"
BUCKET = f"ecp-tfstate-{MEMBER}-{REGION}"
BOUNDARY_VALUES = {"account_id": MEMBER, "state_bucket_arn": f"arn:aws:s3:::{BUCKET}"}
OLD_TRUST = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{MANAGEMENT}:root"},
    "Action": "sts:AssumeRole"}]}  # fmt: skip


# The first apply plans on local state through this override; Terraform rejects the one-line
# `terraform { backend "local" {} }` (a one-line block may hold only one argument).
LOCAL_BACKEND = 'terraform {\n  backend "local" {}\n}\n'
REPO_STACK = bpc.STACK


@pytest.fixture(autouse=True)
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A copy of the bootstrap stack's .tf files (never its tfvars) with the allowed override."""
    copy_dir = tmp_path / "bootstrap"
    copy_dir.mkdir()
    for tf in REPO_STACK.glob("*.tf"):
        if tf.name != "backend_override.tf":
            (copy_dir / tf.name).write_text(tf.read_text())
    (copy_dir / "backend_override.tf").write_text(LOCAL_BACKEND)
    monkeypatch.setattr(bpc, "STACK", copy_dir)
    return copy_dir


def break_glass_trust(management: str = MANAGEMENT) -> dict:
    return policy_templates.render("break-glass-trust", {"management_account_id": management})


def rc(address: str, actions: list[str], after: Any, before: Any = None, **extra: Any) -> dict:
    change = {"actions": actions, "before": before, "after": after, "after_unknown": {}, **extra}
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "change": change}  # fmt: skip


def creates() -> list[dict]:
    p = "aws_s3_bucket"
    return [
        rc(f"{p}.member_state[0]", ["create"], {"bucket": BUCKET, "force_destroy": False},
           after_unknown={"arn": True, "id": True}),
        rc(f"{p}_versioning.member_state[0]", ["create"],
           {"versioning_configuration": [{"status": "Enabled", "mfa_delete": None}]}),
        rc(f"{p}_server_side_encryption_configuration.member_state[0]", ["create"],
           {"rule": [{"apply_server_side_encryption_by_default": [
               {"sse_algorithm": "AES256", "kms_master_key_id": None}],
               "bucket_key_enabled": None}]}),
        rc(f"{p}_public_access_block.member_state[0]", ["create"],
           {"block_public_acls": True, "block_public_policy": True,
            "ignore_public_acls": True, "restrict_public_buckets": True}),
        rc(f"{p}_ownership_controls.member_state[0]", ["create"],
           {"rule": [{"object_ownership": "BucketOwnerEnforced"}]}),
        rc(f"{p}_lifecycle_configuration.member_state[0]", ["create"],
           {"rule": [{"id": "expire-noncurrent-state", "status": "Enabled",
                      "noncurrent_version_expiration": [{"noncurrent_days": 90,
                                                         "newer_noncurrent_versions": None}],
                      "abort_incomplete_multipart_upload": [{"days_after_initiation": 7}]}]}),
        rc(f"{p}_policy.member_state[0]", ["create"], {}, after_unknown={"policy": True}),
        rc("aws_iam_openid_connect_provider.github[0]", ["create"],
           {"url": "https://token.actions.githubusercontent.com",
            "client_id_list": ["sts.amazonaws.com"]}, after_unknown={"arn": True}),
        rc("aws_iam_role.gha_plan", ["create"],
           {"name": "ecp-gha-plan", "max_session_duration": 3600, "permissions_boundary": None},
           after_unknown={"assume_role_policy": True, "arn": True}),
        rc("aws_iam_role_policy_attachment.gha_plan_readonly", ["create"],
           {"policy_arn": "arn:aws:iam::aws:policy/ReadOnlyAccess", "role": "ecp-gha-plan"}),
        rc("aws_iam_role_policy.gha_plan_state", ["create"], {"name": "terraform-state-read"},
           after_unknown={"role": True, "policy": True}),
        rc("aws_iam_role.gha_deploy", ["create"],
           {"name": "ecp-gha-deploy", "max_session_duration": 3600, "permissions_boundary": None},
           after_unknown={"assume_role_policy": True, "arn": True}),
        rc("aws_iam_role_policy_attachment.gha_deploy_poweruser", ["create"],
           {"policy_arn": "arn:aws:iam::aws:policy/PowerUserAccess", "role": "ecp-gha-deploy"}),
        rc("aws_iam_role_policy.gha_deploy_iam", ["create"], {"name": "ecp-scoped-iam-and-state"},
           after_unknown={"role": True, "policy": True}),
        rc("aws_iam_role_policy.gha_deploy_state_read", ["create"],
           {"name": "terraform-state-read"}, after_unknown={"role": True, "policy": True}),
        rc("aws_iam_policy.workload_boundary", ["create"],
           {"name": "ecp-workload-boundary", "path": "/",
            "policy": json.dumps(policy_templates.render("workload-boundary", BOUNDARY_VALUES))},
           after_unknown={"arn": True, "id": True, "name_prefix": True}),
    ]  # fmt: skip


@pytest.fixture
def plan() -> dict:
    role = {"name": "OrganizationAccountAccessRole", "max_session_duration": 3600,
            "description": "", "tags": {}, "tags_all": {}}  # fmt: skip
    return {
        "complete": True,
        "variables": {"expected_account_id": {"value": MEMBER},
                      "member_instance": {"value": True}, "region": {"value": REGION}},
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_caller_identity.current", "mode": "data",
             "values": {"account_id": MEMBER}},
            {"address": "data.aws_organizations_organization.this", "mode": "data",
             "values": {"master_account_id": MANAGEMENT}},
            {"address": bpc.BREAK_GLASS, "mode": "managed",  # pending import
             "values": {**role, "assume_role_policy": json.dumps(OLD_TRUST)}},
        ]}}},
        "configuration": {"root_module": {"resources": [
            {"address": "aws_iam_role.break_glass", "type": "aws_iam_role"},
            {"address": "aws_s3_bucket.member_state", "type": "aws_s3_bucket"},
        ]}},
        "resource_changes": [
            *creates(),
            rc(bpc.BREAK_GLASS, ["update"],
               {**role, "assume_role_policy": json.dumps(break_glass_trust()),
                "tags": {}, "tags_all": {"project": "energy-curve-platform"}},
               {**role, "assume_role_policy": json.dumps(OLD_TRUST)},
               importing={"id": "OrganizationAccountAccessRole"}),
        ],
        "resource_drift": [],
        "output_changes": {name: {"actions": ["create"]} for name in bpc.FIRST_APPLY_OUTPUTS},
    }  # fmt: skip


def change(plan: dict, address: str) -> dict:
    return next(r for r in plan["resource_changes"] if r["address"] == address)["change"]


def problems(plan: dict) -> list[str]:
    return bpc.check_first_apply(plan)[0]


def test_the_reviewed_first_apply_passes(plan: dict) -> None:
    found, report = bpc.check_first_apply(plan)
    assert found == []
    assert len([line for line in report if line.startswith("create")]) == 16
    assert any(line.startswith("update") and "(import)" in line for line in report)
    assert not any(MEMBER in line or MANAGEMENT in line for line in report)


def test_the_resource_set_is_named_exactly() -> None:
    assert len(bpc.FIRST_APPLY_CREATES) == 16
    assert bpc.BREAK_GLASS == 'aws_iam_role.break_glass["OrganizationAccountAccessRole"]'


@pytest.mark.parametrize("missing", sorted(bpc.FIRST_APPLY_CREATES))
def test_a_missing_create_stops(plan: dict, missing: str) -> None:
    plan["resource_changes"] = [r for r in plan["resource_changes"] if r["address"] != missing]
    assert f"missing change: create {missing}" in problems(plan)


def test_a_missing_break_glass_import_stops(plan: dict) -> None:
    plan["resource_changes"] = [r for r in plan["resource_changes"]
                                if r["address"] != bpc.BREAK_GLASS]  # fmt: skip
    assert f"missing change: import and update {bpc.BREAK_GLASS}" in problems(plan)


@pytest.mark.parametrize("extra", ["aws_s3_bucket.extra", "aws_iam_role.extra"])
def test_any_other_create_stops(plan: dict, extra: str) -> None:
    plan["resource_changes"].append(rc(extra, ["create"], {}))
    assert problems(plan) == [f"unexpected change: create {extra}"]


@pytest.mark.parametrize("kind", ["aws_budgets_budget", "aws_ce_cost_allocation_tag"])
def test_a_budget_or_cost_tag_in_the_plan_stops(plan: dict, kind: str) -> None:
    plan["resource_changes"].append(rc(f"{kind}.project", ["create"], {}))
    assert f"{kind}.project: the member bootstrap has no budget or cost allocation tag" in (
        problems(plan)
    )


@pytest.mark.parametrize("kind", ["aws_budgets_budget", "aws_ce_cost_allocation_tag"])
def test_a_budget_or_cost_tag_in_the_configuration_stops(plan: dict, kind: str) -> None:
    plan["configuration"]["root_module"]["resources"].append(
        {"address": f"{kind}.project", "type": kind}
    )
    assert problems(plan) == [
        f"{kind}.project: the member bootstrap has no budget or cost allocation tag"
    ]


@pytest.mark.parametrize("actions", [["delete"], ["update"], ["delete", "create"]])
def test_a_create_with_another_action_stops(plan: dict, actions: list[str]) -> None:
    change(plan, "aws_iam_role.gha_plan")["actions"] = actions
    found = problems(plan)
    assert f"unexpected change: {'+'.join(actions)} aws_iam_role.gha_plan" in found


def test_any_other_import_stops(plan: dict) -> None:
    change(plan, "aws_iam_role.gha_plan")["importing"] = {"id": "ecp-gha-plan"}
    assert problems(plan) == [
        "unexpected import: aws_iam_role.gha_plan",
        "missing change: create aws_iam_role.gha_plan",
    ]


def test_the_break_glass_import_must_be_that_role(plan: dict) -> None:
    change(plan, bpc.BREAK_GLASS)["importing"] = {"id": "SomeOtherRole"}
    assert problems(plan) == [f"{bpc.BREAK_GLASS}: must import OrganizationAccountAccessRole"]


def test_the_break_glass_role_must_be_imported_not_created(plan: dict) -> None:
    c = change(plan, bpc.BREAK_GLASS)
    c.pop("importing")
    c["actions"] = ["create"]
    found = problems(plan)
    assert f"unexpected change: create {bpc.BREAK_GLASS}" in found


@pytest.mark.parametrize("trust", [OLD_TRUST, break_glass_trust("222222222222"), None])
def test_a_break_glass_trust_other_than_the_template_stops(plan: dict, trust: Any) -> None:
    change(plan, bpc.BREAK_GLASS)["after"]["assume_role_policy"] = (
        json.dumps(trust) if trust else None
    )
    found = problems(plan)
    assert (
        f"{bpc.BREAK_GLASS}: trust policy differs from policies/break-glass-trust.json.tftpl"
        in found
    )


@pytest.mark.parametrize(("key", "value"), [("max_session_duration", 43200),
                                            ("description", "changed"),
                                            ("permissions_boundary", "arn:aws:iam::x:policy/y"),
                                            ("name", "Renamed")])  # fmt: skip
def test_the_break_glass_role_may_change_only_its_trust_and_tags(
    plan: dict, key: str, value: Any
) -> None:
    change(plan, bpc.BREAK_GLASS)["after"][key] = value
    assert f"{bpc.BREAK_GLASS}: may change only assume_role_policy and tags" in problems(plan)


def test_the_bucket_must_be_named_for_this_account(plan: dict) -> None:
    change(plan, "aws_s3_bucket.member_state[0]")["after"]["bucket"] = "ecp-tfstate-other"
    assert problems(plan) == [
        "aws_s3_bucket.member_state[0]: bucket must be named for this account"
    ]


@pytest.mark.parametrize(("address", "path", "value"), [
    ("aws_s3_bucket_versioning.member_state[0]", ("versioning_configuration", 0, "status"),
     "Suspended"),
    ("aws_s3_bucket_server_side_encryption_configuration.member_state[0]",
     ("rule", 0, "apply_server_side_encryption_by_default", 0, "sse_algorithm"), "aws:kms"),
    ("aws_s3_bucket_public_access_block.member_state[0]", ("restrict_public_buckets",), False),
    ("aws_s3_bucket_ownership_controls.member_state[0]", ("rule", 0, "object_ownership"),
     "ObjectWriter"),
    ("aws_s3_bucket_lifecycle_configuration.member_state[0]",
     ("rule", 0, "noncurrent_version_expiration", 0, "noncurrent_days"), 1),
    ("aws_iam_openid_connect_provider.github[0]", ("client_id_list",), ["other"]),
    ("aws_iam_openid_connect_provider.github[0]", ("url",), "https://example.com"),
    ("aws_iam_role.gha_plan", ("name",), "other"),
    ("aws_iam_role.gha_deploy", ("max_session_duration",), 43200),
    ("aws_iam_role_policy_attachment.gha_deploy_poweruser", ("policy_arn",),
     "arn:aws:iam::aws:policy/AdministratorAccess"),
    ("aws_iam_role_policy.gha_deploy_iam", ("name",), "other"),
])  # fmt: skip
def test_each_create_must_be_as_reviewed(plan: dict, address: str, path: tuple,
                                         value: Any) -> None:  # fmt: skip
    target = change(plan, address)["after"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert problems(plan) == [f"{address}: not as reviewed ({path[0]})"]


def test_the_boundary_must_equal_its_template(plan: dict) -> None:
    doc = policy_templates.render("workload-boundary", BOUNDARY_VALUES)
    doc["Statement"] = doc["Statement"][:-1]
    change(plan, "aws_iam_policy.workload_boundary")["after"]["policy"] = json.dumps(doc)
    assert problems(plan) == [
        "aws_iam_policy.workload_boundary: policy differs from the workload-boundary template"
    ]


def test_a_known_deploy_policy_must_equal_its_template(plan: dict) -> None:
    c = change(plan, "aws_iam_role_policy.gha_deploy_iam")
    c["after_unknown"]["policy"] = False
    c["after"]["policy"] = json.dumps({"Version": "2012-10-17", "Statement": []})
    assert problems(plan) == [
        "aws_iam_role_policy.gha_deploy_iam: a known policy must equal the deploy-iam template"
    ]


@pytest.mark.parametrize(("key", "value", "message"), [
    ("expected_account_id", "444444444444", "the caller is not expected_account_id"),
    ("member_instance", False, "member_instance must be true"),
])  # fmt: skip
def test_the_instance_variables_must_fit(plan: dict, key: str, value: Any, message: str) -> None:
    plan["variables"][key]["value"] = value
    assert message in problems(plan)


def test_the_management_account_stops(plan: dict) -> None:
    org = plan["prior_state"]["values"]["root_module"]["resources"][1]
    org["values"]["master_account_id"] = MEMBER
    assert "this is the organization's management account, not a member" in problems(plan)


def test_an_existing_state_stops(plan: dict) -> None:
    plan["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_iam_role.gha_plan", "mode": "managed", "values": {}}
    )
    assert "the state is not empty: a first apply starts from no managed resources" in problems(
        plan
    )


def test_drift_stops(plan: dict) -> None:
    plan["resource_drift"] = [{"address": bpc.BREAK_GLASS, "change": {"actions": ["update"]}}]
    assert problems(plan) == [f"resource_drift: {bpc.BREAK_GLASS}"]


def test_errored_incomplete_or_deferred_plans_stop(plan: dict) -> None:
    plan.update(errored=True, complete=False, deferred_changes=[{"address": "x"}])
    assert problems(plan)[:3] == ["the plan errored", "the plan is incomplete",
                                  "deferred_changes: x"]  # fmt: skip


def test_an_unexpected_output_stops(plan: dict) -> None:
    plan["output_changes"]["something_else"] = {"actions": ["create"]}
    assert problems(plan) == ["unexpected output change: create something_else"]


def test_the_r2_mode_refuses_a_first_apply(plan: dict) -> None:
    assert bpc.check(copy.deepcopy(plan))[0]


def test_the_cli_selects_first_apply(tmp_path: Any, plan: dict) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert bpc.main(["--change", "first-apply", str(path)]) == 0
    change(plan, "aws_iam_role.gha_plan")["actions"] = ["delete"]
    path.write_text(json.dumps(plan))
    assert bpc.main(["--change", "first-apply", str(path)]) == 1


# --- the source: overrides can change what the plan JSON cannot show (unknown policies) -------


def test_the_exact_local_backend_override_passes_and_is_hashed(plan: dict) -> None:
    found, report = bpc.check_first_apply(plan)
    assert found == []
    digest = hashlib.sha256(LOCAL_BACKEND.encode()).hexdigest()
    assert f"source: backend_override.tf sha256 {digest}" in report


def test_a_plain_run_without_the_override_stops(plan: dict, stack: Path) -> None:
    (stack / "backend_override.tf").unlink()
    assert problems(plan) == [
        "backend_override.tf is missing: the first apply plans on local state"
    ]


@pytest.mark.parametrize("content", [
    LOCAL_BACKEND + 'resource "aws_iam_role" "extra" {}\n',
    LOCAL_BACKEND + 'locals {\n  x = 1\n}\n',
    'terraform {\n  backend "local" {\n    path = "elsewhere.tfstate"\n  }\n}\n',
    'terraform {\n  backend "s3" {}\n}\n',
    LOCAL_BACKEND.rstrip("\n"),
    "# comment\n" + LOCAL_BACKEND,
])  # fmt: skip
def test_an_override_with_other_content_stops(plan: dict, stack: Path, content: str) -> None:
    (stack / "backend_override.tf").write_text(content)
    assert problems(plan) == [
        'backend_override.tf must be exactly terraform { backend "local" {} } (fmt layout)'
    ]


@pytest.mark.parametrize("name", ["override.tf", "main_override.tf", "override.tf.json",
                                  "backend_override.tf.json", "boundary_override.tf"])  # fmt: skip
def test_any_other_override_file_stops(plan: dict, stack: Path, name: str) -> None:
    (stack / name).write_text("{}\n" if name.endswith(".json") else "locals {}\n")
    assert problems(plan) == [
        f"override files other than backend_override.tf are not allowed in the stack: {name}"
    ]


def test_json_configuration_stops(plan: dict, stack: Path) -> None:
    (stack / "extra.tf.json").write_text("{}\n")
    assert problems(plan) == ["JSON configuration is not allowed in the stack: extra.tf.json"]


def test_a_module_call_in_the_source_stops(plan: dict, stack: Path) -> None:
    (stack / "extra.tf").write_text('module "shadow" {\n  source = "./shadow"\n}\n')
    assert problems(plan) == ["module calls are not allowed in the stack: shadow (extra.tf)"]


def test_a_module_call_in_the_plan_configuration_stops(plan: dict) -> None:
    plan["configuration"]["root_module"]["module_calls"] = {"shadow": {"source": "./shadow"}}
    assert problems(plan) == ["module calls are not allowed in the stack: shadow"]


def test_an_unreadable_source_file_stops(plan: dict, stack: Path) -> None:
    (stack / "extra.tf").write_text('resource "aws_iam_role" "x" {\n')
    assert problems(plan) == ["cannot read the stack source: extra.tf"]


def test_files_terraform_does_not_load_are_ignored(plan: dict, stack: Path) -> None:
    for name in (".hidden_override.tf", "override.tf~", "#override.tf#", "notes.txt"):
        (stack / name).write_text("anything\n")
    assert problems(plan) == []


def test_the_cli_runs_the_source_check(tmp_path: Path, plan: dict, stack: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert bpc.main(["--change", "first-apply", str(path)]) == 0
    (stack / "backend_override.tf").unlink()
    assert bpc.main(["--change", "first-apply", str(path)]) == 1


def test_the_repository_commits_no_override() -> None:
    assert not [p.name for p in REPO_STACK.iterdir() if "override" in p.name]
