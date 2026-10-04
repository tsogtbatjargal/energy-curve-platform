"""Evaluate a Terraform plan JSON against the Rego policies with conftest.

Exit codes: 0 pass, 1 policy violation, 2 vacuous or broken run (nothing was really checked).
A gate that silently checks nothing is worse than no gate, so vacuity is a failure.

It also runs the PLAN.md R1 check (iam_approval.py): for a workload stack, an IAM policy unknown
at plan time fails unless a matching, unexpired approval exists. `--stack` is required so that
check can never be skipped by omission.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

EXPECTED_NAMESPACES = frozenset(
    {"terraform.network", "terraform.storage", "terraform.tags", "terraform.iam"}
)


# Must match policy/terraform/lib.rego: only these action lists mean "gone after apply".
# Replacements (delete+create in either order) leave a new object that must be checked,
# and unrecognised future actions count as live so the gate fails closed.
GONE_ACTIONS = frozenset({("delete",), ("forget",)})


def is_live(rc: dict) -> bool:
    return rc.get("mode") == "managed" and tuple(rc["change"]["actions"]) not in GONE_ACTIONS


def live_resource_count(plan: dict) -> int:
    return sum(1 for rc in plan.get("resource_changes", []) if is_live(rc))


def evaluate(plan: dict, results: list[dict]) -> tuple[int, list[str]]:
    """Return (exit_code, report_lines) from a plan and conftest's JSON results."""
    lines: list[str] = []
    if live_resource_count(plan) == 0:
        return 2, ["VACUOUS: plan contains no managed resources to check"]

    seen = {r.get("namespace") for r in results}
    missing = EXPECTED_NAMESPACES - seen
    if missing:
        return 2, [f"BROKEN: policy namespaces not evaluated: {sorted(missing)}"]

    failures = [f for r in results for f in r.get("failures") or []]
    warnings = [w for r in results for w in r.get("warnings") or []]
    lines += [f"WARN  {w['msg']}" for w in warnings]
    lines += [f"FAIL  {f['msg']}" for f in failures]
    lines.append(
        f"{live_resource_count(plan)} resources, {len(seen)} namespaces, "
        f"{len(failures)} failures, {len(warnings)} warnings"
    )
    return (1 if failures else 0), lines


def iam_check(
    plan: dict, stack: str, stack_dir: Path, approvals_path: Path
) -> tuple[int, list[str]]:
    """PLAN.md R1, from iam_approval.py: (exit code, report lines)."""
    import iam_approval

    try:
        result = iam_approval.check(
            plan, stack, stack_dir, iam_approval.load_approvals(approvals_path)
        )
    except iam_approval.GateError as exc:
        return 1, [f"FAIL  R1: {exc}"]
    lines = [f"NOTE  R1: {n}" for n in result.notes]
    lines += [f"FAIL  R1: {f}" for f in result.failures]
    return (1 if result.failures else 0), lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="policy_gate.py")
    parser.add_argument("plan", type=Path)
    parser.add_argument("policy_dir")
    parser.add_argument("--stack", required=True, help="stack name, e.g. bootstrap or batch")
    parser.add_argument("--stack-dir", type=Path, default=Path("."))
    parser.add_argument("--approvals", type=Path, help="default: <policy-dir>/approvals/...")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        return 2
    plan_path, policy_dir = args.plan, args.policy_dir
    approvals = args.approvals or Path(policy_dir) / "approvals" / "iam_unknown.json"
    plan = json.loads(plan_path.read_text())
    conftest = shutil.which("conftest")
    if conftest is None:
        print("BROKEN: conftest not found on PATH", file=sys.stderr)
        return 2
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            conftest,
            "test",
            str(plan_path),
            "--policy",
            policy_dir,
            "--all-namespaces",
            "--output",
            "json",
            "--no-color",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        results = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"BROKEN: conftest produced no JSON\n{proc.stderr}", file=sys.stderr)
        return 2
    code, lines = evaluate(plan, results)
    if code != 2:
        iam_code, iam_lines = iam_check(plan, args.stack, args.stack_dir, approvals)
        code, lines = max(code, iam_code), [*lines, *iam_lines]
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
