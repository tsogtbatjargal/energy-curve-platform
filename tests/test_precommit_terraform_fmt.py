"""The terraform-fmt pre-commit hook must only check the staged Terraform files.

It used to run `terraform fmt -recursive infra`, which also rewrote git-ignored `*.tfvars` (private
values) on any commit that staged an `infra/**/*.tf` file, and silently reformatted staged files.
Each test makes a throwaway git repository with this repo's `.pre-commit-config.yaml`, installs the
hook, and commits with every other hook skipped. Needs terraform through mise, so it is skipped
where that is unavailable (CI's python job); CI's iac-static job runs `terraform fmt -check`.
"""

import os
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / ".pre-commit-config.yaml"
FORMATTED = 'variable "x" {\n  type = string\n}\n'
UNFORMATTED = 'variable "x"   {\ntype=string\n}\n'
TFVARS = 'region   =   "ca-central-1"\n'  # unformatted on purpose; terraform fmt would align it


@cache
def terraform_available() -> bool:
    if shutil.which("mise") is None:
        return False
    probe = subprocess.run(["mise", "exec", "--", "terraform", "version"], capture_output=True)  # noqa: S607
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not terraform_available(), reason="needs terraform through mise")


def git(repo: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)  # noqa: S603, S607


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repo with the real hook config, a formatted infra/stack/main.tf, *.tfvars ignored."""
    root = tmp_path / "repo"
    (root / "infra/stack").mkdir(parents=True)
    hook_ids = [h["id"] for r in yaml.safe_load(CONFIG.read_text())["repos"] for h in r["hooks"]]
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-home"),
        "SKIP": ",".join(i for i in hook_ids if i != "terraform-fmt"),
    }
    assert "terraform-fmt" in hook_ids
    shutil.copy(CONFIG, root / ".pre-commit-config.yaml")
    (root / ".gitignore").write_text("*.tfvars\n")
    (root / "infra/stack/main.tf").write_text(FORMATTED)
    assert git(root, "init", "-q", "-b", "main", env=env).returncode == 0
    assert git(root, "add", ".", env=env).returncode == 0
    assert git(root, "commit", "-q", "-m", "init", env=env).returncode == 0
    install = subprocess.run([sys.executable, "-m", "pre_commit", "install"], cwd=root, env=env,
                             capture_output=True, text=True)  # fmt: skip
    assert install.returncode == 0, install.stderr
    return root, env


def commit(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return git(root, "commit", "-m", "change", env=env)


def test_committing_a_tf_file_leaves_an_ignored_tfvars_file_untouched(repo) -> None:
    root, env = repo
    tfvars = root / "infra/stack/terraform.tfvars"
    tfvars.write_text(TFVARS)
    (root / "infra/stack/main.tf").write_text(FORMATTED + '\nvariable "y" {\n  type = number\n}\n')
    git(root, "add", "infra/stack/main.tf", env=env)
    result = commit(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tfvars.read_text() == TFVARS


def test_an_unformatted_staged_tf_file_fails_the_commit_and_is_not_rewritten(repo) -> None:
    root, env = repo
    main = root / "infra/stack/main.tf"
    main.write_text(UNFORMATTED)
    git(root, "add", "infra/stack/main.tf", env=env)
    result = commit(root, env)
    assert result.returncode != 0
    assert main.read_text() == UNFORMATTED
    assert git(root, "rev-list", "--count", "HEAD", env=env).stdout.strip() == "1"


def test_an_unformatted_staged_test_file_fails_the_commit(repo) -> None:
    root, env = repo
    tftest = root / "infra/stack/tests/a.tftest.hcl"
    tftest.parent.mkdir()
    tftest.write_text('run "a" {\ncommand=plan\n}\n')
    git(root, "add", str(tftest.relative_to(root)), env=env)
    result = commit(root, env)
    assert result.returncode != 0
    assert tftest.read_text() == 'run "a" {\ncommand=plan\n}\n'


def test_only_the_staged_files_are_checked(repo) -> None:
    root, env = repo
    other = root / "infra/other/old.tf"
    other.parent.mkdir()
    other.write_text(UNFORMATTED)
    git(root, "add", str(other.relative_to(root)), env=env)
    git(root, "commit", "-q", "--no-verify", "-m", "an old unformatted file", env=env)
    (root / "infra/stack/main.tf").write_text(FORMATTED + '\nvariable "y" {\n  type = number\n}\n')
    git(root, "add", "infra/stack/main.tf", env=env)
    result = commit(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert other.read_text() == UNFORMATTED
