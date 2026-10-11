"""M5b (ADR-0023): the Glue stack's source stays inside its reviewed boundaries."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
STACK = ROOT / "infra" / "glue"


def tracked() -> list[str]:
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "infra/glue"],  # noqa: S607
        cwd=ROOT, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return sorted(out.stdout.split())


def test_the_stack_holds_exactly_the_reviewed_files() -> None:
    assert tracked() == [
        "infra/glue/.terraform.lock.hcl",
        "infra/glue/backend.hcl.example",
        "infra/glue/backend.tf",
        "infra/glue/guard.tf",
        "infra/glue/iam.tf",
        "infra/glue/job.tf",
        "infra/glue/locals.tf",
        "infra/glue/logs.tf",
        "infra/glue/objects.tf",
        "infra/glue/policies/glue-policy.json.tftpl",
        "infra/glue/policies/glue-trust.json.tftpl",
        "infra/glue/storage.tf",
        "infra/glue/tests/glue.tftest.hcl",
        "infra/glue/variables.tf",
        "infra/glue/versions.tf",
    ]


def test_no_tfvars_backend_config_state_or_build_output_is_tracked() -> None:
    for name in tracked():
        assert not name.endswith((".tfvars", ".tfstate", ".tfplan", ".zip", "backend.hcl")), name


def text() -> str:
    return "\n".join(p.read_text() for p in sorted(STACK.glob("*.tf")))


def test_the_stack_never_names_the_batch_store_or_the_state_bucket() -> None:
    body = text()
    assert "ecp-data-" not in body and "ecp-tfstate" not in body


def test_the_stack_has_no_admin_secret_key_network_or_alarm_resources() -> None:
    body = text()
    for forbidden in (
        "aws_kms_", "aws_secretsmanager_", "aws_ssm_parameter", "aws_vpc", "aws_subnet",
        "aws_nat_gateway", "aws_internet_gateway", "aws_cloudwatch_metric_alarm", "aws_sns_",
        "aws_glue_crawler", "aws_glue_catalog_", "aws_glue_trigger", "aws_glue_connection",
        "aws_glue_dev_endpoint", "aws_glue_security_configuration", "aws_redshift",
    ):  # fmt: skip
        assert forbidden not in body, forbidden


def test_the_job_asks_for_no_data_catalog_and_no_pypi_modules() -> None:
    body = (STACK / "job.tf").read_text()
    assert "--enable-glue-datacatalog" not in body
    assert "--additional-python-modules" not in body
    assert "--TempDir" not in body


# --- Glue 5 logging and metrics (ADR-0023, M5b second fix) ---------------------------------------
# The second run's error log showed cloudwatch:PutMetricData refused (--enable-metrics, a metrics
# sink the plain-SparkSession job gains little from), and /aws-glue/ecp-shape stayed empty: Glue 5
# ignores the continuous-logging arguments and writes to <--custom-logGroup-prefix>/output and
# /error. Sources: "Migrating to AWS Glue 5.0" and "Logging for AWS Glue jobs".

NO_LONGER_USED = (
    "--enable-metrics",
    "--enable-continuous-cloudwatch-log",
    "--continuous-log-logGroup",
    "--continuous-log-logStreamPrefix",
    "--continuous-log-conversionPattern",
    "--enable-continuous-log-filter",
)
PREFIX = "/aws-glue/ecp-shape"


def job_argument_names() -> set[str]:
    import re

    block = re.search(
        r"default_arguments\s*=\s*\{(.*?)\n  \}", (STACK / "job.tf").read_text(), re.S
    )
    assert block, "default_arguments not found in job.tf"
    return set(re.findall(r'^\s*"(--[^"]+)"\s*=', block.group(1), re.M))


def test_the_job_passes_none_of_the_metrics_or_glue_4_logging_arguments() -> None:
    assert job_argument_names().isdisjoint(NO_LONGER_USED)


def test_the_job_passes_the_glue_5_log_group_prefix() -> None:
    import re

    assert "--custom-logGroup-prefix" in job_argument_names()
    assert re.search(
        r'"--custom-logGroup-prefix"\s*=\s*local\.log_group_prefix', (STACK / "job.tf").read_text()
    )
    assert re.search(rf'log_group_prefix\s*=\s*"{PREFIX}"', (STACK / "locals.tf").read_text())


def test_the_prefix_names_two_managed_groups_with_14_day_retention() -> None:
    import hcl2

    with (STACK / "logs.tf").open() as fh:
        doc = hcl2.load(fh)
    groups = {
        name.strip('"'): body
        for block in doc["resource"]
        for kind, bodies in block.items()
        if kind.strip('"') == "aws_cloudwatch_log_group"
        for name, body in bodies.items()
    }
    assert set(groups) == {"output", "error"}
    assert all(g["retention_in_days"] == 14 for g in groups.values())
    assert {g["name"].strip('"') for g in groups.values()} == {
        "${local.log_group_prefix}/output",
        "${local.log_group_prefix}/error",
    }


def test_the_reviewed_arguments_in_the_plan_check_are_the_same_set() -> None:
    import glue_plan_check as gpc

    names = set(gpc.arguments("b"))
    assert names.isdisjoint(NO_LONGER_USED)
    assert gpc.arguments("b")["--custom-logGroup-prefix"] == PREFIX
    assert names == job_argument_names()
