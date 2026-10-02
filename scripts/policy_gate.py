"""Evaluate a Terraform plan JSON against the Rego policies with conftest.

Exit codes: 0 pass, 1 policy violation, 2 vacuous or broken run (nothing was really checked).
A gate that silently checks nothing is worse than no gate, so vacuity is a failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

EXPECTED_NAMESPACES = frozenset(
    {"terraform.network", "terraform.storage", "terraform.tags", "terraform.iam"}
)


def live_resource_count(plan: dict) -> int:
    return sum(
        1
        for rc in plan.get("resource_changes", [])
        if rc.get("mode") == "managed" and "delete" not in rc["change"]["actions"]
    )


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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: policy_gate.py <plan.json> <policy-dir>", file=sys.stderr)
        return 2
    plan_path, policy_dir = Path(argv[1]), argv[2]
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
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
