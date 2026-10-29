"""The dependency audit has no weekly schedule: the project is complete.

It still runs on pull requests (a required check), on pushes to main and by hand.
"""

from pathlib import Path

import yaml

PATH = Path(__file__).parents[1] / ".github" / "workflows" / "dependency-audit.yml"
TEXT = PATH.read_text(encoding="utf-8")


def triggers() -> dict[str, object]:
    loaded = yaml.safe_load(TEXT)
    return dict(loaded.get("on") or loaded[True])  # YAML 1.1 reads the key `on` as True


def test_there_is_no_schedule_trigger() -> None:
    assert "schedule" not in triggers()
    assert "cron" not in TEXT


def test_the_pull_request_push_and_manual_triggers_remain() -> None:
    on = triggers()
    assert set(on) == {"pull_request", "push", "workflow_dispatch"}
    assert on["push"] == {"branches": ["main"]}


def test_a_comment_says_why_the_schedule_was_removed() -> None:
    assert "schedule was removed because the project is complete" in TEXT
