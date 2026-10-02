import policy_gate

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


def test_delete_only_plan_is_vacuous() -> None:
    plan = {"resource_changes": [{"mode": "managed", "change": {"actions": ["delete"]}}]}
    code, _ = policy_gate.evaluate(plan, ns_results())
    assert code == 2


def test_missing_namespace_is_broken() -> None:
    code, lines = policy_gate.evaluate(PLAN, ns_results()[:-1])
    assert code == 2
    assert "not evaluated" in lines[0]
