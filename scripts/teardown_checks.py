"""The closing teardown's two plan shapes, shared by the batch and Glue plan checks.

`prepare` is the in-place apply that lets Terraform empty the stack: `force_destroy` on the bucket
(and `force_delete` on the batch ECR repository) go from false to true, and nothing else changes.
`destroy` is the teardown itself: exactly the stack's resources as plain deletes, from a state
that holds those flags as true, so the provider empties the bucket and the repository (versions
and delete markers included) before deleting them. Any resource the owner keeps (the ECS
service-linked role) is taken out of state first and must not appear in the plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MODES = ("prepare", "destroy")


def _changed(change: dict[str, Any]) -> set[str]:
    before, after = change.get("before") or {}, change.get("after") or {}
    return {k for k in {*before, *after} if before.get(k) != after.get(k)}


def check_changes(
    plan: dict[str, Any],
    mode: str,
    live: frozenset[str],
    flags: Mapping[str, str],
    kept: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """(problems, report) for the plan's resource changes against the stack's resources `live`.

    `flags` maps an address to its emptying attribute. `kept` resources are in `live` for
    `prepare` (still in state, unchanged) and forbidden in `destroy` (already out of state)."""
    problems: list[str] = []
    report: list[str] = []
    want = live if mode == "prepare" else live - kept
    seen: set[str] = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        if rc.get("mode") == "data":
            continue
        report.append(f"{'+'.join(actions):8} {address}")
        if mode == "destroy" and address in kept:
            problems.append(f"{address}: kept; remove it from state before planning the destroy")
            continue
        if address not in want or change.get("importing"):
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
            continue
        flag = flags.get(address)
        if mode == "prepare":
            if flag:
                problems += _check_flag_update(address, change, flag)
            elif actions != ["no-op"]:
                problems.append(f"unexpected change: {'+'.join(actions)} {address}")
                continue
        else:
            if actions != ["delete"]:
                problems.append(f"unexpected change: {'+'.join(actions)} {address}")
                continue
            if flag and (change.get("before") or {}).get(flag) is not True:
                problems.append(
                    f"{address}: {flag} is not true in the state; apply `prepare` first"
                )
        seen.add(address)
    for address in sorted(want - seen):
        problems.append(
            f"missing change: {'update' if address in flags else 'no-op'} {address}"
            if mode == "prepare"
            else f"missing change: delete {address}"
        )
    changes = len(flags) if mode == "prepare" else 0
    destroys = len(want) if mode == "destroy" else 0
    report.append(
        f"summary: 0 to add, {changes} to change, {destroys} to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def _check_flag_update(address: str, change: dict[str, Any], flag: str) -> list[str]:
    before, after = change.get("before") or {}, change.get("after") or {}
    problems = []
    if change["actions"] != ["update"]:
        problems.append(f"unexpected change: {'+'.join(change['actions'])} {address}")
        return problems
    if before.get(flag) is not False or after.get(flag) is not True:
        problems.append(f"{address}: {flag} must go from false to true")
    if _changed(change) - {flag}:
        problems.append(f"{address}: only {flag} may change")
    if any((change.get("after_unknown") or {}).values()):
        problems.append(f"{address}: no attribute may be unknown after the update")
    return problems
