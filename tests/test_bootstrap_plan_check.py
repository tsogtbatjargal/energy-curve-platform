"""The pre-apply check for the R2 bootstrap apply (ADR-0018): only the reviewed change passes.

The plan is synthetic but shaped like `terraform show -json` (Terraform 1.15.8): prior state
with the caller identity, CI roles and state bucket; one create, one in-place update, no-ops.
"""

import copy
import json
from typing import Any

import bootstrap_plan_check as bpc
import pytest
import r2_policies as r2

BOUNDARY, DEPLOY = "aws_iam_policy.workload_boundary", "aws_iam_role_policy.gha_deploy_iam"


def rc(address: str, actions: list[str], before: Any, after: Any, unknown: Any = None) -> dict:
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "change": {"actions": actions, "before": before, "after": after,
                       "after_unknown": unknown or {}}}  # fmt: skip


def state(address: str, values: dict) -> dict:
    return {"address": address, "values": values}


@pytest.fixture
def plan() -> dict:
    stmt = {"Sid": "WriteState", "Effect": "Allow", "Action": "s3:PutObject", "Resource": "*"}
    old = {"Version": "2012-10-17", "Statement": [stmt]}
    return {
        "prior_state": {"values": {"root_module": {"resources": [
            state("data.aws_caller_identity.current", {"account_id": r2.ACCOUNT}),
            state("aws_iam_role.gha_deploy", {"arn": r2.DEPLOY_ROLE_ARN}),
            state("aws_iam_role.gha_plan", {"arn": r2.PLAN_ROLE_ARN}),
            state("aws_s3_bucket.tfstate", {"arn": r2.VALUES["state_bucket_arn"]}),
        ]}}},
        "resource_changes": [
            rc("aws_s3_bucket.tfstate", ["no-op"], {"bucket": "b"}, {"bucket": "b"}),
            rc("aws_iam_role.gha_deploy", ["no-op"], {"name": "d"}, {"name": "d"}),
            rc(BOUNDARY, ["create"], None,  # as in a real 1.15.8 plan: name_prefix unknown, absent
               {"name": "ecp-workload-boundary", "path": "/", "policy": json.dumps(r2.boundary())},
               {"arn": True, "id": True, "name_prefix": True}),
            rc(DEPLOY, ["update"], {"name": "n", "policy": json.dumps(old)},
               {"name": "n", "policy": json.dumps(r2.render("deploy-iam"))}),
        ],
        "output_changes": {
            "state_bucket": {"actions": ["no-op"]},
            "workload_boundary_arn": {"actions": ["create"]},
        },
    }  # fmt: skip


def change(plan: dict, address: str) -> dict:
    return next(r for r in plan["resource_changes"] if r["address"] == address)


def test_the_reviewed_change_passes(plan: dict) -> None:
    problems, report = bpc.check(plan)
    assert problems == []
    assert any(line.startswith("create") and BOUNDARY in line for line in report)
    assert any(f"{DEPLOY}  attributes: ['policy']" in line for line in report)


def mutate_extra_resource(plan: dict) -> None:
    plan["resource_changes"].append(rc("aws_s3_bucket.extra", ["create"], None, {"bucket": "x"}))


def mutate_destroy(plan: dict) -> None:
    change(plan, "aws_s3_bucket.tfstate")["change"]["actions"] = ["delete"]


def mutate_replace_deploy_policy(plan: dict) -> None:
    change(plan, DEPLOY)["change"]["actions"] = ["delete", "create"]


def mutate_rename_deploy_policy(plan: dict) -> None:
    change(plan, DEPLOY)["change"]["after"]["name"] = "renamed"


def mutate_missing_boundary(plan: dict) -> None:
    plan["resource_changes"].remove(change(plan, BOUNDARY))


def mutate_drift(plan: dict) -> None:
    plan["resource_drift"] = [{"address": "aws_iam_role.gha_deploy"}]


def mutate_output(plan: dict) -> None:
    plan["output_changes"]["state_bucket"] = {"actions": ["update"]}


def mutate_errored(plan: dict) -> None:
    plan["errored"] = True


def mutate_policy_content(plan: dict) -> None:
    doc = r2.render("deploy-iam")
    doc["Statement"] = [s for s in doc["Statement"] if s.get("Sid") != "NoIdentityCenter"]
    change(plan, DEPLOY)["change"]["after"]["policy"] = json.dumps(doc)


def mutate_unknown_policy(plan: dict) -> None:
    change(plan, BOUNDARY)["change"]["after_unknown"]["policy"] = True


def mutate_prior_state(plan: dict) -> None:
    plan["prior_state"]["values"]["root_module"]["resources"].pop(0)


def boundary_after(plan: dict) -> dict:
    return change(plan, BOUNDARY)["change"]["after"]


def mutate_boundary_name(plan: dict) -> None:
    boundary_after(plan)["name"] = "ecp-workload-boundary-v2"


def mutate_boundary_path(plan: dict) -> None:
    boundary_after(plan)["path"] = "/workload/"


def mutate_boundary_name_prefix(plan: dict) -> None:
    after = boundary_after(plan)
    after["name"], after["name_prefix"] = None, "ecp-workload-boundary"
    change(plan, BOUNDARY)["change"]["after_unknown"]["name"] = True


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (mutate_extra_resource, "unexpected change: create aws_s3_bucket.extra"),
        (mutate_destroy, "unexpected change: delete aws_s3_bucket.tfstate"),
        (mutate_replace_deploy_policy, "delete+create, expected update"),
        (mutate_rename_deploy_policy, "only ['policy'] allowed"),
        (mutate_missing_boundary, "missing expected change: aws_iam_policy.workload_boundary"),
        (mutate_drift, "resource_drift: aws_iam_role.gha_deploy"),
        (mutate_output, "unexpected output change: update state_bucket"),
        (mutate_errored, "the plan errored"),
        (mutate_policy_content, "differs from the deploy-iam template"),
        (mutate_unknown_policy, "policy unknown at plan time"),
        (mutate_prior_state, "prior state lacks"),
        # the boundary's identity: another name or path is another policy ARN
        (mutate_boundary_name, "boundary name must be ecp-workload-boundary"),
        (mutate_boundary_path, "boundary path must be /"),
        (mutate_boundary_name_prefix, "boundary name must be ecp-workload-boundary"),
    ],
)
def test_any_other_difference_stops_the_apply(plan: dict, mutate: Any, why: str) -> None:
    mutate(plan)
    problems, _ = bpc.check(plan)
    assert any(why in p for p in problems), problems


def test_the_cli_exits_nonzero_and_prints_no_values(
    plan: dict, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert bpc.main([str(path)]) == 0
    bad = copy.deepcopy(plan)
    mutate_extra_resource(bad)
    path.write_text(json.dumps(bad))
    assert bpc.main([str(path)]) == 1
    out = capsys.readouterr().out
    assert "STOP: do not apply this plan" in out
    assert r2.ACCOUNT not in out  # addresses, attribute names and digests only
