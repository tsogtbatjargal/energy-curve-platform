"""Pre-apply check of a saved infra/batch plan, one stage at a time (M4c, ADR-0022).

    terraform show -json <saved.tfplan> > <plan.json>
    python scripts/batch_plan_check.py --stage 1 <plan.json>
    python scripts/batch_plan_check.py --stage 2 --digest sha256:<64 hex> <plan.json>
    python scripts/batch_plan_check.py --stage 3 --digest sha256:<64 hex> <plan.json>

Run it from the clean checkout the saved plan was made from: it also reads infra/batch's source.
It runs after policy_gate.py --stack batch (ADR-0006 rules, PLAN.md R1 and R2), not instead of it.

Both stages:
- a sound plan: not errored, complete, no deferred changes, no drift, no output changes;
- ca-central-1, the caller is expected_account_id, and that is not the management account;
- no excluded resource (NAT gateway, Elastic IP, KMS key, secret, SSM parameter), no endpoint but
  the S3 gateway, no ingress rule, and no `ingress` block on the task's security group;
- the ECS cluster depends on the explicit ECS service-linked role (ADR-0022);
- the only module is the data bucket's, `terraform-aws-modules/s3-bucket/aws` at exactly 5.16.2;
  no override file and no JSON configuration in the stack source.

`--stage 1` (image_digest = ""): from an empty state, exactly the STAGE1 creates, each with its
reviewed settings; every role and policy is the reviewed template for this account, known at plan
time, and every role carries the workload boundary.

`--stage 2` (image_digest = the approved digest): every stage-1 resource unchanged, so no IAM
changes at all; exactly the STAGE2 creates; the digest is in the repository (the image lookup
was read at plan time); both steps run that digest, synthetic data only, with no key; no reserved
concurrency; the schedule is disabled; the alarm notifies the alerts topic.

`--stage 3` (image_digest = the approved digest, schedule_enabled = true): every stage-1 and stage-2
resource unchanged except one in-place update of the schedule, which may change only its state
(to ENABLED) and its target (the universal Step Functions StartExecution target, whose input is
the exact reviewed bytes: the state machine, `Name` = Scheduler's execution ID, an empty input).
The schedule's other settings stay as reviewed.

Plans hold sensitive values in plain text, so the report prints addresses, actions and attribute
names only, never values (the alert address in particular).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import policy_templates
from bootstrap_plan_check import UNCHANGED, dig, prior, variable

STACK = Path(__file__).resolve().parents[1] / "infra" / "batch"
POLICIES = STACK / "policies"
REGION = "ca-central-1"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MODULE = ("data_bucket", "terraform-aws-modules/s3-bucket/aws", "5.16.2")
ROLES = ("stage", "task", "exec", "sfn", "scheduler")
EXCLUDED_TYPES = frozenset(
    {
        "aws_nat_gateway", "aws_eip", "aws_kms_key", "aws_kms_alias",
        "aws_secretsmanager_secret", "aws_secretsmanager_secret_version", "aws_ssm_parameter",
        "aws_lambda_provisioned_concurrency_config",
    }
)  # fmt: skip
INGRESS_TYPES = frozenset({"aws_vpc_security_group_ingress_rule", "aws_security_group_rule"})

_B = "module.data_bucket.aws_s3_bucket"
STAGE1 = frozenset(
    {
        f"{_B}.this[0]", f"{_B}_public_access_block.this[0]", f"{_B}_ownership_controls.this[0]",
        f"{_B}_server_side_encryption_configuration.this[0]", f"{_B}_versioning.this[0]",
        f"{_B}_lifecycle_configuration.this[0]", f"{_B}_policy.this[0]",
        "aws_ecr_repository.batch", "aws_ecr_lifecycle_policy.batch",
        "aws_ecr_repository_policy.batch",
        "aws_vpc.batch", "aws_default_security_group.batch", "aws_internet_gateway.batch",
        'aws_subnet.public["a"]', 'aws_subnet.public["b"]', "aws_route_table.public",
        "aws_route.internet", 'aws_route_table_association.public["a"]',
        'aws_route_table_association.public["b"]', "aws_vpc_endpoint.s3",
        "aws_security_group.task", "aws_vpc_security_group_egress_rule.https",
        "aws_iam_service_linked_role.ecs", "aws_ecs_cluster.batch",
        "aws_ecs_cluster_capacity_providers.batch",
        "aws_cloudwatch_log_group.lambda", "aws_cloudwatch_log_group.pipeline",
        "aws_sns_topic.alerts", "aws_sns_topic_subscription.email",
        *(f'aws_iam_role.batch["{r}"]' for r in ROLES),
        *(f'aws_iam_role_policy.batch["{r}"]' for r in ROLES),
    }
)  # fmt: skip
STAGE2 = frozenset(
    {
        "aws_lambda_function.stage[0]", "aws_ecs_task_definition.pipeline[0]",
        "aws_sfn_state_machine.batch[0]", "aws_scheduler_schedule.daily[0]",
        "aws_cloudwatch_metric_alarm.failures[0]",
    }
)  # fmt: skip
# (address, attribute path, required value): each stage-1 create's reviewed settings. The bucket
# name and the policies depend on the account and are checked in check_stage1_create.
UNIVERSAL_TARGET = "arn:aws:scheduler:::aws-sdk:sfn:startExecution"
SCHEDULE = "aws_scheduler_schedule.daily[0]"


def schedule_input(account: str) -> str:
    """The target input, byte for byte: written by `format` in workflow.tf, not `jsonencode`,
    which would escape `<` and `>` and leave Scheduler no keyword to replace."""
    machine = f"arn:aws:states:{REGION}:{account}:stateMachine:ecp-batch"
    return (
        '{"StateMachineArn":"' + machine + '","Name":"<aws.scheduler.execution-id>","Input":"{}"}'
    )


STAGE1_SETTINGS = [
    (f"{_B}.this[0]", ("force_destroy",), False),
    *[(f"{_B}_public_access_block.this[0]", (flag,), True)
      for flag in ("block_public_acls", "block_public_policy", "ignore_public_acls",
                   "restrict_public_buckets")],
    (f"{_B}_ownership_controls.this[0]", ("rule", 0, "object_ownership"), "BucketOwnerEnforced"),
    (f"{_B}_server_side_encryption_configuration.this[0]",
     ("rule", 0, "apply_server_side_encryption_by_default", 0, "sse_algorithm"), "AES256"),
    (f"{_B}_versioning.this[0]", ("versioning_configuration", 0, "status"), "Enabled"),
    (f"{_B}_lifecycle_configuration.this[0]", ("rule", 0, "id"), "expire-staging"),
    (f"{_B}_lifecycle_configuration.this[0]", ("rule", 0, "status"), "Enabled"),
    (f"{_B}_lifecycle_configuration.this[0]", ("rule", 0, "filter", 0, "prefix"),
     "store/staging/"),
    (f"{_B}_lifecycle_configuration.this[0]", ("rule", 0, "expiration", 0, "days"), 7),
    (f"{_B}_lifecycle_configuration.this[0]", ("rule", 1, "id"), "expire-old-versions"),
    (f"{_B}_lifecycle_configuration.this[0]", ("rule", 1, "status"), "Enabled"),
    (f"{_B}_lifecycle_configuration.this[0]",
     ("rule", 1, "noncurrent_version_expiration", 0, "noncurrent_days"), 30),
    (f"{_B}_lifecycle_configuration.this[0]",
     ("rule", 1, "abort_incomplete_multipart_upload", 0, "days_after_initiation"), 1),
    ("aws_ecr_repository.batch", ("name",), "ecp-batch"),
    ("aws_ecr_repository.batch", ("image_tag_mutability",), "IMMUTABLE"),
    ("aws_ecr_repository.batch", ("force_delete",), False),
    ("aws_ecr_repository.batch", ("image_scanning_configuration", 0, "scan_on_push"), True),
    ("aws_ecr_repository.batch", ("encryption_configuration", 0, "encryption_type"), "AES256"),
    ('aws_subnet.public["a"]', ("map_public_ip_on_launch",), False),
    ('aws_subnet.public["b"]', ("map_public_ip_on_launch",), False),
    ("aws_vpc_endpoint.s3", ("vpc_endpoint_type",), "Gateway"),
    ("aws_vpc_endpoint.s3", ("service_name",), f"com.amazonaws.{REGION}.s3"),
    ("aws_security_group.task", ("name",), "ecp-batch-task"),
    ("aws_vpc_security_group_egress_rule.https", ("ip_protocol",), "tcp"),
    ("aws_vpc_security_group_egress_rule.https", ("from_port",), 443),
    ("aws_vpc_security_group_egress_rule.https", ("to_port",), 443),
    ("aws_iam_service_linked_role.ecs", ("aws_service_name",), "ecs.amazonaws.com"),
    ("aws_iam_service_linked_role.ecs", ("custom_suffix",), None),
    ("aws_ecs_cluster.batch", ("name",), "ecp-batch"),
    ("aws_ecs_cluster.batch", ("setting", 0, "name"), "containerInsights"),
    ("aws_ecs_cluster.batch", ("setting", 0, "value"), "disabled"),
    ("aws_ecs_cluster_capacity_providers.batch", ("capacity_providers",), ["FARGATE"]),
    ("aws_cloudwatch_log_group.lambda", ("name",), "/aws/lambda/ecp-batch-stage"),
    ("aws_cloudwatch_log_group.lambda", ("retention_in_days",), 14),
    ("aws_cloudwatch_log_group.pipeline", ("name",), "/ecs/ecp-batch-pipeline"),
    ("aws_cloudwatch_log_group.pipeline", ("retention_in_days",), 14),
    ("aws_sns_topic.alerts", ("name",), "ecp-batch-alerts"),
    ("aws_sns_topic_subscription.email", ("protocol",), "email"),
]  # fmt: skip


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


def check_policy(address: str, change: dict[str, Any], attribute: str, want: Any) -> list[str]:
    if known_document(change, attribute) != want:
        return [f"{address}: {attribute} is not the reviewed template, known at plan time"]
    return []


def check_stage1_create(address: str, change: dict[str, Any], account: str) -> list[str]:
    after = change.get("after") or {}
    problems = [
        f"{address}: {'.'.join(map(str, path))} is not the reviewed value"
        for a, path, want in STAGE1_SETTINGS
        if a == address and dig(after, path) != want
    ]
    if address == f"{_B}.this[0]" and after.get("bucket") != f"ecp-data-{account}-{REGION}":
        problems.append(f"{address}: bucket is not ecp-data-<account>-{REGION}")
    if address == "aws_sns_topic_subscription.email":
        endpoint = after.get("endpoint")
        if not (isinstance(endpoint, str) and endpoint.count("@") == 1):
            problems.append(f"{address}: endpoint is not one email address")
    if address == "aws_ecr_repository_policy.batch":
        problems += check_policy(address, change, "policy", render("ecr-repository", account))
    for role in ROLES:
        if address == f'aws_iam_role.batch["{role}"]':
            if after.get("name") != f"ecp-batch-{role}":
                problems.append(f"{address}: name is not ecp-batch-{role}")
            boundary = f"arn:aws:iam::{account}:policy/ecp-workload-boundary"
            if after.get("permissions_boundary") != boundary:
                problems.append(f"{address}: permissions_boundary is not the workload boundary")
            problems += check_policy(address, change, "assume_role_policy",
                                     render(f"{role}-trust", account))  # fmt: skip
        if address == f'aws_iam_role_policy.batch["{role}"]':
            if after.get("name") != f"ecp-batch-{role}":
                problems.append(f"{address}: name is not ecp-batch-{role}")
            problems += check_policy(address, change, "policy", render(f"{role}-policy", account))
    return problems


def check_stage2_create(
    address: str, change: dict[str, Any], account: str, digest: str, topic: str | None
) -> list[str]:
    after = change.get("after") or {}
    uri = f"{account}.dkr.ecr.{REGION}.amazonaws.com/ecp-batch@{digest}"
    store = {"ECP_SOURCE": "synthetic", "ECP_STORE_URL": f"s3://ecp-data-{account}-{REGION}/store"}

    def role(name: str) -> str:
        return f"arn:aws:iam::{account}:role/ecp-batch-{name}"

    want: dict[str, dict[tuple[Any, ...], Any]] = {
        "aws_lambda_function.stage[0]": {
            ("function_name",): "ecp-batch-stage", ("package_type",): "Image",
            ("image_uri",): uri, ("role",): role("stage"), ("memory_size",): 512,
            ("timeout",): 60, ("environment", 0, "variables"): store, ("vpc_config",): [],
        },
        "aws_ecs_task_definition.pipeline[0]": {
            ("family",): "ecp-batch-pipeline", ("cpu",): "512", ("memory",): "1024",
            ("requires_compatibilities",): ["FARGATE"], ("network_mode",): "awsvpc",
            ("execution_role_arn",): role("exec"), ("task_role_arn",): role("task"),
        },
        "aws_sfn_state_machine.batch[0]": {
            ("name",): "ecp-batch", ("type",): "STANDARD", ("role_arn",): role("sfn"),
        },
        "aws_scheduler_schedule.daily[0]": {
            ("name",): "ecp-batch-daily", ("state",): "DISABLED",
            ("target", 0, "role_arn"): role("scheduler"),
        },
        "aws_cloudwatch_metric_alarm.failures[0]": {
            ("alarm_name",): "ecp-batch-failures", ("alarm_actions",): [topic],
        },
    }  # fmt: skip
    problems = [
        f"{address}: {'.'.join(map(str, path))} is not the reviewed value"
        for path, value in want[address].items()
        if dig(after, path) != value or (path == ("alarm_actions",) and topic is None)
    ]
    if address == "aws_lambda_function.stage[0]" and after.get(
        "reserved_concurrent_executions"
    ) not in (None, -1):
        problems.append(f"{address}: reserved_concurrent_executions must be unset (ADR-0022)")
    if address == "aws_ecs_task_definition.pipeline[0]":
        problems += check_container(address, after.get("container_definitions"), uri, store)
    return problems


def check_schedule_update(change: dict[str, Any], account: str, role: str) -> list[str]:
    """Stage 3: the one in-place update. State to ENABLED and the universal target; nothing else."""
    after, before = change.get("after") or {}, change.get("before") or {}
    want: dict[tuple[Any, ...], Any] = {
        ("name",): "ecp-batch-daily", ("state",): "ENABLED",
        ("schedule_expression",): "cron(0 12 * * ? *)",
        ("schedule_expression_timezone",): "America/Toronto",
        ("flexible_time_window", 0, "mode"): "OFF",
        ("target", 0, "arn"): UNIVERSAL_TARGET, ("target", 0, "role_arn"): role,
        ("target", 0, "input"): schedule_input(account),
        ("target", 0, "retry_policy", 0, "maximum_retry_attempts"): 2,
    }  # fmt: skip
    problems = [
        f"{SCHEDULE}: {'.'.join(map(str, path))} is not the reviewed value"
        for path, value in want.items()
        if dig(after, path) != value
    ]
    if (change.get("after_unknown") or {}).get("target"):
        problems.append(f"{SCHEDULE}: the target is not known at plan time")
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    target_before, target_after = (
        (before.get("target") or [{}])[0],
        (after.get("target") or [{}])[0],
    )
    target_changed = sorted(
        k
        for k in set(target_before) | set(target_after)
        if target_before.get(k) != target_after.get(k)
    )
    extra = [k for k in changed if k not in ("state", "target")]
    extra += [f"target.{k}" for k in target_changed if k not in ("arn", "input")]
    if extra:
        problems.append(
            f"{SCHEDULE}: changes more than the state and the target ({', '.join(extra)})"
        )
    return problems


def check_container(address: str, definitions: Any, uri: str, env: dict[str, str]) -> list[str]:
    try:
        containers = json.loads(definitions)
    except (TypeError, json.JSONDecodeError):
        return [f"{address}: container_definitions are not known at plan time"]
    if not (isinstance(containers, list) and len(containers) == 1):
        return [f"{address}: exactly one container is expected"]
    c = containers[0]
    problems = []
    if c.get("name") != "pipeline" or c.get("image") != uri:
        problems.append(f"{address}: the container is not `pipeline` on the approved digest")
    if c.get("entryPoint") != ["python", "-m", "energy_curves.cloud"]:
        problems.append(f"{address}: entryPoint is not the pipeline entry point")
    got = {e.get("name"): e.get("value") for e in c.get("environment") or []}
    if got != env or c.get("secrets"):
        problems.append(f"{address}: environment is not synthetic-only with no key or secret")
    return problems


def check_common(plan: dict[str, Any]) -> tuple[list[str], str | None]:
    """Problems that hold for both stages, and the account the plan runs in."""
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
    account = (prior(plan, "data.aws_caller_identity.current") or {}).get("account_id")
    master = (prior(plan, "data.aws_organizations_organization.this") or {}).get(
        "master_account_id"
    )
    if not account or account != variable(plan, "expected_account_id"):
        problems.append("the caller is not expected_account_id")
    if not master or master == account:
        problems.append("this is the organization's management account, not a member")
    for rc in plan.get("resource_changes", []):
        address, after = rc["address"], rc["change"].get("after") or {}
        if rc["change"]["actions"] in (["delete"], ["forget"]):
            continue
        if rc.get("type") in EXCLUDED_TYPES:
            problems.append(f"{address}: excluded from the batch stack (ADR-0022)")
        if rc.get("type") in INGRESS_TYPES:
            problems.append(f"{address}: no ingress rule in the batch stack (ADR-0004)")
        if rc.get("type") == "aws_vpc_endpoint" and after.get("vpc_endpoint_type") != "Gateway":
            problems.append(f"{address}: only the S3 gateway endpoint (ADR-0004)")
    for name, out in (plan.get("output_changes") or {}).items():
        if out["actions"] not in UNCHANGED:
            problems.append(f"unexpected output change: {'+'.join(out['actions'])} {name}")
    problems += check_configuration((plan.get("configuration") or {}).get("root_module") or {})
    return problems, account


def check_configuration(root: dict[str, Any]) -> list[str]:
    problems = []
    resources = {r["address"]: r for r in root.get("resources") or []}
    cluster = resources.get("aws_ecs_cluster.batch") or {}
    if "aws_iam_service_linked_role.ecs" not in (cluster.get("depends_on") or []):
        problems.append(
            "aws_ecs_cluster.batch: depends_on must name aws_iam_service_linked_role.ecs"
        )
    sg = resources.get("aws_security_group.task")
    if sg is None or "ingress" in (sg.get("expressions") or {}):
        problems.append("aws_security_group.task: no ingress block (ADR-0004)")
    name, source, version = MODULE
    for call_name, call in sorted((root.get("module_calls") or {}).items()):
        if call_name != name:
            problems.append(f"module call not allowed: {call_name}")
        elif call.get("source") != source or call.get("version_constraint") != version:
            problems.append(f"module data_bucket must be {source} at exactly {version}")
    if name not in (root.get("module_calls") or {}):
        problems.append("module data_bucket is missing")
    return problems


def check_source(stack_dir: Path) -> list[str]:
    """Over exactly the files Terraform loads: no override file, no JSON configuration, and no
    module block but the pinned data bucket. Run from the clean checkout of the saved plan."""
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


def check(
    plan: dict[str, Any], stage: int, digest: str | None = None, stack_dir: Path | None = None
) -> tuple[list[str], list[str]]:
    """(problems, report) for a saved plan of the given stage."""
    from org_plan_check import already_owned

    problems, account = check_common(plan)
    problems += check_source(stack_dir or STACK)
    report: list[str] = []
    topic = (prior(plan, "aws_sns_topic.alerts") or {}).get("arn")
    if stage == 1:
        if variable(plan, "image_digest") != "":
            problems.append('stage 1 needs image_digest = ""')
        if already_owned(plan):
            problems.append("the state is not empty: stage 1 starts from no managed resources")
        expected, unchanged = STAGE1, frozenset()
    elif stage == 2:
        if not (isinstance(digest, str) and DIGEST.match(digest)):
            problems.append("stage 2 needs --digest sha256:<64 lowercase hex>")
            digest = ""
        if variable(plan, "image_digest") != digest:
            problems.append("image_digest is not the approved digest")
        image = prior(plan, "data.aws_ecr_image.batch[0]") or {}
        if not digest or image.get("image_digest") != digest:
            problems.append(
                "data.aws_ecr_image.batch[0]: the approved digest was not found in the repository"
                " at plan time"
            )
        expected, unchanged = STAGE2, STAGE1
    elif stage == 3:
        if not (isinstance(digest, str) and DIGEST.match(digest)):
            problems.append("stage 3 needs --digest sha256:<64 lowercase hex>")
            digest = ""
        if variable(plan, "image_digest") != digest:
            problems.append("image_digest is not the approved digest")
        # `terraform show -json` records a -var value as a string; a bool never appears, but
        # `1 == True` in Python, so test for True by identity.
        enabled = variable(plan, "schedule_enabled")
        if not (enabled is True or enabled == "true"):
            problems.append("stage 3 needs schedule_enabled = true")
        image = prior(plan, "data.aws_ecr_image.batch[0]") or {}
        if not digest or image.get("image_digest") != digest:
            problems.append(
                "data.aws_ecr_image.batch[0]: the approved digest was not found in the repository"
                " at plan time"
            )
        expected, unchanged = frozenset({SCHEDULE}), (STAGE1 | STAGE2) - {SCHEDULE}
    else:
        return [f"unknown stage {stage}"], report

    seen: set[str] = set()
    for rc in plan.get("resource_changes", []):
        address, change = rc["address"], rc["change"]
        actions = change["actions"]
        if stage == 3 and address == SCHEDULE and actions != ["update"]:
            if actions not in UNCHANGED:  # a no-op is reported below as the missing update
                report.append(f"{'+'.join(actions):8} {address}")
                problems.append(f"unexpected change: {'+'.join(actions)} {address}")
            continue
        if actions in UNCHANGED and change.get("importing") is None:
            seen.add(address)
            continue
        report.append(f"{'+'.join(actions):8} {address}")
        if stage == 3 and address == SCHEDULE and actions == ["update"]:
            seen.add(address)
            if account:
                role = f"arn:aws:iam::{account}:role/ecp-batch-scheduler"
                problems += check_schedule_update(change, account, role)
        elif address in expected and actions == ["create"] and change.get("importing") is None:
            seen.add(address)
            if account and stage == 1:
                problems += check_stage1_create(address, change, account)
            elif account:
                problems += check_stage2_create(address, change, account, digest or "", topic)
        else:
            problems.append(f"unexpected change: {'+'.join(actions)} {address}")
    for address in sorted(expected - seen):
        problems.append(f"missing change: {'update' if stage == 3 else 'create'} {address}")
    for address in sorted(unchanged - seen):
        problems.append(f"missing stage-1 resource: {address} must be in the plan, unchanged")
    report.append(
        f"summary: stage {stage}, {len(expected) if stage < 3 else 0} to add,"
        f" {1 if stage == 3 else 0} to change, 0 to destroy"
        if not problems
        else "summary: not the reviewed shape"
    )
    return problems, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batch_plan_check.py")
    parser.add_argument("--stage", type=int, choices=(1, 2, 3), required=True)
    parser.add_argument("--digest", help="stages 2 and 3: the approved image digest")
    parser.add_argument("--stack-dir", type=Path, default=STACK)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args(argv)
    problems, report = check(
        json.loads(args.plan.read_text()), args.stage, args.digest, args.stack_dir
    )
    print("\n".join([*report, *(f"FAIL  {p}" for p in problems)] or ["(empty plan)"]))
    if not problems:
        print(f"OK    stage {args.stage}: the reviewed shape")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
