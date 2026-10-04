"""The offline IAM evaluator behind the R2 tests: its own semantics, on hand-written policies."""

import pytest
from iam_eval import ALLOWED, EXPLICIT_DENY, IMPLICIT_DENY, decide

ROLE = "arn:aws:iam::123456789012:role/ecp-task"
B = "arn:aws:iam::123456789012:policy/ecp-workload-boundary"


def policy(*statements: dict) -> dict:
    return {"Version": "2012-10-17", "Statement": list(statements)}


ALLOW_ROLES = {"Effect": "Allow", "Action": "iam:*Role*", "Resource": "arn:aws:iam::*:role/ecp-*"}


def test_allow_wildcards_and_implicit_deny() -> None:
    assert decide([policy(ALLOW_ROLES)], "iam:CreateRole", ROLE) == ALLOWED
    assert decide([policy(ALLOW_ROLES)], "IAM:createrole", ROLE) == ALLOWED  # actions: any case
    assert decide([policy(ALLOW_ROLES)], "iam:CreateRole", ROLE.upper()) == IMPLICIT_DENY
    assert decide([policy(ALLOW_ROLES)], "iam:CreateUser", ROLE) == IMPLICIT_DENY
    assert decide([policy(ALLOW_ROLES)], "iam:CreateRole", ROLE.replace("ecp-", "x-")) == (
        IMPLICIT_DENY
    )


def test_an_explicit_deny_wins() -> None:
    deny = {"Effect": "Deny", "Action": "iam:CreateRole", "Resource": "*"}
    assert decide([policy(ALLOW_ROLES, deny)], "iam:CreateRole", ROLE) == EXPLICIT_DENY
    assert decide([policy(ALLOW_ROLES), policy(deny)], "iam:CreateRole", ROLE) == EXPLICIT_DENY


def test_a_missing_key_fails_positive_operators_and_passes_negated_ones() -> None:
    needs = {**ALLOW_ROLES, "Condition": {"StringEquals": {"iam:PermissionsBoundary": B}}}
    unless = {"Effect": "Deny", "Action": "iam:CreateRole", "Resource": "*",
              "Condition": {"StringNotEquals": {"iam:PermissionsBoundary": B}}}  # fmt: skip
    with_b = {"iam:PermissionsBoundary": B}
    assert decide([policy(needs)], "iam:CreateRole", ROLE) == IMPLICIT_DENY
    assert decide([policy(needs)], "iam:CreateRole", ROLE, with_b) == ALLOWED
    assert decide([policy(ALLOW_ROLES, unless)], "iam:CreateRole", ROLE) == EXPLICIT_DENY
    assert decide([policy(ALLOW_ROLES, unless)], "iam:CreateRole", ROLE, with_b) == ALLOWED
    other = {"iam:PermissionsBoundary": B + "-v2"}
    assert decide([policy(ALLOW_ROLES, unless)], "iam:CreateRole", ROLE, other) == EXPLICIT_DENY


def test_condition_keys_are_case_insensitive_and_values_are_not() -> None:
    unless = {"Effect": "Deny", "Action": "iam:CreateRole", "Resource": "*",
              "Condition": {"StringNotEquals": {"iam:permissionsboundary": B}}}  # fmt: skip
    assert decide([policy(ALLOW_ROLES, unless)], "iam:CreateRole", ROLE,
                  {"iam:PermissionsBoundary": B}) == ALLOWED  # fmt: skip
    assert decide([policy(ALLOW_ROLES, unless)], "iam:CreateRole", ROLE,
                  {"iam:PermissionsBoundary": B.upper()}) == EXPLICIT_DENY  # fmt: skip


def test_not_action_and_arn_like() -> None:
    freeze = {"Effect": "Deny", "NotAction": ["iam:Get*", "iam:List*"], "Resource": B}
    allow = {"Effect": "Allow", "Action": "iam:*", "Resource": "*"}
    assert decide([policy(allow, freeze)], "iam:CreatePolicyVersion", B) == EXPLICIT_DENY
    assert decide([policy(allow, freeze)], "iam:GetPolicyVersion", B) == ALLOWED
    like = {"ArnLike": {"iam:PolicyARN": "arn:aws:iam::aws:policy/Admin*"}}
    admin = {"Effect": "Deny", "Action": "iam:AttachRolePolicy", "Resource": "*", "Condition": like}
    ctx = {"iam:PolicyARN": "arn:aws:iam::aws:policy/AdministratorAccess"}
    assert decide([policy(allow, admin)], "iam:AttachRolePolicy", ROLE, ctx) == EXPLICIT_DENY


def test_a_boundary_caps_what_the_identity_allows() -> None:
    everything = {"Effect": "Allow", "Action": "*", "Resource": "*"}
    boundary = policy({"Effect": "Allow", "Action": "s3:*", "Resource": "*"})
    assert decide([policy(everything)], "s3:GetObject", "*", boundary=boundary) == ALLOWED
    assert decide([policy(everything)], "iam:CreateUser", "*", boundary=boundary) == IMPLICIT_DENY
    deny_in_boundary = policy({"Effect": "Deny", "Action": "s3:*", "Resource": "*"})
    assert decide([policy(everything)], "s3:GetObject", "*", boundary=deny_in_boundary) == (
        EXPLICIT_DENY
    )


@pytest.mark.parametrize(
    "stmt",
    [
        {"Effect": "Allow", "Action": "*", "Resource": "*",
         "Condition": {"NumericEquals": {"aws:x": "1"}}},
        {"Effect": "Allow", "Action": "*", "Resource": "*", "Principal": "*"},
    ],
)  # fmt: skip
def test_anything_unsupported_raises_rather_than_guesses(stmt: dict) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        decide([policy(stmt)], "s3:GetObject", "*")
