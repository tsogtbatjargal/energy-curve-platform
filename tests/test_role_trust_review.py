"""scripts/role_trust_review.py: which trust policies the deploy role's sts:AssumeRole could use."""

import pytest
from role_trust_review import flags

ACCOUNT = "123456789012"


def trust(principal: object, condition: dict | None = None) -> dict:
    stmt = {"Effect": "Allow", "Principal": principal, "Action": "sts:AssumeRole"}
    return {"Statement": [{**stmt, **({"Condition": condition} if condition else {})}]}


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        (trust("*"), ["PRINCIPAL-STAR-NO-CONDITION"]),
        (trust({"AWS": "*"}, {"StringEquals": {"aws:PrincipalOrgID": "o-1"}}), ["PRINCIPAL-STAR"]),
        (trust({"AWS": f"arn:aws:iam::{ACCOUNT}:root"}), ["THIS-ACCOUNT-ROOT"]),
        (trust({"AWS": ACCOUNT}), ["THIS-ACCOUNT-ROOT"]),
        (trust({"AWS": f"arn:aws:iam::{ACCOUNT}:role/x"}), ["THIS-ACCOUNT:role/x"]),
        (trust({"AWS": ["arn:aws:iam::999999999999:root"]}), ["OTHER-ACCOUNT"]),
        (trust({"Federated": "arn:aws:iam::1:oidc-provider/token.actions.githubusercontent.com"},
               {"StringEquals": {"x": "y"}}), ["federated:token.actions.githubusercontent.com"]),
        (trust({"Federated": "arn:aws:iam::1:saml-provider/idp"}), ["federated:idp-NO-CONDITION"]),
        (trust({"Service": ["ecs-tasks.amazonaws.com"]}), ["service:ecs-tasks.amazonaws.com"]),
    ],
)  # fmt: skip
def test_trust_policies_are_classified(doc: dict, expected: list[str]) -> None:
    assert flags(doc, ACCOUNT) == expected


def test_deny_statements_grant_nothing() -> None:
    doc = trust("*")
    doc["Statement"][0]["Effect"] = "Deny"
    assert flags(doc, ACCOUNT) == []
