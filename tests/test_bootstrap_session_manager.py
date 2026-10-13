"""Only the reviewed in-place boundary-policy amendment may pass the bootstrap gate."""

import copy
import json

import bootstrap_plan_check as bpc
import pytest
import r2_policies as r2
from session_manager_boundary import AFTER, AGENT_ACTIONS, BEFORE

MODE = "session-manager"
BOUNDARY = bpc.BOUNDARY


def resource(address, actions, before, after):
    return {"address": address, "mode": "managed", "type": address.split(".")[0],
            "change": {"actions": actions, "before": before, "after": after,
                       "after_unknown": {}}}  # fmt: skip


@pytest.fixture
def plan():
    before = {"name": bpc.BOUNDARY_NAME, "path": "/", "policy": json.dumps(BEFORE)}
    after = {**before, "policy": json.dumps(AFTER)}
    return {
        "complete": True,
        "errored": False,
        "variables": {"region": {"value": "ca-central-1"}},
        "prior_state": {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": "data.aws_caller_identity.current",
                            "values": {"account_id": r2.ACCOUNT},
                        },
                        {
                            "address": "aws_iam_role.gha_deploy",
                            "values": {"arn": r2.DEPLOY_ROLE_ARN},
                        },
                        {"address": "aws_iam_role.gha_plan", "values": {"arn": r2.PLAN_ROLE_ARN}},
                    ]
                }
            }
        },
        "resource_changes": [
            resource(BOUNDARY, ["update"], before, after),
            resource("aws_iam_role.gha_deploy", ["no-op"], {}, {}),
        ],
        "output_changes": {
            "workload_boundary_arn": {
                "actions": ["no-op"],
                "before": r2.BOUNDARY_ARN,
                "after": r2.BOUNDARY_ARN,
                "after_unknown": False,
            }
        },
    }


def checked(plan):
    return bpc.check(plan, MODE)[0]


def test_exact_boundary_update_passes(plan):
    assert checked(plan) == []


def test_cli_dispatches_the_boundary_mode(plan, tmp_path, capsys):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert bpc.main(["--change", MODE, str(path)]) == 0
    assert "exactly the reviewed session-manager change" in capsys.readouterr().out


@pytest.mark.parametrize("action", AGENT_ACTIONS)
def test_missing_any_agent_action_is_refused(plan, action):
    policy = copy.deepcopy(AFTER)
    policy["Statement"][0]["Action"].remove(action)
    plan["resource_changes"][0]["change"]["after"]["policy"] = json.dumps(policy)
    assert checked(plan)


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize(
    "mutation", ["extra-action", "remove-state-deny", "pass-other-roles", "new-statement"]
)
def test_other_policy_edits_are_refused(plan, side, mutation):
    change = plan["resource_changes"][0]["change"]
    policy = json.loads(change[side]["policy"])
    if mutation == "extra-action":
        policy["Statement"][0]["Action"].append("ssm:StartSession")
    elif mutation == "remove-state-deny":
        policy["Statement"].pop()
    elif mutation == "pass-other-roles":
        policy["Statement"][1]["Resource"] = "*"
    else:
        policy["Statement"].append({"Effect": "Allow", "Action": "ec2:*", "Resource": "*"})
    change[side]["policy"] = json.dumps(policy)
    assert checked(plan)


@pytest.mark.parametrize(
    "actions", [["create"], ["delete"], ["no-op"], ["delete", "create"], ["create", "delete"]]
)
def test_wrong_boundary_actions_are_refused(plan, actions):
    plan["resource_changes"][0]["change"]["actions"] = actions
    assert checked(plan)


@pytest.mark.parametrize("attribute", ["name", "path", "description", "tags", "arn", "id"])
def test_other_changed_or_unknown_attributes_are_refused(plan, attribute):
    change = plan["resource_changes"][0]["change"]
    change["after"][attribute] = "different"
    assert checked(plan)


@pytest.mark.parametrize(
    "attribute", ["policy", "name", "path", "description", "tags", "arn", "id"]
)
def test_any_unknown_attribute_is_refused(plan, attribute):
    plan["resource_changes"][0]["change"]["after_unknown"][attribute] = True
    assert checked(plan)


@pytest.mark.parametrize(
    "problem",
    [
        "drift",
        "deferred",
        "output",
        "hidden-output",
        "errored",
        "incomplete",
        "missing-complete",
        "extra-resource",
        "missing-update",
        "missing-resource-changes",
        "duplicate",
        "import",
        "replace-path",
        "hidden-noop",
        "unknown-noop",
        "read",
        "missing-identity",
        "malformed-before",
        "malformed-after",
        "old-noop",
    ],
)
def test_other_plan_differences_are_refused(plan, problem):
    change = plan["resource_changes"][0]["change"]
    if problem in {"drift", "deferred"}:
        plan[{"drift": "resource_drift", "deferred": "deferred_changes"}[problem]] = [
            {"address": BOUNDARY}
        ]
    elif problem == "output":
        plan["output_changes"]["workload_boundary_arn"]["actions"] = ["update"]
    elif problem == "hidden-output":
        plan["output_changes"]["workload_boundary_arn"]["after_unknown"] = True
    elif problem == "errored":
        plan["errored"] = True
    elif problem == "incomplete":
        plan["complete"] = False
    elif problem == "missing-complete":
        del plan["complete"]
    elif problem == "extra-resource":
        plan["resource_changes"].append(
            resource("aws_iam_role.extra", ["update"], {}, {"name": "x"})
        )
    elif problem == "missing-update":
        plan["resource_changes"].pop(0)
    elif problem == "missing-resource-changes":
        del plan["resource_changes"]
    elif problem == "duplicate":
        plan["resource_changes"].append(copy.deepcopy(plan["resource_changes"][0]))
    elif problem == "import":
        change["importing"] = {"id": "other"}
    elif problem == "replace-path":
        change["replace_paths"] = [["policy"]]
    elif problem == "hidden-noop":
        plan["resource_changes"][1]["change"]["after"]["name"] = "changed"
    elif problem == "unknown-noop":
        plan["resource_changes"][1]["change"]["after_unknown"] = {"name": True}
    elif problem == "read":
        plan["resource_changes"][1]["change"]["actions"] = ["read"]
    elif problem == "missing-identity":
        plan["prior_state"]["values"]["root_module"]["resources"].pop(0)
    elif problem.startswith("malformed-"):
        change[problem.removeprefix("malformed-")]["policy"] = "not JSON"
    else:
        change["after"]["policy"] = change["before"]["policy"]
    assert checked(plan)
