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
