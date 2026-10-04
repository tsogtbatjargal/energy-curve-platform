"""PLAN.md R1: IAM policies unknown at plan time need a reviewed, fingerprinted approval.

For a workload stack, an IAM policy whose JSON is unknown at plan time fails the gate unless
`policy/approvals/iam_unknown.json` holds an unexpired approval for the same stack, address and
attribute whose fingerprint matches the one recomputed from this plan (ADR-0017).

The fingerprint is the SHA-256 of a canonical JSON document of:
1. identity: stack, address, resource type, attribute, format version;
2. the dependency closure in the plan's `configuration`: from the attribute's references,
   transitively, every resource and data source reached (full expressions), every module call
   reached (source, version constraint, input expressions), module outputs and variables;
3. inputs: the plan's `variables` values for root variables in the closure, and the resolved
   version of every registry module in the closure (`.terraform/modules/modules.json`);
4. source: the SHA-256 of every `*.tf`/`*.tf.json` in the stack root and in every local module
   in the closure, plus `.terraform.lock.hcl`. Plan JSON omits locals and literals inside
   function calls (`jsonencode`), so only the source covers them. Registry modules in the closure
   are hashed too, from their downloaded copy, so a re-published or moved version tag cannot
   change a policy under an existing approval.

Gate rules beyond the fingerprint: a sensitive variable in the closure fails outright (its value
is never hashed), and every unknown value the policy depends on must be the `arn`, `id` or
`name` of a managed resource in the same stack.

Plans can hold sensitive values in plain text, so nothing here prints plan contents: only
addresses, attribute names, file paths and fingerprints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

FORMAT = "ecp-iam-approval-v1"
MAX_APPROVAL_DAYS = 30
EXEMPT_STACKS = frozenset({"bootstrap"})  # ADR-0006: warning only; it creates no workload roles
UNKNOWN_OK = frozenset({"arn", "id", "name"})
PURE_DATA_SOURCES = frozenset({"aws_iam_policy_document"})  # evaluated locally from inputs

# Resource type -> attributes that hold an IAM policy document.
POLICY_ATTRIBUTES = {
    "aws_iam_policy": ("policy",),
    "aws_iam_role_policy": ("policy",),
    "aws_iam_user_policy": ("policy",),
    "aws_iam_group_policy": ("policy",),
    "aws_iam_role": ("assume_role_policy",),
}
APPROVAL_FIELDS = (
    "stack", "address", "attribute", "fingerprint", "reviewer", "approved_on", "expires_on",
    "reason",
)  # fmt: skip
GONE_ACTIONS = frozenset({("delete",), ("forget",)})  # as in policy_gate.py and lib.rego


class GateError(ValueError):
    """The approvals file or the stack directory cannot be trusted; the gate fails."""


# --- addresses and references -------------------------------------------------------------------

KEY = re.compile(r'\[(?:"(?:[^"\\]|\\.)*"|[0-9]+|[^\]]*)\]')


def strip_keys(address: str) -> str:
    """`module.a["x"].aws_iam_policy.p[0]` -> `module.a.aws_iam_policy.p`."""
    return KEY.sub("", address)


def split_address(address: str) -> tuple[tuple[str, ...], str]:
    """Module path and the resource part: `module.a.module.b.aws_x.y` -> (("a", "b"), "aws_x.y")."""
    parts = strip_keys(address).split(".")
    path = []
    while len(parts) > 2 and parts[0] == "module":
        path.append(parts[1])
        parts = parts[2:]
    return tuple(path), ".".join(parts)


def prefix(path: tuple[str, ...]) -> str:
    return "".join(f"module.{p}." for p in path)


def references(expr: Any) -> Iterator[str]:
    """Every reference string anywhere in an expression tree (blocks nest lists of dicts)."""
    if isinstance(expr, dict):
        for k, v in expr.items():
            if k == "references" and isinstance(v, list):
                yield from (r for r in v if isinstance(r, str))
            else:
                yield from references(v)
    elif isinstance(expr, list):
        for item in expr:
            yield from references(item)


# --- the closure ---------------------------------------------------------------------------------


@dataclass
class Closure:
    nodes: dict[str, Any] = field(default_factory=dict)  # scoped name -> configuration
    root_variables: set[str] = field(default_factory=set)
    sensitive: set[str] = field(default_factory=set)  # scoped variable names
    module_keys: set[str] = field(default_factory=set)  # modules.json keys, e.g. "logs.inner"
    attributes: set[tuple[str, str]] = field(default_factory=set)  # (resource address, attr)
    data_attributes: set[tuple[str, str]] = field(default_factory=set)


class Walker:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.root = plan["configuration"]["root_module"]
        self.closure = Closure()
        self.seen: set[tuple[tuple[str, ...], str]] = set()

    def module(self, path: tuple[str, ...]) -> dict[str, Any]:
        node = self.root
        for name in path:
            node = node["module_calls"][name]["module"]
        return node

    def follow(self, path: tuple[str, ...], expr: Any) -> None:
        for ref in references(expr):
            self.reference(path, strip_keys(ref))

    def reference(self, path: tuple[str, ...], ref: str) -> None:
        if (path, ref) in self.seen:
            return
        self.seen.add((path, ref))
        parts = ref.split(".")
        head = parts[0]
        if head in {"count", "each", "path", "terraform", "self"}:
            return
        if head == "local":  # absent from plan JSON; the source hash covers locals
            self.closure.nodes[f"{prefix(path)}local.{parts[1]}"] = "covered by source"
        elif head == "var":
            self.variable(path, parts[1])
        elif head == "module":
            self.module_call(path, parts[1], parts[2] if len(parts) > 2 else None)
        elif head == "data" and len(parts) >= 3:
            self.resource(path, "data", parts[1], parts[2], parts[3] if len(parts) > 3 else None)
        elif len(parts) >= 2 and "_" in head:
            self.resource(path, "managed", head, parts[1], parts[2] if len(parts) > 2 else None)
        else:
            self.closure.nodes[f"{prefix(path)}{ref}"] = "unresolved reference"

    def variable(self, path: tuple[str, ...], name: str) -> None:
        config = self.module(path).get("variables", {}).get(name, {})
        scoped = f"{prefix(path)}var.{name}"
        self.closure.nodes[scoped] = {"config": config}
        if config.get("sensitive"):
            self.closure.sensitive.add(scoped)
        if not path:
            self.closure.root_variables.add(name)
        else:  # the value comes from the parent module call's input expression
            parent, call = path[:-1], path[-1]
            self.module_call(parent, call, None)

    def module_call(self, path: tuple[str, ...], name: str, output: str | None) -> None:
        call = self.module(path)["module_calls"][name]
        scoped = f"{prefix(path)}module.{name}"
        if scoped not in self.closure.nodes:
            self.closure.nodes[scoped] = {
                "source": call.get("source"),
                "version_constraint": call.get("version_constraint"),
                "expressions": call.get("expressions", {}),
            }
            self.closure.module_keys.add(".".join((*path, name)))
            self.follow(path, call.get("expressions", {}))  # inputs, in the caller's scope
        if output is not None:
            child = (*path, name)
            out = call["module"].get("outputs", {}).get(output, {})
            self.closure.nodes[f"{prefix(child)}output.{output}"] = out
            self.follow(child, out.get("expression", {}))

    def resource(
        self, path: tuple[str, ...], mode: str, rtype: str, name: str, attr: str | None
    ) -> None:
        local = f"data.{rtype}.{name}" if mode == "data" else f"{rtype}.{name}"
        address = f"{prefix(path)}{local}"
        if attr is not None:
            target = self.closure.data_attributes if mode == "data" else self.closure.attributes
            target.add((address, attr))
        if address in self.closure.nodes:
            return
        config = next(
            (r for r in self.module(path).get("resources", []) if r.get("address") == local), None
        )
        if config is None:
            self.closure.nodes[address] = "resource not in configuration"
            return
        self.closure.nodes[address] = {
            "mode": config.get("mode"),
            "type": config.get("type"),
            "expressions": config.get("expressions", {}),
            "count_expression": config.get("count_expression"),
            "for_each_expression": config.get("for_each_expression"),
        }
        self.follow(path, config.get("expressions", {}))
        self.follow(path, config.get("count_expression"))
        self.follow(path, config.get("for_each_expression"))


def closure_of(plan: dict[str, Any], address: str, attribute: str) -> Closure:
    path, local = split_address(address)
    walker = Walker(plan)
    config = next(
        (r for r in walker.module(path).get("resources", []) if r.get("address") == local), None
    )
    if config is None:
        raise GateError(f"{address}: not found in the plan's configuration")
    walker.follow(path, config.get("expressions", {}).get(attribute, {}))
    return walker.closure


# --- fingerprint ----------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def terraform_files(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.iterdir() if p.suffix == ".tf" or p.name.endswith(".tf.json")
    )


def modules_json(stack_dir: Path) -> dict[str, dict[str, Any]]:
    path = stack_dir / ".terraform" / "modules" / "modules.json"
    if not path.exists():
        return {}
    return {m["Key"]: m for m in json.loads(path.read_text())["Modules"]}


def is_local_source(source: str) -> bool:
    return source.startswith(("./", "../"))


def fingerprint_document(
    plan: dict[str, Any], stack: str, stack_dir: Path, address: str, attribute: str
) -> tuple[dict[str, Any], Closure]:
    closure = closure_of(plan, address, attribute)
    rc = next(r for r in plan["resource_changes"] if r["address"] == address)
    modules = modules_json(stack_dir)
    registry: dict[str, Any] = {}
    module_dirs: set[str] = set()
    for key in sorted(closure.module_keys):
        entry = modules.get(key)
        if entry is None:
            raise GateError(
                f"{address}: module {key} missing from modules.json; run terraform init"
            )
        if not (stack_dir / entry["Dir"]).is_dir():
            raise GateError(f"{address}: module {key} not downloaded; run terraform init")
        module_dirs.add(entry["Dir"])
        if not is_local_source(entry.get("Source", "")):
            registry[key] = {"source": entry.get("Source"), "version": entry.get("Version")}
    lock = stack_dir / ".terraform.lock.hcl"
    if not lock.exists():
        raise GateError(f"{stack_dir}: .terraform.lock.hcl is missing")
    files = [*terraform_files(stack_dir), lock]
    for d in sorted(module_dirs):
        files += terraform_files(stack_dir / d)
    source = {p.relative_to(stack_dir).as_posix(): sha256_file(p) for p in files}
    variables = plan.get("variables", {})
    document = {
        "format": FORMAT,
        "identity": {
            "stack": stack,
            "address": address,
            "type": rc["type"],
            "attribute": attribute,
        },  # fmt: skip
        "closure": closure.nodes,
        "inputs": {
            "variables": {
                name: variables.get(name, {}).get("value")
                for name in sorted(closure.root_variables)
                if f"var.{name}" not in closure.sensitive
            },
            "registry_modules": registry,
        },
        "source": source,
    }
    return document, closure


def canonical(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fingerprint(
    plan: dict[str, Any], stack: str, stack_dir: Path, address: str, attribute: str = "policy"
) -> str:
    document, _ = fingerprint_document(plan, stack, stack_dir, address, attribute)
    return "sha256:" + hashlib.sha256(canonical(document)).hexdigest()


# --- approvals ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    stack: str
    address: str
    attribute: str
    fingerprint: str
    reviewer: str
    approved_on: date
    expires_on: date
    reason: str


def load_approvals(path: Path) -> list[Approval]:
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    if set(doc) != {"approvals"} or not isinstance(doc["approvals"], list):
        raise GateError(f"{path}: expected exactly {{'approvals': [...]}}")
    approvals, keys = [], set()
    for i, raw in enumerate(doc["approvals"]):
        where = f"{path} approval {i}"
        if not isinstance(raw, dict) or set(raw) != set(APPROVAL_FIELDS):
            raise GateError(f"{where}: fields must be exactly {list(APPROVAL_FIELDS)}")
        if not all(isinstance(raw[f], str) and raw[f].strip() for f in APPROVAL_FIELDS):
            raise GateError(f"{where}: every field must be a non-empty string")
        try:
            approved, expires = (
                date.fromisoformat(raw["approved_on"]),
                date.fromisoformat(raw["expires_on"]),
            )
        except ValueError as exc:
            raise GateError(f"{where}: dates must be YYYY-MM-DD") from exc
        if not timedelta(0) <= expires - approved <= timedelta(days=MAX_APPROVAL_DAYS):
            raise GateError(f"{where}: expiry must be 0-{MAX_APPROVAL_DAYS} days after approval")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", raw["fingerprint"]):
            raise GateError(f"{where}: fingerprint must be sha256:<64 hex digits>")
        key = (raw["stack"], raw["address"], raw["attribute"])
        if key in keys:
            raise GateError(f"{where}: duplicate approval for {key}")
        keys.add(key)
        approvals.append(Approval(**{**raw, "approved_on": approved, "expires_on": expires}))
    return approvals


# --- the gate -------------------------------------------------------------------------------------


@dataclass
class Result:
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def unknown_policies(plan: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    for rc in plan.get("resource_changes", []):
        if rc.get("mode") != "managed" or tuple(rc["change"]["actions"]) in GONE_ACTIONS:
            continue
        for attribute in POLICY_ATTRIBUTES.get(rc["type"], ()):
            if (rc["change"].get("after_unknown") or {}).get(attribute) is True:
                yield rc["address"], rc["type"], attribute


def attribute_unknown(plan: dict[str, Any], address: str, attribute: str) -> bool:
    """Is this attribute unknown for any planned instance of the (key-stripped) address?"""
    return any(
        strip_keys(rc["address"]) == address
        and (rc["change"].get("after_unknown") or {}).get(attribute) is True
        for rc in plan.get("resource_changes", [])
    )


def check(
    plan: dict[str, Any],
    stack: str,
    stack_dir: Path,
    approvals: list[Approval],
    today: date | None = None,
) -> Result:
    today = today or datetime.now(UTC).date()
    result = Result()
    if stack in EXEMPT_STACKS:
        for address, _, attribute in unknown_policies(plan):
            result.notes.append(f"{address}.{attribute}: unknown at plan time (exempt stack)")
        return result
    for address, _, attribute in unknown_policies(plan):
        what = f"{stack}:{address}.{attribute}"
        try:
            document, closure = fingerprint_document(plan, stack, stack_dir, address, attribute)
        except GateError as exc:
            result.failures.append(f"{what}: {exc}")
            continue
        if closure.sensitive:
            result.failures.append(
                f"{what}: depends on sensitive variable(s) {sorted(closure.sensitive)};"
                " IAM policies must not depend on secrets (no approval can override this)"
            )
            continue
        bad = sorted(
            f"{a}.{attr}"
            for a, attr in closure.attributes
            if attribute_unknown(plan, a, attr) and attr not in UNKNOWN_OK
        ) + sorted(
            f"{a}.{attr}"
            for a, attr in closure.data_attributes
            if attribute_unknown(plan, a, attr) and a.split(".")[-2] not in PURE_DATA_SOURCES
        )
        if bad:
            result.failures.append(
                f"{what}: depends on unknown values that are not a same-stack arn, id or name:"
                f" {bad}"
            )
            continue
        fp = "sha256:" + hashlib.sha256(canonical(document)).hexdigest()
        match = next(
            (a for a in approvals
             if (a.stack, a.address, a.attribute) == (stack, address, attribute)),
            None,
        )  # fmt: skip
        if match is None:
            result.failures.append(f"{what}: unknown at plan time and not approved ({fp})")
        elif match.fingerprint != fp:
            result.failures.append(
                f"{what}: approval fingerprint does not match this plan ({fp}); re-review"
            )
        elif today > match.expires_on:
            result.failures.append(f"{what}: approval expired on {match.expires_on}")
        else:
            result.notes.append(
                f"{what}: approved by {match.reviewer} until {match.expires_on} ({fp})"
            )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "fingerprint"):
        p = sub.add_parser(name)
        p.add_argument("plan", type=Path)
        p.add_argument("--stack", required=True)
        p.add_argument("--stack-dir", type=Path, required=True)
        if name == "check":
            p.add_argument("--approvals", type=Path, required=True)
        else:
            p.add_argument("--address", required=True)
            p.add_argument("--attribute", default="policy")
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text())
    try:
        if args.command == "fingerprint":
            print(fingerprint(plan, args.stack, args.stack_dir, args.address, args.attribute))
            return 0
        result = check(plan, args.stack, args.stack_dir, load_approvals(args.approvals))
    except GateError as exc:
        print(f"FAIL  {exc}")
        return 1
    for line in result.notes:
        print(f"OK    {line}")
    for line in result.failures:
        print(f"FAIL  {line}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
