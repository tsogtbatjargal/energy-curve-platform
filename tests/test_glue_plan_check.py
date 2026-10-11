"""M5b (ADR-0023): the Glue stack's plan must be exactly the reviewed shape before its apply.

The plan is synthetic but shaped like a real `terraform show -json` (Terraform 1.15.8) of
infra/glue from an empty state, read read-only on 2026-10-11 (14 creates since the Glue 5 logging
fix: two log groups). Account IDs are placeholders. The object hashes are recomputed from the
repository, as the check does.
"""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import build_glue_bundle
import glue_plan_check as gpc
import policy_templates
import pytest

ROOT = Path(__file__).parents[1]
MEMBER, MANAGEMENT, REGION = "333333333333", "111111111111", "ca-central-1"
BUCKET = f"ecp-glue-{MEMBER}-{REGION}"
BOUNDARY = f"arn:aws:iam::{MEMBER}:policy/ecp-workload-boundary"
POLICIES = ROOT / "infra" / "glue" / "policies"
VALUES = {"account_id": MEMBER, "region": REGION}
_B = "module.bucket.aws_s3_bucket"
JOB, ROLE, ROLE_POLICY = "aws_glue_job.shape", "aws_iam_role.glue", "aws_iam_role_policy.glue"
LOG_OUT, LOG_ERR = "aws_cloudwatch_log_group.output", "aws_cloudwatch_log_group.error"
LOG_OLD = "aws_cloudwatch_log_group.glue"  # the group of the first apply, deleted by the Glue 5 fix
SCRIPT, BUNDLE = "aws_s3_object.script", "aws_s3_object.bundle"
ARGUMENTS = {
    "--job-language": "python",
    "--extra-py-files": f"s3://{BUCKET}/glue/energy_curves_m5.zip",
    "--custom-logGroup-prefix": "/aws-glue/ecp-shape",
    "--start": "1983-01-03",
    "--end": "2024-04-05",
    "--window-start": "2014-01-01",
    "--window-end": "2024-04-05",
    "--partitions": "8",
    "--output": f"s3://{BUCKET}/m5/shape",
}
ADDRESSES = [
    JOB, ROLE, ROLE_POLICY, LOG_OUT, LOG_ERR, SCRIPT, BUNDLE,
    f"{_B}.this[0]", f"{_B}_lifecycle_configuration.this[0]", f"{_B}_ownership_controls.this[0]",
    f"{_B}_policy.this[0]", f"{_B}_public_access_block.this[0]",
    f"{_B}_server_side_encryption_configuration.this[0]", f"{_B}_versioning.this[0]",
]  # fmt: skip


def template(name: str) -> str:
    return json.dumps(policy_templates.render(name, VALUES, POLICIES))


def md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def after_values() -> dict[str, dict[str, Any]]:
    return {
        f"{_B}.this[0]": {"bucket": BUCKET, "force_destroy": False},
        f"{_B}_public_access_block.this[0]": {
            "block_public_acls": True, "block_public_policy": True,
            "ignore_public_acls": True, "restrict_public_buckets": True,
        },  # fmt: skip
        f"{_B}_ownership_controls.this[0]": {"rule": [{"object_ownership": "BucketOwnerEnforced"}]},
        f"{_B}_server_side_encryption_configuration.this[0]": {
            "rule": [{"apply_server_side_encryption_by_default": [{"sse_algorithm": "AES256"}]}]
        },
        f"{_B}_versioning.this[0]": {"versioning_configuration": [{"status": "Enabled"}]},
        f"{_B}_lifecycle_configuration.this[0]": {
            "rule": [
                {"id": "expire-committer-temp", "status": "Enabled",
                 "filter": [{"prefix": "m5/tmp/"}], "expiration": [{"days": 7}],
                 "abort_incomplete_multipart_upload": [], "noncurrent_version_expiration": []},
                {"id": "expire-old-versions", "status": "Enabled", "filter": [{"prefix": ""}],
                 "expiration": [{"days": 0, "expired_object_delete_marker": True}],
                 "abort_incomplete_multipart_upload": [{"days_after_initiation": 1}],
                 "noncurrent_version_expiration": [{"noncurrent_days": 30}]},
            ]
        },  # fmt: skip
        f"{_B}_policy.this[0]": {},
        SCRIPT: {"key": "glue/shape_job.py", "source": "./../../jobs/glue/shape_job.py",
                 "etag": md5((ROOT / "jobs" / "glue" / "shape_job.py").read_bytes())},
        BUNDLE: {"key": "glue/energy_curves_m5.zip", "source": "./build/energy_curves_m5.zip",
                 "etag": md5(build_glue_bundle.build_bytes())},
        ROLE: {"name": "ecp-glue-shape", "permissions_boundary": BOUNDARY,
               "assume_role_policy": template("glue-trust")},
        ROLE_POLICY: {"name": "ecp-glue-shape", "policy": template("glue-policy")},
        LOG_OUT: {"name": "/aws-glue/ecp-shape/output", "retention_in_days": 14},
        LOG_ERR: {"name": "/aws-glue/ecp-shape/error", "retention_in_days": 14},
        JOB: {
            "name": "ecp-glue-shape", "glue_version": "5.1", "worker_type": "G.1X",
            "number_of_workers": 2, "timeout": 10, "max_retries": 0, "execution_class": "STANDARD",
            "command": [{"name": "glueetl", "python_version": "3",
                         "script_location": f"s3://{BUCKET}/glue/shape_job.py"}],
            "execution_property": [{"max_concurrent_runs": 1}],
            "default_arguments": dict(ARGUMENTS), "connections": None,
            "security_configuration": None, "non_overridable_arguments": None,
            "notification_property": None, "maintenance_window": None,
        },
    }  # fmt: skip


def rc(address: str, after: dict[str, Any], actions: list[str] | None = None) -> dict[str, Any]:
    type_ = address.removeprefix("module.bucket.").split(".")[0]
    return {
        "address": address, "mode": "managed", "type": type_,
        "change": {"actions": actions or ["create"], "before": None, "after": after,
                   "after_unknown": {}},
    }  # fmt: skip


def configuration() -> dict[str, Any]:
    call = {"source": "terraform-aws-modules/s3-bucket/aws", "version_constraint": "5.16.2"}
    return {"root_module": {"resources": [], "module_calls": {"bucket": {**call, "module": {}}}}}


def plan() -> dict[str, Any]:
    values = after_values()
    return {
        "format_version": "1.2", "terraform_version": "1.15.8", "errored": False,
        "complete": True,
        "variables": {"region": {"value": REGION}, "expected_account_id": {"value": MEMBER},
                      "owner": {"value": "o"}},
        "prior_state": {"values": {"root_module": {"resources": [
            {"address": "data.aws_caller_identity.current", "mode": "data",
             "values": {"account_id": MEMBER}},
            {"address": "data.aws_organizations_organization.this", "mode": "data",
             "values": {"master_account_id": MANAGEMENT}},
        ]}}},
        "resource_changes": [rc(a, values[a]) for a in ADDRESSES],
        "output_changes": {},
        "configuration": configuration(),
    }  # fmt: skip


def change(p: dict[str, Any], address: str) -> dict[str, Any]:
    return next(r for r in p["resource_changes"] if r["address"] == address)


@pytest.fixture
def stack(tmp_path: Path) -> Path:
    """A copy of the Glue stack's .tf files (never a tfvars file or a build directory)."""
    copy_dir = tmp_path / "glue"
    copy_dir.mkdir()
    for tf in gpc.STACK.glob("*.tf"):
        (copy_dir / tf.name).write_text(tf.read_text())
    return copy_dir


def run(p: dict[str, Any], stack: Path) -> list[str]:
    problems, _ = gpc.check(p, stack)
    return problems


# --- the reviewed shape passes -----------------------------------------------------------------


def test_the_reviewed_plan_passes(stack: Path) -> None:
    problems, report = gpc.check(plan(), stack)
    assert problems == []
    assert "summary: 14 to add, 0 to change, 0 to destroy" in report


def test_a_plan_with_every_variable_recorded_as_a_string_passes(stack: Path) -> None:
    p = plan()
    assert all(isinstance(v["value"], str) for v in p["variables"].values())
    assert run(p, stack) == []


def test_the_report_never_prints_the_account_or_a_hash_value(stack: Path) -> None:
    _, report = gpc.check(plan(), stack)
    text = "\n".join(report)
    assert MEMBER not in text and md5(build_glue_bundle.build_bytes()) not in text


# --- the resources: exactly these, only creates ------------------------------------------------


@pytest.mark.parametrize("address", ADDRESSES)
def test_a_missing_create_is_refused(stack: Path, address: str) -> None:
    p = plan()
    p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != address]
    assert any(f"missing change: create {address}" in x for x in run(p, stack))


@pytest.mark.parametrize(
    "type_",
    [
        "aws_glue_crawler", "aws_glue_catalog_database", "aws_glue_catalog_table",
        "aws_glue_trigger",
        "aws_glue_workflow", "aws_glue_connection", "aws_glue_dev_endpoint",
        "aws_glue_security_configuration", "aws_kms_key", "aws_nat_gateway", "aws_eip", "aws_vpc",
        "aws_internet_gateway", "aws_secretsmanager_secret", "aws_ssm_parameter",
        "aws_cloudwatch_metric_alarm", "aws_sns_topic", "aws_iam_user",
        "aws_iam_role_policy_attachment",
        "aws_redshiftserverless_namespace", "aws_lambda_function",
    ],
)  # fmt: skip
def test_any_other_resource_is_refused(stack: Path, type_: str) -> None:
    p = plan()
    p["resource_changes"].append(rc(f"{type_}.x", {}))
    assert any(f"unexpected change: create {type_}.x" in x for x in run(p, stack))


@pytest.mark.parametrize("actions", [["update"], ["delete"], ["delete", "create"], ["no-op"]])
def test_only_creates_are_accepted(stack: Path, actions: list[str]) -> None:
    p = plan()
    change(p, JOB)["change"]["actions"] = actions
    assert run(p, stack) != []


def test_an_import_is_refused(stack: Path) -> None:
    p = plan()
    change(p, ROLE)["change"]["importing"] = {"id": "ecp-glue-shape"}
    assert run(p, stack) != []


def test_a_state_that_is_not_empty_is_refused(stack: Path) -> None:
    p = plan()
    p["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_s3_bucket.old", "mode": "managed", "values": {}}
    )
    assert any("state is not empty" in x for x in run(p, stack))


# --- the job -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("glue_version",), "6.0"), (("glue_version",), "4.0"), (("worker_type",), "G.2X"),
        (("number_of_workers",), 10), (("timeout",), 2880), (("max_retries",), 1),
        (("execution_class",), "FLEX"), (("name",), "other"),
        (("command", 0, "name"), "pythonshell"), (("command", 0, "python_version"), "2"),
        (("command", 0, "script_location"), "s3://elsewhere/shape_job.py"),
        (("execution_property", 0, "max_concurrent_runs"), 5),
        (("connections",), ["conn"]), (("security_configuration",), "sec"),
        (("non_overridable_arguments",), {"--x": "y"}),
    ],
)  # fmt: skip
def test_a_job_setting_that_differs_is_refused(stack: Path, path: tuple, value: Any) -> None:
    p = plan()
    node = change(p, JOB)["change"]["after"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert any(JOB in x for x in run(p, stack))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update({"--enable-glue-datacatalog": ""}),
        lambda a: a.update({"--TempDir": "s3://x/tmp"}),
        lambda a: a.update({"--additional-python-modules": "requests"}),
        lambda a: a.update({"--extra-py-files": "s3://elsewhere/evil.zip"}),
        lambda a: a.update({"--output": "s3://elsewhere/m5/shape"}),
        lambda a: a.update({"--partitions": "800"}),
        lambda a: a.update({"--start": "2000-01-01"}),
        lambda a: a.update({"--window-end": "2030-01-01"}),
        lambda a: a.pop("--start"),
        lambda a: a.pop("--window-start"),
        lambda a: a.update({"--conf": "spark.hadoop.fs.s3a.access.key=x"}),
        lambda a: a.pop("--output"),
        lambda a: a.update({"--enable-metrics": ""}),
        lambda a: a.update({"--enable-continuous-cloudwatch-log": "true"}),
        lambda a: a.update({"--continuous-log-logGroup": "/aws-glue/ecp-shape"}),
        lambda a: a.update({"--enable-continuous-log-filter": "true"}),
        lambda a: a.pop("--custom-logGroup-prefix"),
        lambda a: a.update({"--custom-logGroup-prefix": "/aws-glue/jobs"}),
        lambda a: a.update({"--custom-logStream-prefix": "x"}),
    ],
)
def test_the_arguments_must_be_exactly_the_reviewed_ones(stack: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    p = plan()
    mutate(change(p, JOB)["change"]["after"]["default_arguments"])
    assert any("default_arguments" in x for x in run(p, stack))


# --- what Glue runs is the repository's source -------------------------------------------------


@pytest.mark.parametrize("address", [SCRIPT, BUNDLE])
def test_an_object_whose_hash_is_not_the_repositorys_is_refused(stack: Path, address: str) -> None:
    p = plan()
    change(p, address)["change"]["after"]["etag"] = md5(b"something else")
    assert any(address in x and "repository" in x for x in run(p, stack))


@pytest.mark.parametrize("address", [SCRIPT, BUNDLE])
def test_an_object_with_an_unknown_hash_is_refused(stack: Path, address: str) -> None:
    p = plan()
    after = change(p, address)["change"]["after"]
    after.pop("etag")
    change(p, address)["change"]["after_unknown"] = {"etag": True}
    assert any(address in x for x in run(p, stack))


@pytest.mark.parametrize(
    ("address", "key", "value"),
    [(SCRIPT, "key", "glue/other.py"), (SCRIPT, "source", "/elsewhere/evil.py"),
     (BUNDLE, "key", "glue/other.zip"), (BUNDLE, "source", "./build/other.zip")],
)  # fmt: skip
def test_an_object_key_or_source_that_differs_is_refused(
    stack: Path, address: str, key: str, value: str
) -> None:
    p = plan()
    change(p, address)["change"]["after"][key] = value
    assert any(address in x for x in run(p, stack))


# --- the role: the boundary and the reviewed templates -----------------------------------------


def test_a_role_without_the_boundary_is_refused(stack: Path) -> None:
    p = plan()
    change(p, ROLE)["change"]["after"]["permissions_boundary"] = None
    assert any("permissions_boundary" in x for x in run(p, stack))


def test_a_role_policy_other_than_the_template_is_refused(stack: Path) -> None:
    p = plan()
    doc = json.loads(change(p, ROLE_POLICY)["change"]["after"]["policy"])
    doc["Statement"].append({"Effect": "Allow", "Action": "s3:*", "Resource": "*"})
    change(p, ROLE_POLICY)["change"]["after"]["policy"] = json.dumps(doc)
    assert any(ROLE_POLICY in x and "template" in x for x in run(p, stack))


def test_a_trust_policy_other_than_the_template_is_refused(stack: Path) -> None:
    p = plan()
    doc = json.loads(change(p, ROLE)["change"]["after"]["assume_role_policy"])
    doc["Statement"][0]["Principal"] = {"AWS": "*"}
    change(p, ROLE)["change"]["after"]["assume_role_policy"] = json.dumps(doc)
    assert any(ROLE in x and "template" in x for x in run(p, stack))


@pytest.mark.parametrize("address", [ROLE, ROLE_POLICY])
def test_a_policy_unknown_at_plan_time_is_refused(stack: Path, address: str) -> None:
    p = plan()
    attribute = "assume_role_policy" if address == ROLE else "policy"
    change(p, address)["change"]["after_unknown"] = {attribute: True}
    del change(p, address)["change"]["after"][attribute]
    assert any(address in x and "known at plan time" in x for x in run(p, stack))


def test_a_role_policy_for_another_account_is_refused(stack: Path) -> None:
    p = plan()
    policy = change(p, ROLE_POLICY)["change"]["after"]["policy"].replace(MEMBER, "444444444444")
    change(p, ROLE_POLICY)["change"]["after"]["policy"] = policy
    assert any(ROLE_POLICY in x for x in run(p, stack))


# --- the bucket, the log group -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "path", "value"),
    [
        (f"{_B}.this[0]", ("bucket",), "ecp-data-333333333333-ca-central-1"),
        (f"{_B}.this[0]", ("force_destroy",), True),
        (f"{_B}_public_access_block.this[0]", ("block_public_acls",), False),
        (f"{_B}_public_access_block.this[0]", ("restrict_public_buckets",), False),
        (f"{_B}_ownership_controls.this[0]", ("rule", 0, "object_ownership"), "ObjectWriter"),
        (f"{_B}_server_side_encryption_configuration.this[0]",
         ("rule", 0, "apply_server_side_encryption_by_default", 0, "sse_algorithm"), "aws:kms"),
        (f"{_B}_versioning.this[0]", ("versioning_configuration", 0, "status"), "Suspended"),
        (f"{_B}_lifecycle_configuration.this[0]", ("rule", 0, "filter", 0, "prefix"), "m5/"),
        (f"{_B}_lifecycle_configuration.this[0]", ("rule", 0, "expiration", 0, "days"), 365),
        (f"{_B}_lifecycle_configuration.this[0]", ("rule", 1, "status"), "Disabled"),
        (f"{_B}_lifecycle_configuration.this[0]",
         ("rule", 1, "noncurrent_version_expiration", 0, "noncurrent_days"), 3650),
        (LOG_OUT, ("name",), "/aws-glue/ecp-shape"), (LOG_OUT, ("retention_in_days",), 0),
        (LOG_ERR, ("name",), "/aws-glue/other/error"), (LOG_ERR, ("retention_in_days",), 0),
    ],
)  # fmt: skip
def test_a_reviewed_setting_that_differs_is_refused(
    stack: Path, address: str, path: tuple, value: Any
) -> None:
    p = plan()
    node = change(p, address)["change"]["after"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert any(address in x for x in run(p, stack))


# --- the plan is sound, in the member account, in the region -----------------------------------


def test_the_wrong_account_is_refused(stack: Path) -> None:
    p = plan()
    p["variables"]["expected_account_id"]["value"] = "444444444444"
    assert any("expected_account_id" in x for x in run(p, stack))


def test_the_management_account_is_refused(stack: Path) -> None:
    p = plan()
    p["prior_state"]["values"]["root_module"]["resources"][1]["values"]["master_account_id"] = (
        MEMBER
    )
    assert any("management account" in x for x in run(p, stack))


def test_another_region_is_refused(stack: Path) -> None:
    p = plan()
    p["variables"]["region"]["value"] = "us-east-1"
    assert any("region" in x for x in run(p, stack))


@pytest.mark.parametrize("key", ["errored", "deferred_changes", "resource_drift"])
def test_an_unsound_plan_is_refused(stack: Path, key: str) -> None:
    p = plan()
    p[key] = True if key == "errored" else [{"address": "aws_glue_job.shape"}]
    assert run(p, stack) != []


def test_an_incomplete_plan_is_refused(stack: Path) -> None:
    p = plan()
    p["complete"] = False
    assert any("incomplete" in x for x in run(p, stack))


def test_an_output_change_is_refused(stack: Path) -> None:
    p = plan()
    p["output_changes"] = {"x": {"actions": ["create"]}}
    assert any("output" in x for x in run(p, stack))


def test_an_unexpected_variable_is_refused(stack: Path) -> None:
    p = plan()
    p["variables"]["image_digest"] = {"value": "sha256:" + "ab" * 32}
    assert any("variable" in x for x in run(p, stack))


# --- the source: one pinned module, no override, no JSON ---------------------------------------


@pytest.mark.parametrize(
    ("source", "version"),
    [("terraform-aws-modules/s3-bucket/aws", "5.16.1"), ("evil/s3-bucket/aws", "5.16.2")],
)
def test_only_the_pinned_bucket_module_is_allowed(stack: Path, source: str, version: str) -> None:
    p = plan()
    call = p["configuration"]["root_module"]["module_calls"]["bucket"]
    call["source"], call["version_constraint"] = source, version
    assert any("module bucket must be" in x for x in run(p, stack))


def test_another_module_call_is_refused(stack: Path) -> None:
    p = plan()
    p["configuration"]["root_module"]["module_calls"]["extra"] = {
        "source": "x/y/z", "version_constraint": "1.0.0", "module": {},
    }  # fmt: skip
    assert any("module call not allowed: extra" in x for x in run(p, stack))


@pytest.mark.parametrize("name", ["override.tf", "storage_override.tf", "x.tf.json"])
def test_override_and_json_files_in_the_source_are_refused(stack: Path, name: str) -> None:
    (stack / name).write_text("{}" if name.endswith(".json") else "")
    assert any(name in x for x in run(plan(), stack))


def test_a_module_block_in_the_source_other_than_the_bucket_is_refused(stack: Path) -> None:
    (stack / "extra.tf").write_text('module "x" {\n  source = "./local"\n}\n')
    assert any("module block not allowed: x" in x for x in run(plan(), stack))


# --- the command line --------------------------------------------------------------------------


def test_main_exits_zero_for_the_reviewed_plan_and_nonzero_otherwise(
    tmp_path: Path, stack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan()))
    assert gpc.main(["--stack-dir", str(stack), str(path)]) == 0
    assert "OK    the reviewed shape" in capsys.readouterr().out
    bad = copy.deepcopy(plan())
    bad["variables"]["region"]["value"] = "us-east-1"
    path.write_text(json.dumps(bad))
    assert gpc.main(["--stack-dir", str(stack), str(path)]) == 1
    assert "FAIL  region must be ca-central-1" in capsys.readouterr().out


# --- the update: Glue 5 logging and no metrics (M5b second fix) --------------------------------
# On the applied stack with no drift: the job's default_arguments update in place (the four
# Glue-4 logging/metrics arguments go, --custom-logGroup-prefix arrives), two log groups are
# created and the empty /aws-glue/ecp-shape group is deleted. 1 update, 2 creates, 1 delete,
# 10 no-ops; nothing else.

REMOVED_ARGUMENTS = {
    "--enable-metrics": "",
    "--enable-continuous-cloudwatch-log": "true",
    "--continuous-log-logGroup": "/aws-glue/ecp-shape",
    "--enable-continuous-log-filter": "true",
}
APPLIED = [JOB, ROLE, ROLE_POLICY, LOG_OLD, SCRIPT, BUNDLE]
CREATED = [LOG_OUT, LOG_ERR]
UNCHANGED = [a for a in ADDRESSES if a not in (JOB, *CREATED)]


def update_plan() -> dict[str, Any]:
    p = plan()
    p["prior_state"]["values"]["root_module"]["resources"] += [
        {"address": a, "mode": "managed", "values": {}} for a in APPLIED
    ]
    for r in p["resource_changes"]:
        c = r["change"]
        if r["address"] in CREATED:
            continue  # a create: nothing before
        c["before"] = copy.deepcopy(c["after"])
        c["actions"] = ["no-op"]
    job = change(p, JOB)["change"]
    job["actions"] = ["update"]
    for side in ("before", "after"):  # the provider's read-back, recorded by the refresh-only apply
        job[side].update(connections=[], security_configuration="", non_overridable_arguments={})
    old = {k: v for k, v in ARGUMENTS.items() if k != "--custom-logGroup-prefix"}
    job["before"]["default_arguments"] = {**old, **REMOVED_ARGUMENTS}
    p["resource_changes"].append(
        {
            "address": LOG_OLD, "mode": "managed", "type": "aws_cloudwatch_log_group",
            "action_reason": "delete_because_no_resource_config",
            "change": {
                "actions": ["delete"], "after": None, "after_unknown": {},
                "before": {"name": "/aws-glue/ecp-shape", "retention_in_days": 14},
            },
        }
    )  # fmt: skip
    return p


def run_update(p: dict[str, Any], stack: Path) -> list[str]:
    problems, _ = gpc.check(p, stack, "update")
    return problems


def test_the_reviewed_update_plan_passes(stack: Path) -> None:
    problems, report = gpc.check(update_plan(), stack, "update")
    assert problems == []
    assert "summary: 2 to add, 1 to change, 1 to destroy" in report
    assert sorted(x for x in report if not x.startswith(("no-op", "summary"))) == sorted(
        [f"create   {LOG_OUT}", f"create   {LOG_ERR}", f"update   {JOB}", f"delete   {LOG_OLD}"]
    )


def test_the_update_plan_is_refused_in_create_mode_and_the_create_plan_in_update_mode(
    stack: Path,
) -> None:
    assert run(update_plan(), stack) != []
    assert run_update(plan(), stack) != []


def test_update_mode_is_chosen_on_the_command_line(
    tmp_path: Path, stack: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(update_plan()))
    assert gpc.main(["--stack-dir", str(stack), str(path)]) == 1  # create is the default
    capsys.readouterr()
    assert gpc.main(["--change", "update", "--stack-dir", str(stack), str(path)]) == 0
    assert "OK    the reviewed shape" in capsys.readouterr().out


def test_an_unknown_change_mode_is_refused(stack: Path) -> None:
    problems, _ = gpc.check(update_plan(), stack, "replace")
    assert any("unknown change mode" in x for x in problems)


def test_update_mode_refuses_drift(stack: Path) -> None:
    p = update_plan()
    p["resource_drift"] = [{"address": LOG_OLD}]
    assert any("resource_drift" in x for x in run_update(p, stack))


def test_update_mode_needs_the_applied_state(stack: Path) -> None:
    p = update_plan()
    p["prior_state"]["values"]["root_module"]["resources"] = p["prior_state"]["values"][
        "root_module"
    ]["resources"][:2]  # only the two data sources
    assert any("state" in x for x in run_update(p, stack))
    p = update_plan()
    p["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": "aws_s3_bucket.old", "mode": "managed", "values": {}}
    )
    assert any("state" in x for x in run_update(p, stack))
    p = update_plan()  # the new groups are not in the state yet
    p["prior_state"]["values"]["root_module"]["resources"].append(
        {"address": LOG_OUT, "mode": "managed", "values": {}}
    )
    assert any("state" in x for x in run_update(p, stack))


def test_a_missing_change_is_refused(stack: Path) -> None:
    for address, verb in [(JOB, "update"), (LOG_OUT, "create"), (LOG_ERR, "create"),
                          (LOG_OLD, "delete")]:  # fmt: skip
        p = update_plan()
        p["resource_changes"] = [r for r in p["resource_changes"] if r["address"] != address]
        assert any(f"missing change: {verb} {address}" in x for x in run_update(p, stack))


@pytest.mark.parametrize("address", UNCHANGED)
@pytest.mark.parametrize("actions", [["update"], ["delete", "create"], ["delete"], ["create"]])
def test_any_other_resource_must_stay_unchanged(
    stack: Path, address: str, actions: list[str]
) -> None:
    p = update_plan()
    change(p, address)["change"]["actions"] = actions
    assert any(
        f"unexpected change: {'+'.join(actions)} {address}" in x for x in run_update(p, stack)
    )


@pytest.mark.parametrize(
    "actions", [["delete", "create"], ["create", "delete"], ["delete"], ["create"], ["no-op"]]
)
def test_the_job_must_be_updated_in_place(stack: Path, actions: list[str]) -> None:
    p = update_plan()
    change(p, JOB)["change"]["actions"] = actions
    assert run_update(p, stack) != []


@pytest.mark.parametrize("address", CREATED)
@pytest.mark.parametrize("actions", [["no-op"], ["update"], ["delete"], ["delete", "create"]])
def test_the_new_log_groups_must_be_plain_creates(
    stack: Path, address: str, actions: list[str]
) -> None:
    p = update_plan()
    change(p, address)["change"]["actions"] = actions
    assert run_update(p, stack) != []


@pytest.mark.parametrize("actions", [["no-op"], ["update"], ["create"], ["delete", "create"]])
def test_the_old_log_group_must_be_deleted(stack: Path, actions: list[str]) -> None:
    p = update_plan()
    change(p, LOG_OLD)["change"]["actions"] = actions
    assert run_update(p, stack) != []


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("name", "/aws-glue/jobs/output"), ("retention_in_days", 0)],
)
def test_only_the_empty_first_log_group_may_be_deleted(
    stack: Path, attribute: str, value: Any
) -> None:
    p = update_plan()
    change(p, LOG_OLD)["change"]["before"][attribute] = value
    assert any(LOG_OLD in x for x in run_update(p, stack))


def test_another_deleted_resource_is_refused(stack: Path) -> None:
    p = update_plan()
    p["resource_changes"].append(
        {"address": "aws_cloudwatch_log_group.jobs", "mode": "managed",
         "type": "aws_cloudwatch_log_group",
         "change": {"actions": ["delete"], "before": {"name": "/aws-glue/jobs/error"},
                    "after": None, "after_unknown": {}}}
    )  # fmt: skip
    assert any("unexpected change: delete" in x for x in run_update(p, stack))


def test_a_new_resource_is_refused(stack: Path) -> None:
    p = update_plan()
    p["resource_changes"].append(rc("aws_glue_crawler.x", {}))
    assert any("unexpected change: create aws_glue_crawler.x" in x for x in run_update(p, stack))


@pytest.mark.parametrize(
    ("address", "path", "value"),
    [
        (LOG_OUT, ("name",), "/aws-glue/ecp-shape"),
        (LOG_OUT, ("name",), "/aws-glue/jobs/output"),
        (LOG_OUT, ("retention_in_days",), 0),
        (LOG_ERR, ("name",), "/aws-glue/other/error"),
        (LOG_ERR, ("retention_in_days",), 3650),
    ],
)
def test_the_created_log_groups_are_the_reviewed_ones(
    stack: Path, address: str, path: tuple, value: Any
) -> None:
    p = update_plan()
    change(p, address)["change"]["after"][path[0]] = value
    assert any(address in x for x in run_update(p, stack))


# the job: only default_arguments, from the Glue-4 set to exactly the reviewed one


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("timeout", 2880), ("worker_type", "G.2X"), ("number_of_workers", 10), ("max_retries", 3),
     ("glue_version", "6.0"), ("execution_class", "FLEX"), ("connections", ["c"]),
     ("security_configuration", "s"), ("name", "other"), ("role_arn", "arn:aws:iam::1:role/x"),
     ("command", [{"name": "glueetl", "python_version": "3", "script_location": "s3://x/y.py"}])],
)  # fmt: skip
def test_the_job_may_change_nothing_but_its_arguments(
    stack: Path, attribute: str, value: Any
) -> None:
    p = update_plan()
    change(p, JOB)["change"]["before"][attribute] = value
    assert any(JOB in x for x in run_update(p, stack))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update({"--start": "2000-01-01"}),
        lambda a: a.update({"--output": "s3://elsewhere/m5/shape"}),
        lambda a: a.update({"--extra-py-files": "s3://elsewhere/evil.zip"}),
        lambda a: a.update({"--TempDir": "s3://x/tmp"}),
        lambda a: a.update({"--enable-glue-datacatalog": ""}),
        lambda a: a.pop("--window-end"),
        lambda a: a.pop("--partitions"),
        lambda a: a.pop("--custom-logGroup-prefix"),
        lambda a: a.update({"--custom-logGroup-prefix": "/aws-glue/jobs"}),
        lambda a: a.update({"--custom-logStream-prefix": "x"}),
        lambda a: a.update({"--enable-metrics": ""}),
        lambda a: a.update({"--enable-continuous-cloudwatch-log": "true"}),
        lambda a: a.update({"--continuous-log-logGroup": "/aws-glue/ecp-shape"}),
        lambda a: a.update({"--enable-continuous-log-filter": "true"}),
    ],
)
def test_the_arguments_after_must_be_exactly_the_reviewed_ones(stack: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    p = update_plan()
    mutate(change(p, JOB)["change"]["after"]["default_arguments"])
    assert any("default_arguments" in x for x in run_update(p, stack))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update({"--output": "s3://elsewhere/m5/shape"}),
        lambda a: a.update({"--partitions": "800"}),
        lambda a: a.pop("--extra-py-files"),
        lambda a: a.update({"--conf": "x"}),
        lambda a: a.pop("--enable-metrics"),
        lambda a: a.update({"--enable-metrics": "true"}),
        lambda a: a.update({"--continuous-log-logGroup": "/aws-glue/other"}),
        lambda a: a.update({"--custom-logGroup-prefix": "/aws-glue/ecp-shape"}),
    ],
)
def test_the_arguments_before_must_be_the_applied_ones(stack: Path, mutate) -> None:  # type: ignore[no-untyped-def]
    p = update_plan()
    mutate(change(p, JOB)["change"]["before"]["default_arguments"])
    assert any(JOB in x for x in run_update(p, stack))


def test_the_job_may_have_nothing_unknown_after_apply(stack: Path) -> None:
    p = update_plan()
    change(p, JOB)["change"]["after_unknown"] = {"arn": True}
    assert any(JOB in x for x in run_update(p, stack))


# the script and the bundle, IAM: unchanged, and still the reviewed ones


def test_the_script_may_not_change_in_this_update(stack: Path) -> None:
    p = update_plan()
    c = change(p, SCRIPT)["change"]
    c["actions"] = ["update"]
    c["before"]["etag"] = md5(b"an older script")
    assert any(SCRIPT in x for x in run_update(p, stack))


def test_a_bundle_that_is_not_the_repositorys_is_refused_even_unchanged(stack: Path) -> None:
    p = update_plan()
    for side in ("before", "after"):
        change(p, BUNDLE)["change"][side]["etag"] = md5(b"another bundle")
    assert any(BUNDLE in x and "repository" in x for x in run_update(p, stack))


def test_a_script_that_is_not_the_repositorys_is_refused_even_unchanged(stack: Path) -> None:
    p = update_plan()
    for side in ("before", "after"):
        change(p, SCRIPT)["change"][side]["etag"] = md5(b"another script")
    assert any(SCRIPT in x and "repository" in x for x in run_update(p, stack))


def test_a_role_policy_that_is_not_the_template_is_refused_even_unchanged(stack: Path) -> None:
    p = update_plan()
    for side in ("before", "after"):
        doc = json.loads(change(p, ROLE_POLICY)["change"][side]["policy"])
        doc["Statement"].append({"Effect": "Allow", "Action": "s3:*", "Resource": "*"})
        change(p, ROLE_POLICY)["change"][side]["policy"] = json.dumps(doc)
    assert any(ROLE_POLICY in x and "template" in x for x in run_update(p, stack))


def test_update_mode_still_refuses_the_wrong_account_region_and_module(stack: Path) -> None:
    p = update_plan()
    p["variables"]["expected_account_id"]["value"] = "444444444444"
    p["variables"]["region"]["value"] = "us-east-1"
    p["configuration"]["root_module"]["module_calls"]["bucket"]["version_constraint"] = "5.16.1"
    problems = run_update(p, stack)
    assert any("expected_account_id" in x for x in problems)
    assert any("region" in x for x in problems)
    assert any("module bucket must be" in x for x in problems)


def test_the_update_report_never_prints_a_value(stack: Path) -> None:
    _, report = gpc.check(update_plan(), stack, "update")
    text = "\n".join(report)
    assert MEMBER not in text and "1983-01-03" not in text and md5(b"x") not in text


def test_the_job_read_back_empties_count_as_unset_in_both_modes(stack: Path) -> None:
    p = plan()
    after = change(p, JOB)["change"]["after"]
    after.update(connections=[], security_configuration="", non_overridable_arguments={})
    assert run(p, stack) == []


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("connections", ["c"]),
        ("security_configuration", "s"),
        ("non_overridable_arguments", {"a": "b"}),
    ],
)
def test_a_non_empty_value_is_still_refused_in_update_mode(
    stack: Path, attribute: str, value: Any
) -> None:
    p = update_plan()
    for side in ("before", "after"):
        change(p, JOB)["change"][side][attribute] = value
    assert any(JOB in x and attribute in x for x in run_update(p, stack))
