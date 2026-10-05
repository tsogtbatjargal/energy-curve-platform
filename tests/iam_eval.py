"""A small, strict IAM policy evaluator for offline tests of the R2 policies (ADR-0018).

It covers only what those policies use, and raises on anything else rather than guess:
- Effect, Action/NotAction, Resource/NotResource with `*` and `?` wildcards; actions match
  case-insensitively, resources case-sensitively.
- Condition operators String(Not)Equals, String(Not)Like, Arn(Not)Equals, Arn(Not)Like, each
  over single-valued keys. A key missing from the request makes a positive operator false and
  a negated one true (IAM User Guide, "Condition operators" and "missing keys"). `Null` tests
  presence: "true" holds when the key is absent, "false" when it is present.
- Decision: an explicit Deny in any policy wins; otherwise the identity policies must Allow and,
  when a permissions boundary is given, so must the boundary.

AWS's own evaluator is the authority: the same scenarios are run against
`aws iam simulate-custom-policy` (read-only) and recorded in ADR-0018.
"""

from __future__ import annotations

import re
from typing import Any

ALLOWED, EXPLICIT_DENY, IMPLICIT_DENY = "allowed", "explicitDeny", "implicitDeny"

OPERATORS = {
    "StringEquals": (False, False),  # (negated, wildcards)
    "StringNotEquals": (True, False),
    "StringLike": (False, True),
    "StringNotLike": (True, True),
    "ArnEquals": (False, False),
    "ArnNotEquals": (True, False),
    "ArnLike": (False, True),
    "ArnNotLike": (True, True),
}


def as_list(x: Any) -> list[Any]:
    return x if isinstance(x, list) else [x]


def matches(pattern: str, value: str, *, case: bool = True) -> bool:
    """IAM wildcards: `*` any run of characters, `?` one character, nothing else special."""
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, value, re.DOTALL | (0 if case else re.IGNORECASE)) is not None


def condition_holds(condition: dict[str, Any], context: dict[str, str]) -> bool:
    for operator, keys in condition.items():
        if operator == "Null":
            for key, value in keys.items():
                present = any(k.lower() == key.lower() for k in context)
                if value not in ("true", "false"):
                    raise ValueError(f"Null takes 'true' or 'false', not {value!r}")
                if present == (value == "true"):
                    return False
            continue
        if operator not in OPERATORS:
            raise ValueError(f"unsupported condition operator {operator}")
        negated, wildcards = OPERATORS[operator]
        for key, values in keys.items():
            actual = next((v for k, v in context.items() if k.lower() == key.lower()), None)
            if actual is None:
                if not negated:
                    return False
                continue
            hit = any(matches(v, actual) if wildcards else v == actual for v in as_list(values))
            if hit == negated:
                return False
    return True


def statement_applies(stmt: dict[str, Any], action: str, resource: str, context: dict) -> bool:
    unknown = set(stmt) - {"Sid", "Effect", "Action", "NotAction", "Resource", "NotResource",
                           "Condition"}  # fmt: skip
    if unknown:
        raise ValueError(f"unsupported statement elements {sorted(unknown)}")
    if "Action" in stmt:
        acted = any(matches(a, action, case=False) for a in as_list(stmt["Action"]))
    else:
        acted = not any(matches(a, action, case=False) for a in as_list(stmt["NotAction"]))
    if "Resource" in stmt:
        hit = any(matches(r, resource) for r in as_list(stmt["Resource"]))
    else:
        hit = not any(matches(r, resource) for r in as_list(stmt["NotResource"]))
    return acted and hit and condition_holds(stmt.get("Condition", {}), context)


def effects(policy: dict[str, Any], action: str, resource: str, context: dict) -> set[str]:
    return {
        s["Effect"]
        for s in as_list(policy["Statement"])
        if statement_applies(s, action, resource, context)
    }


def decide(
    identity: list[dict[str, Any]],
    action: str,
    resource: str,
    context: dict[str, str] | None = None,
    boundary: dict[str, Any] | None = None,
) -> str:
    context = context or {}
    policies = [*identity, *([boundary] if boundary else [])]
    if any("Deny" in effects(p, action, resource, context) for p in policies):
        return EXPLICIT_DENY
    allowed = any("Allow" in effects(p, action, resource, context) for p in identity)
    if boundary is not None:
        allowed = allowed and "Allow" in effects(boundary, action, resource, context)
    return ALLOWED if allowed else IMPLICIT_DENY
