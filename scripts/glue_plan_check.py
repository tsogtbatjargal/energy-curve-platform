"""Pre-apply check of a saved infra/glue plan (M5b, ADR-0023).

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/build_glue_bundle.py     # the plan was made with this bundle
    python scripts/glue_plan_check.py [--change create|update] <plan.json>

Run it from the clean checkout the saved plan was made from: it rebuilds the bundle and reads
infra/glue's source. It runs after policy_gate.py --stack glue (ADR-0006 rules, PLAN.md R1 and R2),
not instead of it. One stage: nothing here depends on an image digest.

The plan must be exactly the 14 creates of the reviewed stack, from an empty state:
- the stack's own bucket (the pinned registry module) with its reviewed settings, never the batch
  store; the two S3 objects, whose hashes equal the repository's `jobs/glue/shape_job.py` and the
  bundle `scripts/build_glue_bundle.py` builds, so Glue runs exactly the reviewed source;
- the job role: the workload boundary, and trust and inline policy equal to the reviewed templates
  for this account, known at plan time;
- the two log groups (Glue 5 writes to <prefix>/output and <prefix>/error), and the Glue job with
  every setting and argument exactly as reviewed (Glue 5.1, two G.1X workers, a 10-minute
  cap, no retries, no connection, no Data Catalog, no PyPI modules;
  the job's six own arguments are all passed, since the script defaults none; no metrics and no
  Glue 4 continuous-logging arguments, which Glue 5 ignores, only --custom-logGroup-prefix).
Nothing else: no crawler, catalog, trigger, endpoint, security configuration, KMS key, alarm,
network or any other service. Also: a sound plan (not errored, complete, no deferred changes, no
drift, no output changes), ca-central-1, the caller is expected_account_id and not the management
account, and the source has exactly one module call, the bucket, at 5.16.2, with no override file
or JSON configuration.

`--change update` (the job's rerun fix) is for the applied stack with no drift (a refresh-only
apply records the provider's read-back first): exactly one in-place update, the script object, and
thirteen no-ops. The script's ETag moves to the repository script's hash (its version_id is known
only after apply) and no other attribute changes. The job, both log groups, the bundle, the role
and its policy and the bucket do not change. Every resource, changed or not, must still be the
reviewed one.

Plans hold sensitive values in plain text, so the report prints addresses, actions and attribute
names only, never values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import build_glue_bundle
import policy_templates
from bootstrap_plan_check import UNCHANGED, dig, prior, variable

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "infra" / "glue"
POLICIES = STACK / "policies"
REGION = "ca-central-1"
MODULE = ("bucket", "terraform-aws-modules/s3-bucket/aws", "5.16.2")
VARIABLES = frozenset({"region", "owner", "expected_account_id"})
_B = "module.bucket.aws_s3_bucket"
JOB, ROLE, ROLE_POLICY = "aws_glue_job.shape", "aws_iam_role.glue", "aws_iam_role_policy.glue"
LOG_OUT, LOG_ERR = "aws_cloudwatch_log_group.output", "aws_cloudwatch_log_group.error"
LOG_PREFIX = "/aws-glue/ecp-shape"
SCRIPT, BUNDLE = "aws_s3_object.script", "aws_s3_object.bundle"
CREATES = frozenset(
    {
        JOB, ROLE, ROLE_POLICY, LOG_OUT, LOG_ERR, SCRIPT, BUNDLE,
        f"{_B}.this[0]", f"{_B}_lifecycle_configuration.this[0]",
        f"{_B}_ownership_controls.this[0]", f"{_B}_policy.this[0]",
        f"{_B}_public_access_block.this[0]",
        f"{_B}_server_side_encryption_configuration.this[0]", f"{_B}_versioning.this[0]",
    }
)  # fmt: skip
DATA = frozenset({"data.aws_caller_identity.current", "data.aws_organizations_organization.this"})
# What the objects must be: the repository's script, and the bundle built from the repository.
OBJECTS = {
    SCRIPT: ("glue/shape_job.py", "./../../jobs/glue/shape_job.py"),
    BUNDLE: ("glue/energy_curves_m5.zip", "./build/energy_curves_m5.zip"),
}
LIFECYCLE = f"{_B}_lifecycle_configuration.this[0]"
SETTINGS = [
    (f"{_B}.this[0]", ("force_destroy",), False),
    *[(f"{_B}_public_access_block.this[0]", (flag,), True)
      for flag in ("block_public_acls", "block_public_policy", "ignore_public_acls",
                   "restrict_public_buckets")],
    (f"{_B}_ownership_controls.this[0]", ("rule", 0, "object_ownership"), "BucketOwnerEnforced"),
    (f"{_B}_server_side_encryption_configuration.this[0]",
     ("rule", 0, "apply_server_side_encryption_by_default", 0, "sse_algorithm"), "AES256"),
    (f"{_B}_versioning.this[0]", ("versioning_configuration", 0, "status"), "Enabled"),
    (LIFECYCLE, ("rule", 0, "id"), "expire-committer-temp"),
    (LIFECYCLE, ("rule", 0, "status"), "Enabled"),
    (LIFECYCLE, ("rule", 0, "filter", 0, "prefix"), "m5/tmp/"),
    (LIFECYCLE, ("rule", 0, "expiration", 0, "days"), 7),
    (LIFECYCLE, ("rule", 1, "id"), "expire-old-versions"),
    (LIFECYCLE, ("rule", 1, "status"), "Enabled"),
    (LIFECYCLE, ("rule", 1, "noncurrent_version_expiration", 0, "noncurrent_days"), 30),
    (LIFECYCLE, ("rule", 1, "abort_incomplete_multipart_upload", 0, "days_after_initiation"), 1),
    (LOG_OUT, ("name",), f"{LOG_PREFIX}/output"),
    (LOG_OUT, ("retention_in_days",), 14),
    (LOG_ERR, ("name",), f"{LOG_PREFIX}/error"),
    (LOG_ERR, ("retention_in_days",), 14),
    (ROLE, ("name",), "ecp-glue-shape"),
    (ROLE_POLICY, ("name",), "ecp-glue-shape"),
    (JOB, ("name",), "ecp-glue-shape"),
    (JOB, ("glue_version",), "5.1"),
    (JOB, ("worker_type",), "G.1X"),
    (JOB, ("number_of_workers",), 2),
    (JOB, ("timeout",), 10),
    (JOB, ("max_retries",), 0),
    (JOB, ("execution_class",), "STANDARD"),
    (JOB, ("command", 0, "name"), "glueetl"),
    (JOB, ("command", 0, "python_version"), "3"),
    (JOB, ("execution_property", 0, "max_concurrent_runs"), 1),
]  # fmt: skip


JOB_OWN_ARGUMENTS = (
    "--start",
    "--end",
    "--window-start",
    "--window-end",
    "--partitions",
    "--output",
)
# The update (a job that can be rerun): only the script object changes, to the repository's script.
UPDATES = {SCRIPT}
APPLIED = frozenset({JOB, ROLE, ROLE_POLICY, LOG_OUT, LOG_ERR, SCRIPT, BUNDLE})


def arguments(bucket: str) -> dict[str, str]:
    return {
        "--job-language": "python",
        "--extra-py-files": f"s3://{bucket}/glue/energy_curves_m5.zip",
        "--custom-logGroup-prefix": LOG_PREFIX,
        "--start": "1983-01-03",
        "--end": "2024-04-05",
        "--window-start": "2014-01-01",
        "--window-end": "2024-04-05",
        "--partitions": "8",
        "--output": f"s3://{bucket}/m5/shape",
    }


def render(name: str, account: str) -> Any:
    return policy_templates.render(name, {"account_id": account, "region": REGION}, POLICIES)


def known_document(change: dict[str, Any], attribute: str) -> Any:
    """The planned policy document, or None when it is missing or unknown at plan time."""
    if (change.get("after_unknown") or {}).get(attribute):
        return None
    value = (change.get("after") or {}).get(attribute)
    try:
        return json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError:
        return None


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()  # S3's ETag, not a secret


def repository_hashes() -> dict[str, str]:
    script = (ROOT / "jobs" / "glue" / "shape_job.py").read_bytes()
    return {SCRIPT: md5(script), BUNDLE: md5(build_glue_bundle.build_bytes())}


UNSET_AFTER_READ_BACK = ("connections", "security_configuration", "non_overridable_arguments")


def check_create(address: str, change: dict[str, Any], account: str) -> list[str]:
    after, unknown = change.get("after") or {}, change.get("after_unknown") or {}
    bucket = f"ecp-glue-{account}-{REGION}"
    problems = [
        f"{address}: {'.'.join(map(str, path))} is not the reviewed value"
        for a, path, want in SETTINGS
        if a == address and dig(after, path) != want
    ]
    if address == f"{_B}.this[0]" and after.get("bucket") != bucket:
        problems.append(f"{address}: bucket is not ecp-glue-<account>-{REGION}")
    if address in OBJECTS:
        key, source = OBJECTS[address]
        if after.get("key") != key or after.get("source") != source:
            problems.append(f"{address}: key or source is not the reviewed one")
        want = repository_hashes()[address]
        if unknown.get("etag") or after.get("etag") != want:
            problems.append(f"{address}: etag is not the hash of the repository's file")
    if address == ROLE:
        if (
            after.get("permissions_boundary")
            != f"arn:aws:iam::{account}:policy/ecp-workload-boundary"
        ):
            problems.append(f"{address}: permissions_boundary is not the workload boundary")
        if known_document(change, "assume_role_policy") != render("glue-trust", account):
            problems.append(
                f"{address}: assume_role_policy is not the reviewed template, known at plan time"
            )
    if address == ROLE_POLICY and known_document(change, "policy") != render(
        "glue-policy", account
    ):
        problems.append(f"{address}: policy is not the reviewed template, known at plan time")
    if address == JOB:
        for attribute in UNSET_AFTER_READ_BACK:  # null, or the empty value the provider reads back
            if after.get(attribute) not in (None, [], {}, ""):
                problems.append(f"{address}: {attribute} is not the reviewed value")
        if after.get("default_arguments") != arguments(bucket):
            problems.append(f"{address}: default_arguments are not exactly the reviewed ones")
        script = f"s3://{bucket}/glue/shape_job.py"
        if dig(after, ("command", 0, "script_location")) != script:
            problems.append(f"{address}: command.script_location is not the uploaded script")
    return problems


def check_common(plan: dict[str, Any], mode: str = "create") -> tuple[list[str], str | None]:
    problems: list[str] = []
    if plan.get("errored"):
        problems.append("the plan errored")
    if plan.get("complete") is False:
        problems.append("the plan is incomplete")
    for key in ("deferred_changes", "resource_drift"):
        for item in plan.get(key) or []:
            problems.append(f"{key}: {item.get('address', '?')}")
    if variable(plan, "region") != REGION:
        problems.append(f"region must be {REGION}")
    for name in sorted(set(plan.get("variables") or {}) - VARIABLES):
        problems.append(f"unexpected variable: {name}")
    account = (prior(plan, "data.aws_caller_identity.current") or {}).get("account_id")
    master = (prior(plan, "data.aws_organizations_organization.this") or {}).get(
        "master_account_id"
    )
    if not account or account != variable(plan, "expected_account_id"):
        problems.append("the caller is not expected_account_id")
    if not master or master == account:
        problems.append("this is the organization's management account, not a member")
    resources = (plan.get("prior_state") or {}).get("values", {}).get("root_module", {})
    owned = {r["address"] for r in resources.get("resources", [])} - DATA
    if mode == "create" and owned:
        problems.append("the state is not empty: this stack starts from no managed resources")
    if mode == "update" and owned != APPLIED:
        problems.append(
            "the state is not the applied stack: it must hold exactly the reviewed ones"
        )
    for name, out in (plan.get("output_changes") or {}).items():
        if out["actions"] not in UNCHANGED:
            problems.append(f"unexpected output change: {'+'.join(out['actions'])} {name}")
    root = (plan.get("configuration") or {}).get("root_module") or {}
    name, source, version = MODULE
    for call_name, call in sorted((root.get("module_calls") or {}).items()):
        if call_name != name:
            problems.append(f"module call not allowed: {call_name}")
        elif call.get("source") != source or call.get("version_constraint") != version:
            problems.append(f"module {name} must be {source} at exactly {version}")
    if name not in (root.get("module_calls") or {}):
        problems.append(f"module {name} is missing")
    return problems, account


def check_source(stack_dir: Path) -> list[str]:
    """Over exactly the files Terraform loads: no override file, no JSON configuration, and no
    module block but the pinned bucket. Run from the clean checkout of the saved plan."""
    import hcl2
    from hcl2.utils import SerializationOptions
    from phase1b_plan_check import config_ext, config_files, is_override

    problems = []
    for p in config_files(stack_dir):
        if is_override(p.name):
            problems.append(f"override files are not allowed in the stack: {p.name}")
            continue
        if config_ext(p.name) == ".tf.json":
            problems.append(f"JSON configuration is not allowed in the stack: {p.name}")
            continue
        try:
            with p.open() as fh:
                doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
        except Exception:  # noqa: BLE001 - any parse failure means the source is unverified
            problems.append(f"cannot read the stack source: {p.name}")
            continue
        for block in doc.get("module", []):
            for call, body in block.items():
                call = call.strip('"')
                got = (call, str(body.get("source", "")).strip('"'),
                       str(body.get("version", "")).strip('"'))  # fmt: skip
                if got != MODULE:
                    problems.append(f"module block not allowed: {call} ({p.name})")
    return problems


def check_update(address: str, change: dict[str, Any]) -> list[str]:
    """The script object's in-place update: its ETag moves (to the repository script's hash, which
    check_create verifies) and its version_id, known only after apply. Nothing else differs."""
    before, after = change.get("before") or {}, change.get("after") or {}
    unknown = {k for k, v in (change.get("after_unknown") or {}).items() if v}
    differing = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    problems = []
    if unknown - {"version_id"}:
        problems.append(f"{address}: only version_id may be unknown after apply")
    if differing - {"etag", "version_id"}:
        problems.append(
            f"{address}: changes {', '.join(sorted(differing - {'etag', 'version_id'}))}"
        )
    if before.get("etag") == after.get("etag"):
        problems.append(f"{address}: etag does not change")
    return problems


def expected_actions(mode: str) -> dict[str, str]:
    return {a: ("update" if mode == "update" and a in UPDATES else
                "no-op" if mode == "update" else "create") for a in CREATES}  # fmt: skip


def check(
    plan: dict[str, Any], stack_dir: Path | None = None, mode: str = "create"
) -> tuple[list[str], list[str]]:
    """(problems, report) for a saved plan of infra/glue: `create` from an empty state, or
    `update` (the script object alone) on the applied stack."""
    if mode not in ("create", "update"):
        return [f"unknown change mode {mode}"], []
    problems, account = check_common(plan, mode)
    problems += check_source(stack_dir or STACK)
    report: list[str] = []
    want = expected_actions(mode)
    seen: set[str] = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        report.append(f"{'+'.join(actions):8} {address}")
        if (
            want.get(address) is None
            or actions != [want[address]]
            or change.get("importing") is not None
        ):
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
            continue
        seen.add(address)
        if mode == "update" and address in UPDATES:
            problems += check_update(address, change)
        if account:
            problems += check_create(address, change, account)
    for address in sorted(set(want) - seen):
        problems.append(f"missing change: {want[address]} {address}")
    counts = [sum(v == a for v in want.values()) for a in ("create", "update", "delete")]
    report.append(
        "summary: {} to add, {} to change, {} to destroy".format(*counts)
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glue_plan_check.py")
    parser.add_argument("--stack-dir", type=Path, default=STACK)
    parser.add_argument("--change", choices=("create", "update"), default="create")
    parser.add_argument("plan", type=Path)
    args = parser.parse_args(argv)
    problems, report = check(json.loads(args.plan.read_text()), args.stack_dir, args.change)
    print("\n".join([*report, *(f"FAIL  {p}" for p in problems)] or ["(empty plan)"]))
    if not problems:
        print("OK    the reviewed shape")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
