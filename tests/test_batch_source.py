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


def stack_files(root: Path = ROOT) -> list[Path]:
    """The stack's files as git sees them: tracked, or new and not ignored. Git-ignored files
    (terraform.tfvars, backend.hcl, a per-instance TF_DATA_DIR) hold real values or binaries."""
    argv = [
        "git",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "infra/batch",
    ]
    out = subprocess.run(argv, cwd=root, check=True, capture_output=True, text=True).stdout  # noqa: S603, S607
    return sorted(p for p in (root / name for name in out.split("\0") if name) if p.is_file())


def text_of(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="replace")


def findings(files: list[Path], root: Path = ROOT) -> list[str]:
    """What the source scan objects to, as "<path>: <kind>". A matched value is never part of it."""
    out = []
    for p in files:
        text, where = text_of(p), p.relative_to(root)
        # A whole token: hex hashes (the provider lock file) contain 12-digit runs.
        ids = set(re.findall(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])", text))
        if not ids <= PLACEHOLDER_ACCOUNTS:
            out.append(f"{where}: account id other than a placeholder")
        if any(
            not a.endswith("@example.invalid")
            for a in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
        ):
            out.append(f"{where}: email address other than @example.invalid")
        if "EIA_API_KEY" in text or "api_key" in text.lower():
            out.append(f"{where}: EIA key name")
    return out


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
    assert [f for f in findings(stack_files()) if f.endswith("EIA key name")] == []


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
    assert [f for f in findings(stack_files()) if "account id" in f] == []


def test_no_email_address_but_placeholders() -> None:
    assert [f for f in findings(stack_files()) if "email address" in f] == []


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


# --- the file listing and the failure messages ---------------------------------------------------


def _git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


def test_the_scan_lists_only_files_git_does_not_ignore(tmp_path: Path) -> None:
    """A per-instance TF_DATA_DIR (`.terraform-foo/`) and git-ignored private files stay out."""
    stack = tmp_path / "infra" / "batch"
    (stack / ".terraform-foo").mkdir(parents=True)
    (stack / ".terraform-foo" / "provider").write_bytes(b"\x81\x00\xff binary")
    (stack / "main.tf").write_text("# tracked\n")
    (stack / "new.tf").write_text("# not yet added, not ignored\n")
    (stack / "private.tfvars").write_text('alert_email = "t@example.invalid"\n')
    (tmp_path / ".gitignore").write_text("*.tfvars\n.terraform-*/\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "infra/batch/main.tf")
    assert [p.name for p in stack_files(tmp_path)] == ["main.tf", "new.tf"]


def test_findings_name_the_file_and_the_kind_never_the_value(tmp_path: Path) -> None:
    stack = tmp_path / "infra" / "batch"
    stack.mkdir(parents=True)
    leaks = {
        "acct.tf": ("999988887777", "account id"),
        "mail.tf": ("someone@example.org", "email address"),
        "key.tf": ("EIA_API_KEY", "EIA key name"),
        "ok.tf": ("111111111111 t@example.invalid", None),
    }
    for name, (text, _) in leaks.items():
        (stack / name).write_text(f"# {text}\n")
    _git(tmp_path, "init", "-q")
    got = findings(stack_files(tmp_path), tmp_path)
    assert sorted(got) == [
        "infra/batch/acct.tf: account id other than a placeholder",
        "infra/batch/key.tf: EIA key name",
        "infra/batch/mail.tf: email address other than @example.invalid",
    ]
    assert not any(text in " ".join(got) for text, kind in leaks.values() if kind)


# --- what CI plans with (ADR-0022) ----------------------------------------------------------------


def test_the_subscription_ignores_endpoint_changes() -> None:
    sub = declared("resource")["aws_sns_topic_subscription.email"]
    assert "endpoint" in str(sub.get("lifecycle")), "CI plans with a placeholder address"


def test_the_ecr_lifecycle_keeps_five_images() -> None:
    text = (STACK / "ecr.tf").read_text()
    assert 'countType = "imageCountMoreThan", countNumber = 5' in text


def test_the_tracked_tfvars_hold_only_the_image_digest_and_the_schedule_flag() -> None:
    text = (STACK / "image.auto.tfvars").read_text()
    lines = [x for x in text.splitlines() if x.strip() and not x.lstrip().startswith("#")]
    assert len(lines) == 2
    digest = re.fullmatch(r'image_digest\s+= "(sha256:[0-9a-f]{64})"', lines[0])
    assert digest, "the first assignment is the image digest"
    assert re.fullmatch(r"schedule_enabled\s+= true", lines[1])


def test_the_tracked_tfvars_are_not_git_ignored() -> None:
    argv = ["git", "check-ignore", "-q", "infra/batch/image.auto.tfvars"]  # noqa: S607
    assert subprocess.run(argv, cwd=ROOT, check=False).returncode == 1  # noqa: S603
