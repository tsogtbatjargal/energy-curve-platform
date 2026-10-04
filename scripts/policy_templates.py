"""Render infra policy templates (`*.json.tftpl`) as Terraform's templatefile() does (ADR-0018).

The templates use only `${name}` placeholders, so this renders the very same files and fails on
anything Terraform would interpret differently (`%{` directives, unknown names).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

BOOTSTRAP_POLICIES = Path(__file__).parents[1] / "infra" / "bootstrap" / "policies"
PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


def template(name: str, directory: Path = BOOTSTRAP_POLICIES) -> str:
    return (directory / f"{name}.json.tftpl").read_text()


def placeholders(name: str, directory: Path = BOOTSTRAP_POLICIES) -> set[str]:
    return set(PLACEHOLDER.findall(template(name, directory)))


def render(
    name: str, values: dict[str, str], directory: Path = BOOTSTRAP_POLICIES
) -> dict[str, Any]:
    text = template(name, directory)
    if "%{" in text:
        raise ValueError(f"{name}: template directives are not supported here")

    def value(m: re.Match[str]) -> str:
        if m.group(1) not in values:
            raise ValueError(f"{name}: unknown placeholder {m.group(0)}")
        return values[m.group(1)]

    return json.loads(PLACEHOLDER.sub(value, text))
