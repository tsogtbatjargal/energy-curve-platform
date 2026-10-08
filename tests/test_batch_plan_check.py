"""M4c (ADR-0022): each stage's plan must be exactly the reviewed shape before its apply.

The plans are synthetic but shaped like `terraform show -json` (Terraform 1.15.8) for the batch
stack in ecp-workloads: stage 1 from an empty state, stage 2 with stage 1 applied. Account IDs and
the digest are placeholders.
"""

import copy
import json
from pathlib import Path
from typing import Any

import batch_plan_check as bpc
import policy_templates
import pytest

MEMBER, MANAGEMENT, REGION = "333333333333", "111111111111", "ca-central-1"
DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "cd" * 32
BUCKET = f"ecp-data-{MEMBER}-{REGION}"
REPO_URL = f"{MEMBER}.dkr.ecr.{REGION}.amazonaws.com/ecp-batch"
BOUNDARY = f"arn:aws:iam::{MEMBER}:policy/ecp-workload-boundary"
TOPIC = f"arn:aws:sns:{REGION}:{MEMBER}:ecp-batch-alerts"
POLICIES = Path(__file__).parents[1] / "infra" / "batch" / "policies"
VALUES = {"account_id": MEMBER, "region": REGION}
ROLES = ("stage", "task", "exec", "sfn", "scheduler")
MOD = "module.data_bucket"


def template(name: str) -> str:
    return json.dumps(policy_templates.render(name, VALUES, POLICIES))


def role_arn(name: str) -> str:
    return f"arn:aws:iam::{MEMBER}:role/ecp-batch-{name}"


def stage1_creates() -> dict[str, dict[str, Any]]:
    """Address -> planned `after` of every stage-1 create, with the reviewed settings."""
    s3 = f"{MOD}.aws_s3_bucket"
    after: dict[str, dict[str, Any]] = {
        f"{s3}.this[0]": {"bucket": BUCKET, "force_destroy": False},
        f"{s3}_public_access_block.this[0]": {
            "block_public_acls": True, "block_public_policy": True,
            "ignore_public_acls": True, "restrict_public_buckets": True},
        f"{s3}_ownership_controls.this[0]": {"rule": [{"object_ownership": "BucketOwnerEnforced"}]},
        f"{s3}_server_side_encryption_configuration.this[0]": {"rule": [{
            "apply_server_side_encryption_by_default": [
                {"sse_algorithm": "AES256", "kms_master_key_id": None}]}]},
        f"{s3}_versioning.this[0]": {"versioning_configuration": [{"status": "Enabled"}]},
        f"{s3}_lifecycle_configuration.this[0]": {"rule": [
            {"id": "expire-staging", "status": "Enabled",
             "filter": [{"prefix": "store/staging/"}], "expiration": [{"days": 7}]},
            {"id": "expire-old-versions", "status": "Enabled",
             "noncurrent_version_expiration": [{"noncurrent_days": 30}],
             "abort_incomplete_multipart_upload": [{"days_after_initiation": 1}]}]},
        f"{s3}_policy.this[0]": {},
        "aws_ecr_repository.batch": {
            "name": "ecp-batch", "image_tag_mutability": "IMMUTABLE", "force_delete": False,
            "image_scanning_configuration": [{"scan_on_push": True}],
            "encryption_configuration": [{"encryption_type": "AES256", "kms_key": None}]},
        "aws_ecr_lifecycle_policy.batch": {},
        "aws_ecr_repository_policy.batch": {"policy": template("ecr-repository")},
        "aws_vpc.batch": {"cidr_block": "10.42.0.0/16"},
        "aws_default_security_group.batch": {"ingress": [], "egress": []},
        "aws_internet_gateway.batch": {},
        'aws_subnet.public["a"]': {"map_public_ip_on_launch": False},
        'aws_subnet.public["b"]': {"map_public_ip_on_launch": False},
        "aws_route_table.public": {},
        "aws_route.internet": {"destination_cidr_block": "0.0.0.0/0"},
        'aws_route_table_association.public["a"]': {},
        'aws_route_table_association.public["b"]': {},
        "aws_vpc_endpoint.s3": {"vpc_endpoint_type": "Gateway",
                                "service_name": f"com.amazonaws.{REGION}.s3"},
        "aws_security_group.task": {"name": "ecp-batch-task"},
        "aws_vpc_security_group_egress_rule.https": {
            "cidr_ipv4": "0.0.0.0/0", "ip_protocol": "tcp", "from_port": 443, "to_port": 443},
        "aws_iam_service_linked_role.ecs": {"aws_service_name": "ecs.amazonaws.com",
                                            "custom_suffix": None},
        "aws_ecs_cluster.batch": {"name": "ecp-batch",
                                  "setting": [{"name": "containerInsights", "value": "disabled"}]},
        "aws_ecs_cluster_capacity_providers.batch": {"capacity_providers": ["FARGATE"]},
        "aws_cloudwatch_log_group.lambda": {"name": "/aws/lambda/ecp-batch-stage",
                                            "retention_in_days": 14},
        "aws_cloudwatch_log_group.pipeline": {"name": "/ecs/ecp-batch-pipeline",
                                              "retention_in_days": 14},
        "aws_sns_topic.alerts": {"name": "ecp-batch-alerts"},
        "aws_sns_topic_subscription.email": {"protocol": "email",
                                             "endpoint": "alerts@example.invalid"},
    }  # fmt: skip
    for r in ROLES:
        after[f'aws_iam_role.batch["{r}"]'] = {
            "name": f"ecp-batch-{r}", "permissions_boundary": BOUNDARY,
            "assume_role_policy": template(f"{r}-trust")}  # fmt: skip
        after[f'aws_iam_role_policy.batch["{r}"]'] = {
            "name": f"ecp-batch-{r}", "policy": template(f"{r}-policy")}  # fmt: skip
    return after


def stage2_creates(digest: str = DIGEST) -> dict[str, dict[str, Any]]:
    uri = f"{REPO_URL}@{digest}"
    store = f"s3://{BUCKET}/store"
    container = {
        "name": "pipeline", "image": uri, "essential": True,
        "entryPoint": ["python", "-m", "energy_curves.cloud"],
        "environment": [{"name": "ECP_SOURCE", "value": "synthetic"},
                        {"name": "ECP_STORE_URL", "value": store}]}  # fmt: skip
    return {
        "aws_lambda_function.stage[0]": {
            "function_name": "ecp-batch-stage", "package_type": "Image", "image_uri": uri,
            "role": role_arn("stage"), "memory_size": 512, "timeout": 60,
            "reserved_concurrent_executions": None, "vpc_config": [],
            "environment": [{"variables": {"ECP_SOURCE": "synthetic", "ECP_STORE_URL": store}}]},
        "aws_ecs_task_definition.pipeline[0]": {
            "family": "ecp-batch-pipeline", "cpu": "512", "memory": "1024",
            "requires_compatibilities": ["FARGATE"], "network_mode": "awsvpc",
            "execution_role_arn": role_arn("exec"), "task_role_arn": role_arn("task"),
            "container_definitions": json.dumps([container])},
        "aws_sfn_state_machine.batch[0]": {"name": "ecp-batch", "type": "STANDARD",
                                           "role_arn": role_arn("sfn")},
        "aws_scheduler_schedule.daily[0]": {"name": "ecp-batch-daily", "state": "DISABLED",
                                            "target": [{"role_arn": role_arn("scheduler")}]},
        "aws_cloudwatch_metric_alarm.failures[0]": {"alarm_name": "ecp-batch-failures",
                                                    "alarm_actions": [TOPIC]},
    }  # fmt: skip


def rc(address: str, actions: list[str], after: Any, before: Any = None) -> dict[str, Any]:
    parts = address.split(".")
    module, local = (
        (".".join(parts[:2]), ".".join(parts[2:])) if parts[0] == "module" else ("", address)
    )
    type_ = local.split(".")[0]
    name = local.split(".")[1].split("[")[0]
    out = {"address": address, "mode": "managed", "type": type_, "name": name,
           "provider_name": "registry.terraform.io/hashicorp/aws",
           "change": {"actions": actions, "before": before, "after": after,
                      "after_unknown": {}}}  # fmt: skip
    if module:
        out["module_address"] = module
    return out


def data(address: str, values: dict[str, Any]) -> dict[str, Any]:
    return {"address": address, "mode": "data", "type": address.split(".")[1], "values": values}


def configuration() -> dict[str, Any]:
    return {"root_module": {
        "resources": [
            {"address": "aws_ecs_cluster.batch", "mode": "managed", "type": "aws_ecs_cluster",
             "name": "batch", "expressions": {"name": {"references": ["local.name"]}},
             "depends_on": ["aws_iam_service_linked_role.ecs"]},
            {"address": "aws_security_group.task", "mode": "managed",
             "type": "aws_security_group", "name": "task",
             "expressions": {"name": {"constant_value": "ecp-batch-task"},
                             "vpc_id": {"references": ["aws_vpc.batch.id", "aws_vpc.batch"]}}},
        ],
        "module_calls": {"data_bucket": {"source": "terraform-aws-modules/s3-bucket/aws",
                                         "version_constraint": "5.16.2", "module": {}}},
    }}  # fmt: skip


def plan(stage: int = 1, digest: str = DIGEST) -> dict[str, Any]:
    prior = [
        data("data.aws_caller_identity.current", {"account_id": MEMBER}),
        data("data.aws_organizations_organization.this", {"master_account_id": MANAGEMENT}),
    ]
    if stage == 1:
        changes = [rc(a, ["create"], v) for a, v in stage1_creates().items()]
    else:
        existing = stage1_creates()
        existing["aws_sns_topic.alerts"]["arn"] = TOPIC
        changes = [rc(a, ["no-op"], v, v) for a, v in existing.items()]
        changes += [rc(a, ["create"], v) for a, v in stage2_creates(digest).items()]
        prior += [{"address": a, "mode": "managed", "values": v} for a, v in existing.items()]
        prior.append(
            data(
                "data.aws_ecr_image.batch[0]",
                {"image_digest": digest, "repository_name": "ecp-batch"},
            )
        )
    return {
        "format_version": "1.2", "terraform_version": "1.15.8", "errored": False,
        "complete": True,
        "variables": {"region": {"value": REGION}, "expected_account_id": {"value": MEMBER},
                      "image_digest": {"value": "" if stage == 1 else digest},
                      "schedule_enabled": {"value": False}, "owner": {"value": "o"},
                      "alert_email": {"value": "alerts@example.invalid"}},
        "prior_state": {"values": {"root_module": {"resources": prior}}},
        "resource_changes": changes,
        "output_changes": {},
        "configuration": configuration(),
    }  # fmt: skip


def change(p: dict[str, Any], address: str) -> dict[str, Any]:
    return next(r for r in p["resource_changes"] if r["address"] == address)


@pytest.fixture
def stack(tmp_path: Path) -> Path:
    """A copy of the batch stack's .tf files (never a tfvars file)."""
    copy_dir = tmp_path / "batch"
    copy_dir.mkdir()
    for tf in bpc.STACK.glob("*.tf"):
        (copy_dir / tf.name).write_text(tf.read_text())
    return copy_dir


def run(p: dict[str, Any], stage: int, stack: Path, digest: str | None = DIGEST) -> list[str]:
    problems, _ = bpc.check(p, stage, digest if stage == 2 else None, stack)
    return problems


# --- the reviewed shapes pass -----------------------------------------------------------------


def test_the_reviewed_stage1_plan_passes(stack: Path) -> None:
    problems, report = bpc.check(plan(1), 1, None, stack)
    assert problems == []
    assert "summary: stage 1, 39 to add, 0 to change, 0 to destroy" in report


def test_the_reviewed_stage2_plan_passes(stack: Path) -> None:
    problems, report = bpc.check(plan(2), 2, DIGEST, stack)
    assert problems == []
    assert "summary: stage 2, 5 to add, 0 to change, 0 to destroy" in report


def test_the_report_never_prints_the_alert_address(stack: Path) -> None:
    for stage in (1, 2):
        problems, report = bpc.check(plan(stage), stage, DIGEST if stage == 2 else None, stack)
        assert "alerts@example.invalid" not in "\n".join(problems + report)


# --- stage 1 ----------------------------------------------------------------------------------


def test_stage1_refuses_a_digest(stack: Path) -> None:
    p = plan(1)
    p["variables"]["image_digest"]["value"] = DIGEST
    assert 'stage 1 needs image_digest = ""' in run(p, 1, stack)


def test_stage1_refuses_a_state_that_is_not_empty(stack: Path) -> None:
    p = plan(1)
    p["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_vpc.batch", "mode": "managed", "values": {}}
    )
    assert any("state is not empty" in x for x in run(p, 1, stack))


def test_stage1_refuses_a_missing_create(stack: Path) -> None:
    p = plan(1)
    p["resource_changes"] = [
        r for r in p["resource_changes"] if r["address"] != "aws_iam_service_linked_role.ecs"
    ]
    assert "missing change: create aws_iam_service_linked_role.ecs" in run(p, 1, stack)


@pytest.mark.parametrize(
    "address", ["aws_lambda_function.stage[0]", "aws_scheduler_schedule.daily[0]"]
)
def test_stage1_refuses_a_stage2_resource(stack: Path, address: str) -> None:
    p = plan(1)
    p["resource_changes"].append(rc(address, ["create"], stage2_creates()[address]))
    assert f"unexpected change: create {address}" in run(p, 1, stack)


def test_stage1_refuses_an_update_or_delete(stack: Path) -> None:
    p = plan(1)
    change(p, "aws_vpc.batch")["change"]["actions"] = ["delete", "create"]
    assert "unexpected change: delete+create aws_vpc.batch" in run(p, 1, stack)


def test_a_role_without_the_boundary_is_refused(stack: Path) -> None:
    p = plan(1)
    change(p, 'aws_iam_role.batch["sfn"]')["change"]["after"]["permissions_boundary"] = None
    assert any(
        'aws_iam_role.batch["sfn"]' in x and "permissions_boundary" in x for x in run(p, 1, stack)
    )


def test_a_role_policy_other_than_the_template_is_refused(stack: Path) -> None:
    p = plan(1)
    doc = json.loads(template("task-policy"))
    doc["Statement"][0]["Action"].append("s3:DeleteObject")
    change(p, 'aws_iam_role_policy.batch["task"]')["change"]["after"]["policy"] = json.dumps(doc)
    assert any('aws_iam_role_policy.batch["task"]' in x and "policy" in x for x in run(p, 1, stack))


def test_a_trust_policy_other_than_the_template_is_refused(stack: Path) -> None:
    p = plan(1)
    after = change(p, 'aws_iam_role.batch["exec"]')["change"]["after"]
    after["assume_role_policy"] = template("stage-trust")
    assert any(
        'aws_iam_role.batch["exec"]' in x and "assume_role_policy" in x for x in run(p, 1, stack)
    )


def test_an_unknown_policy_is_refused(stack: Path) -> None:
    p = plan(1)
    c = change(p, 'aws_iam_role_policy.batch["stage"]')["change"]
    del c["after"]["policy"]
    c["after_unknown"] = {"policy": True}
    assert any('aws_iam_role_policy.batch["stage"]' in x for x in run(p, 1, stack))


def test_a_changed_ecr_repository_policy_is_refused(stack: Path) -> None:
    p = plan(1)
    doc = json.loads(template("ecr-repository"))
    del doc["Statement"][0]["Condition"]
    change(p, "aws_ecr_repository_policy.batch")["change"]["after"]["policy"] = json.dumps(doc)
    assert any("aws_ecr_repository_policy.batch" in x for x in run(p, 1, stack))


@pytest.mark.parametrize(("address", "key", "bad"), [
    ("aws_ecr_repository.batch", "image_tag_mutability", "MUTABLE"),
    ("aws_iam_service_linked_role.ecs", "aws_service_name", "ecs-tasks.amazonaws.com"),
    (f"{MOD}.aws_s3_bucket.this[0]", "bucket", "ecp-data-other"),
    (f"{MOD}.aws_s3_bucket.this[0]", "force_destroy", True),
    ("aws_vpc_endpoint.s3", "vpc_endpoint_type", "Interface"),
    ('aws_subnet.public["b"]', "map_public_ip_on_launch", True),
    ("aws_vpc_security_group_egress_rule.https", "from_port", 0),
    ("aws_cloudwatch_log_group.pipeline", "retention_in_days", 0),
    ("aws_sns_topic_subscription.email", "protocol", "http"),
])  # fmt: skip
def test_a_reviewed_setting_that_differs_is_refused(
    stack: Path, address: str, key: str, bad: Any
) -> None:
    p = plan(1)
    change(p, address)["change"]["after"][key] = bad
    assert any(x.startswith(f"{address}:") for x in run(p, 1, stack))


def test_bucket_versioning_and_lifecycle_are_checked(stack: Path) -> None:
    p = plan(1)
    change(p, f"{MOD}.aws_s3_bucket_versioning.this[0]")["change"]["after"][
        "versioning_configuration"
    ] = [{"status": "Suspended"}]
    rules = change(p, f"{MOD}.aws_s3_bucket_lifecycle_configuration.this[0]")["change"]["after"]
    rules["rule"][0]["expiration"] = [{"days": 70}]
    problems = run(p, 1, stack)
    assert any("aws_s3_bucket_versioning" in x for x in problems)
    assert any("aws_s3_bucket_lifecycle_configuration" in x for x in problems)


# --- both stages ------------------------------------------------------------------------------


@pytest.mark.parametrize("stage", [1, 2])
def test_the_wrong_account_is_refused(stack: Path, stage: int) -> None:
    p = plan(stage)
    p["prior_state"]["values"]["root_module"]["resources"][0]["values"]["account_id"] = "4" * 12
    assert "the caller is not expected_account_id" in run(p, stage, stack)


@pytest.mark.parametrize("stage", [1, 2])
def test_the_management_account_is_refused(stack: Path, stage: int) -> None:
    p = plan(stage)
    p["prior_state"]["values"]["root_module"]["resources"][1]["values"]["master_account_id"] = (
        MEMBER
    )
    assert "this is the organization's management account, not a member" in run(p, stage, stack)


@pytest.mark.parametrize("stage", [1, 2])
def test_another_region_is_refused(stack: Path, stage: int) -> None:
    p = plan(stage)
    p["variables"]["region"]["value"] = "us-east-1"
    assert "region must be ca-central-1" in run(p, stage, stack)


@pytest.mark.parametrize("key", ["errored", "complete", "deferred_changes", "resource_drift"])
def test_an_unsound_plan_is_refused(stack: Path, key: str) -> None:
    p = plan(1)
    p[key] = {"errored": True, "complete": False}.get(key, [{"address": "aws_vpc.batch"}])
    assert run(p, 1, stack) != []


@pytest.mark.parametrize(
    "type_",
    ["aws_nat_gateway", "aws_kms_key", "aws_secretsmanager_secret", "aws_ssm_parameter", "aws_eip"],
)
def test_excluded_resource_types_are_refused(stack: Path, type_: str) -> None:
    p = plan(1)
    p["resource_changes"].append(rc(f"{type_}.x", ["create"], {}))
    assert any(f"{type_}.x" in x and "excluded" in x for x in run(p, 1, stack))


def test_an_ingress_rule_is_refused(stack: Path) -> None:
    p = plan(1)
    p["resource_changes"].append(rc("aws_vpc_security_group_ingress_rule.x", ["create"], {}))
    assert any("aws_vpc_security_group_ingress_rule.x" in x for x in run(p, 1, stack))


def test_an_ingress_block_on_the_task_security_group_is_refused(stack: Path) -> None:
    p = plan(1)
    sg = p["configuration"]["root_module"]["resources"][1]
    sg["expressions"]["ingress"] = [{"from_port": {"constant_value": 443}}]
    assert any("aws_security_group.task" in x and "ingress" in x for x in run(p, 1, stack))


def test_the_cluster_must_depend_on_the_service_linked_role(stack: Path) -> None:
    p = plan(1)
    p["configuration"]["root_module"]["resources"][0]["depends_on"] = []
    assert any("aws_ecs_cluster.batch" in x and "depends_on" in x for x in run(p, 1, stack))


@pytest.mark.parametrize(("source", "version"), [
    ("terraform-aws-modules/s3-bucket/aws", "~> 5.16"),
    ("terraform-aws-modules/s3-bucket/aws", "5.16.1"),
    ("git::https://example.invalid/s3-bucket.git", "5.16.2"),
])  # fmt: skip
def test_only_the_pinned_bucket_module_is_allowed(stack: Path, source: str, version: str) -> None:
    p = plan(1)
    call = p["configuration"]["root_module"]["module_calls"]["data_bucket"]
    call["source"], call["version_constraint"] = source, version
    assert any("module" in x and "data_bucket" in x for x in run(p, 1, stack))


def test_another_module_call_is_refused(stack: Path) -> None:
    p = plan(1)
    p["configuration"]["root_module"]["module_calls"]["extra"] = {
        "source": "./modules/extra",
        "module": {},
    }
    assert any("extra" in x for x in run(p, 1, stack))


def test_an_output_change_is_refused(stack: Path) -> None:
    p = plan(1)
    p["output_changes"] = {"bucket": {"actions": ["create"]}}
    assert "unexpected output change: create bucket" in run(p, 1, stack)


@pytest.mark.parametrize("name", ["override.tf", "x_override.tf", "extra.tf.json"])
def test_override_and_json_files_in_the_source_are_refused(stack: Path, name: str) -> None:
    (stack / name).write_text("{}\n" if name.endswith(".json") else "locals {}\n")
    assert any(name in x for x in run(plan(1), 1, stack))


def test_a_module_block_in_the_source_other_than_the_bucket_is_refused(stack: Path) -> None:
    (stack / "extra.tf").write_text('module "extra" {\n  source = "./x"\n}\n')
    assert any("extra" in x for x in run(plan(1), 1, stack))


# --- stage 2 ----------------------------------------------------------------------------------


def test_stage2_needs_the_approved_digest(stack: Path) -> None:
    assert any("approved digest" in x for x in run(plan(2), 2, stack, digest=OTHER_DIGEST))


@pytest.mark.parametrize("digest", [None, "latest", "sha256:" + "AB" * 32])
def test_stage2_refuses_a_missing_or_malformed_digest(stack: Path, digest: str | None) -> None:
    assert run(plan(2), 2, stack, digest=digest) != []


def test_stage2_needs_the_image_in_the_repository(stack: Path) -> None:
    p = plan(2)
    resources = p["prior_state"]["values"]["root_module"]["resources"]
    p["prior_state"]["values"]["root_module"]["resources"] = [
        r for r in resources if r["address"] != "data.aws_ecr_image.batch[0]"
    ]
    assert any("data.aws_ecr_image.batch[0]" in x for x in run(p, 2, stack))


@pytest.mark.parametrize(
    "address",
    ['aws_iam_role_policy.batch["task"]', "aws_ecr_repository_policy.batch", "aws_vpc.batch"],
)
def test_stage2_refuses_any_change_to_a_stage1_resource(stack: Path, address: str) -> None:
    p = plan(2)
    change(p, address)["change"]["actions"] = ["update"]
    assert f"unexpected change: update {address}" in run(p, 2, stack)


def test_stage2_refuses_a_missing_stage1_resource(stack: Path) -> None:
    p = plan(2)
    p["resource_changes"] = [
        r for r in p["resource_changes"] if r["address"] != "aws_iam_service_linked_role.ecs"
    ]
    assert any("aws_iam_service_linked_role.ecs" in x for x in run(p, 2, stack))


def test_stage2_refuses_an_image_by_tag_or_another_digest(stack: Path) -> None:
    for uri in (f"{REPO_URL}:latest", f"{REPO_URL}@{OTHER_DIGEST}"):
        p = plan(2)
        change(p, "aws_lambda_function.stage[0]")["change"]["after"]["image_uri"] = uri
        assert any(
            "aws_lambda_function.stage[0]" in x and "image_uri" in x for x in run(p, 2, stack)
        )


def test_stage2_refuses_a_task_definition_with_another_image(stack: Path) -> None:
    p = plan(2)
    after = change(p, "aws_ecs_task_definition.pipeline[0]")["change"]["after"]
    containers = json.loads(after["container_definitions"])
    containers[0]["image"] = f"{REPO_URL}@{OTHER_DIGEST}"
    after["container_definitions"] = json.dumps(containers)
    assert any("aws_ecs_task_definition.pipeline[0]" in x for x in run(p, 2, stack))


@pytest.mark.parametrize("env", [
    {"ECP_SOURCE": "eia", "ECP_STORE_URL": f"s3://{BUCKET}/store"},
    {"ECP_SOURCE": "synthetic", "ECP_STORE_URL": f"s3://{BUCKET}/store", "EIA_API_KEY": "x"},
])  # fmt: skip
def test_stage2_refuses_anything_but_synthetic_with_no_key(stack: Path, env: dict) -> None:
    p = plan(2)
    change(p, "aws_lambda_function.stage[0]")["change"]["after"]["environment"] = [
        {"variables": env}
    ]
    assert any("aws_lambda_function.stage[0]" in x and "environment" in x for x in run(p, 2, stack))


def test_stage2_refuses_reserved_concurrency(stack: Path) -> None:
    p = plan(2)
    change(p, "aws_lambda_function.stage[0]")["change"]["after"][
        "reserved_concurrent_executions"
    ] = 1
    assert any("reserved_concurrent_executions" in x for x in run(p, 2, stack))


def test_stage2_refuses_an_enabled_schedule(stack: Path) -> None:
    p = plan(2)
    change(p, "aws_scheduler_schedule.daily[0]")["change"]["after"]["state"] = "ENABLED"
    assert any("aws_scheduler_schedule.daily[0]" in x and "state" in x for x in run(p, 2, stack))


def test_stage2_refuses_an_alarm_without_the_topic(stack: Path) -> None:
    p = plan(2)
    change(p, "aws_cloudwatch_metric_alarm.failures[0]")["change"]["after"]["alarm_actions"] = []
    assert any("aws_cloudwatch_metric_alarm.failures[0]" in x for x in run(p, 2, stack))


def test_stage2_refuses_another_role_on_the_function(stack: Path) -> None:
    p = plan(2)
    change(p, "aws_lambda_function.stage[0]")["change"]["after"]["role"] = role_arn("task")
    assert any("aws_lambda_function.stage[0]" in x and "role" in x for x in run(p, 2, stack))


def test_a_stage1_plan_is_not_a_stage2_plan(stack: Path) -> None:
    assert run(plan(1), 2, stack) != []
    assert run(copy.deepcopy(plan(2)), 1, stack, digest=None) != []


# --- the command line -------------------------------------------------------------------------


def test_main_exits_nonzero_and_prints_problems(tmp_path: Path, stack: Path, capsys) -> None:
    p = plan(1)
    p["variables"]["region"]["value"] = "us-east-1"
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(p))
    assert bpc.main(["--stage", "1", "--stack-dir", str(stack), str(path)]) == 1
    assert "FAIL  region must be ca-central-1" in capsys.readouterr().out
    path.write_text(json.dumps(plan(2)))
    assert bpc.main(["--stage", "2", "--digest", DIGEST, "--stack-dir", str(stack),
                     str(path)]) == 0  # fmt: skip
