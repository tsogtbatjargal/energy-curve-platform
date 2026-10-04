import json
import os
import shutil
from pathlib import Path

import policy_gate
import pytest

PLAN = {
    "resource_changes": [
        {"mode": "managed", "type": "aws_s3_bucket", "change": {"actions": ["create"]}}
    ]
}


def ns_results(failures=None, warnings=None):
    return [
        {
            "namespace": ns,
            "failures": failures if ns == "terraform.iam" else [],
            "warnings": warnings,
        }
        for ns in policy_gate.EXPECTED_NAMESPACES
    ]


def test_clean_plan_passes() -> None:
    code, _ = policy_gate.evaluate(PLAN, ns_results())
    assert code == 0


def test_violation_fails() -> None:
    code, lines = policy_gate.evaluate(PLAN, ns_results(failures=[{"msg": "bad"}]))
    assert code == 1
    assert "FAIL  bad" in lines


def test_empty_plan_is_vacuous() -> None:
    code, _ = policy_gate.evaluate({"resource_changes": []}, ns_results())
    assert code == 2


def rc(actions: list[str], mode: str = "managed") -> dict:
    return {"mode": mode, "change": {"actions": actions}}


@pytest.mark.parametrize(
    "actions",
    [["no-op"], ["update"], ["create"], ["delete", "create"], ["create", "delete"]],
    ids=["unchanged", "update", "create", "replace-destroy-first", "replace-create-first"],
)
def test_live_actions(actions: list[str]) -> None:
    assert policy_gate.is_live(rc(actions))


@pytest.mark.parametrize("actions", [["delete"], ["forget"]])
def test_gone_actions(actions: list[str]) -> None:
    assert not policy_gate.is_live(rc(actions))


def test_unknown_future_action_counts_as_live() -> None:
    assert policy_gate.is_live(rc(["transmogrify"]))


def test_data_sources_not_counted() -> None:
    assert not policy_gate.is_live(rc(["read"], mode="data"))


@pytest.mark.parametrize("order", [["delete", "create"], ["create", "delete"]])
def test_replacements_counted_alongside_unchanged(order: list[str]) -> None:
    plan = {"resource_changes": [rc(["no-op"]), rc(["no-op"]), rc(order), rc(["delete"])]}
    assert policy_gate.live_resource_count(plan) == 3


@pytest.mark.parametrize("order", [["delete", "create"], ["create", "delete"]])
def test_replacement_only_plan_is_not_vacuous(order: list[str]) -> None:
    code, _ = policy_gate.evaluate({"resource_changes": [rc(order)]}, ns_results())
    assert code == 0


def test_delete_only_plan_is_vacuous() -> None:
    plan = {"resource_changes": [{"mode": "managed", "change": {"actions": ["delete"]}}]}
    code, _ = policy_gate.evaluate(plan, ns_results())
    assert code == 2


def test_missing_namespace_is_broken() -> None:
    code, lines = policy_gate.evaluate(PLAN, ns_results()[:-1])
    assert code == 2
    assert "not evaluated" in lines[0]


def conftest_available() -> None:
    if shutil.which("conftest") is None:
        if os.environ.get("ECP_REQUIRE_CONFTEST"):
            pytest.fail("ECP_REQUIRE_CONFTEST is set but conftest is not on PATH")
        pytest.skip("conftest not on PATH")


@pytest.mark.parametrize(
    ("stack", "denied"), [("batch", True), ("demo", True), ("bootstrap", False)]
)
def test_the_stack_name_reaches_the_boundary_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stack: str, denied: bool
) -> None:
    """PLAN.md R2 through the real gate: conftest sees --stack as data.ecp.stack."""
    conftest_available()
    role = {
        "address": "aws_iam_role.task", "mode": "managed", "type": "aws_iam_role",
        "change": {"actions": ["create"], "after": {"name": "ecp-task", "tags": {}},
                   "after_unknown": {}},
    }  # fmt: skip
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"resource_changes": [role]}))
    policy = Path(__file__).parents[1] / "policy"
    code = policy_gate.main(["policy_gate.py", str(plan), str(policy), "--stack", stack,
                             "--stack-dir", str(tmp_path)])  # fmt: skip
    out = capsys.readouterr().out
    assert ("workload roles need permissions_boundary" in out) is denied
    assert f"(stack {stack}," in out or not denied
    assert code == 1 or not denied
