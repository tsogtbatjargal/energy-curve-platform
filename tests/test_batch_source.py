"""M4c (ADR-0020, ADR-0022): the batch stack runs synthetic data only and holds no secret.

The stack's source is checked directly, so a secret, a key input or a real value cannot enter it
unnoticed: no secret store, no key or secret variable, no sensitive variable, both steps pinned to
`ECP_SOURCE=synthetic` with only the store URL besides, and no account ID or email address but
placeholders. The address the alarm emails is set only in the git-ignored terraform.tfvars.
"""

import re
import subprocess
from pathlib import Path

import hcl2
import pytest
from hcl2.utils import SerializationOptions

ROOT = Path(__file__).parents[1]
STACK = ROOT / "infra" / "batch"
PLACEHOLDER_ACCOUNTS = {"111111111111", "333333333333", "444444444444"}
SECRET_TYPES = re.compile(r"^aws_(secretsmanager_|ssm_parameter|kms_)")
SECRET_NAMES = re.compile(r"key|secret|token|password|credential|eia", re.IGNORECASE)
ENV_NAMES = {"ECP_SOURCE", "ECP_STORE_URL"}


def stack_files() -> list[Path]:
    return sorted(p for p in STACK.rglob("*") if p.is_file() and ".terraform" not in p.parts)


def parsed() -> dict[str, list]:
    blocks: dict[str, list] = {}
    for tf in sorted(STACK.glob("*.tf")):
        with tf.open() as fh:
            doc = hcl2.load(fh, serialization_options=SerializationOptions(with_comments=False))
        for kind, items in doc.items():
            blocks.setdefault(kind, []).extend(items)
    return blocks


def declared(kind: str) -> dict[str, dict]:
    """`resource`/`data` -> {"<type>.<name>": body}; `variable` -> {"<name>": body}."""
    out: dict[str, dict] = {}
    for item in parsed().get(kind, []):
        for first, rest in item.items():
            if kind == "variable":
                out[first.strip('"')] = rest
            else:
                for name, body in rest.items():
                    out[f"{first.strip(chr(34))}.{name.strip(chr(34))}"] = body
    return out


def test_no_secret_store_or_key_resource() -> None:
    found = [a for a in [*declared("resource"), *declared("data")] if SECRET_TYPES.match(a)]
    assert found == []


def test_no_key_or_secret_variable_and_none_sensitive() -> None:
    variables = declared("variable")
    assert [v for v in variables if SECRET_NAMES.search(v)] == []
    assert [v for v, body in variables.items() if body.get("sensitive")] == []


def test_the_eia_key_is_named_nowhere_in_the_stack() -> None:
    for p in stack_files():
        text = p.read_text()
        assert "EIA_API_KEY" not in text and "api_key" not in text.lower(), p


def test_both_steps_run_synthetic_data_with_only_the_store_url() -> None:
    fn = declared("resource")["aws_lambda_function.stage"]
    variables = fn["environment"][0]["variables"]
    assert set(variables) == ENV_NAMES
    assert variables["ECP_SOURCE"].strip('"') == "synthetic"
    task = (STACK / "ecs.tf").read_text()
    env = re.findall(r'\{ name = "([A-Z_]+)", value = ([^}]+) \}', task)
    assert {n for n, _ in env} == ENV_NAMES
    assert dict(env)["ECP_SOURCE"].strip() == '"synthetic"'
    assert "secrets" not in task


def test_no_account_id_but_placeholders() -> None:
    for p in stack_files():
        # A whole token: hex hashes (the provider lock file) contain 12-digit runs.
        found = set(re.findall(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])", p.read_text()))
        assert found <= PLACEHOLDER_ACCOUNTS, p


def test_no_email_address_but_placeholders() -> None:
    for p in stack_files():
        for address in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", p.read_text()):
            assert address.endswith("@example.invalid"), p


def test_the_alert_address_has_no_default() -> None:
    assert "default" not in declared("variable")["alert_email"]


@pytest.mark.parametrize("name", ["terraform.tfvars", "stage2.auto.tfvars", "backend.hcl"])
def test_private_files_are_git_ignored(name: str) -> None:
    argv = ["git", "check-ignore", "-q", f"infra/batch/{name}"]  # noqa: S607 - fixed argv
    result = subprocess.run(argv, cwd=ROOT, check=False)  # noqa: S603
    assert result.returncode == 0


# --- the stage split (ADR-0022) ---------------------------------------------------------------

STAGE2 = {
    "aws_lambda_function.stage", "aws_ecs_task_definition.pipeline",
    "aws_sfn_state_machine.batch", "aws_scheduler_schedule.daily",
    "aws_cloudwatch_metric_alarm.failures", "aws_ecr_image.batch",
}  # fmt: skip


def test_exactly_the_stage2_blocks_depend_on_the_digest() -> None:
    blocks = {**declared("resource"), **declared("data")}
    gated = {a for a, body in blocks.items() if "local.stage2" in str(body.get("count", ""))}
    assert gated == STAGE2
    assert all(body.get("count") is None for a, body in blocks.items() if a not in STAGE2)


def test_the_cluster_waits_for_the_explicit_service_linked_role() -> None:
    cluster = declared("resource")["aws_ecs_cluster.batch"]
    assert "aws_iam_service_linked_role.ecs" in str(cluster.get("depends_on"))
