"""Dependabot version updates are paused: every ecosystem opens no version-update pull requests.

GitHub's Dependabot options reference: `open-pull-requests-limit` defaults to 5 and "you can
temporarily disable version updates for a package manager by setting this option to zero".
Security-update pull requests have a separate fixed limit of 10 that this option does not set, so
security updates are not paused by it. Remove the lines to resume version updates.
"""

from pathlib import Path

import yaml

CONFIG = yaml.safe_load(
    (Path(__file__).parents[1] / ".github" / "dependabot.yml").read_text(encoding="utf-8")
)


def test_every_ecosystem_has_a_zero_version_update_limit() -> None:
    updates = CONFIG["updates"]
    assert {u["package-ecosystem"] for u in updates} == {
        "github-actions", "uv", "terraform", "docker"
    }  # fmt: skip
    for update in updates:
        assert update["open-pull-requests-limit"] == 0, update["package-ecosystem"]


def test_the_config_does_not_ignore_security_updates() -> None:
    for update in CONFIG["updates"]:
        for rule in update.get("ignore", []):
            assert not any("security" in t for t in rule.get("update-types", [])), rule
