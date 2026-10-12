"""R3 (PLAN.md, ADR-0011 addendum): public outputs are synthetic only.

The GitHub Pages snapshot, screenshots, the video and the README show nothing real. This module is
the guard the snapshot builder must call on its input (`require_synthetic_store`) and the checks
CI runs over every public file:
- `require_synthetic_store`: the store is not under the repository's git-ignored `data/` or the
  configured real-data directory (symlinks resolved), has a published pointer, and its manifest
  says `source: synthetic`. Anything unreadable is a refusal.
- `check_artifact`: an output carries the modelled-estimate disclaimer AND a synthetic notice, and
  every price in it is one the synthetic generator can produce (or a documented example).
- `check_workflow`: a Pages workflow reads no real-data path, no EIA key and no real source.

The price check is a tripwire for stray real values, not a proof; the proof is that the builder only
reads a store that passed `require_synthetic_store`. The opt-in realdata test compares every public
file with the local cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import date, timedelta
from functools import cache
from pathlib import Path

from energy_curves.catalog import SERIES
from energy_curves.curves.engine import DISCLAIMER
from energy_curves.synthetic_prices import synthetic_price

REPO = Path(__file__).resolve().parents[1]
PUBLIC_DIRS = ("public", "site", "snapshot")
EXAMPLES = frozenset({"61.25", "99.99"})  # synthetic values the README and tests document
# SHA-256 of the two canonical price strings of the real 2020-04-20 WTI and Brent settlements.
# Digests, not values: the repository holds no real settlement, not even in the guard.
REAL_SETTLEMENT_SHA256 = frozenset(
    {
        "2f3825b49f756038e424714a9ad00478021705a801e9fa3d4184a9a153c04443",
        "3ce33b49cff17108df31481c7868deb408559b32458b6eea1ee071177dffd12e",
    }
)
REAL_DATA_VARIABLES = ("ECP_REAL_DATA_DIR", "DATA_DIR")
SERIES_WORDS = re.compile(r"WTI|Brent|RWTC|RBRTE|RCLC|\bprice|crude|\$", re.IGNORECASE)
PRICE = re.compile(r"(?<![\d.])-?\d{1,4}\.\d{2}(?![\d.]|\.\d)")
WORKFLOW_FORBIDDEN = (
    (re.compile(r"(?<![\w.-])data/"), "reads the git-ignored data/ folder"),
    (re.compile(r"ECP_REAL_DATA_DIR|\bDATA_DIR\b"), "names a real-data directory"),
    (re.compile(r"EIA_API_KEY"), "uses the EIA API key"),
    (re.compile(r"(--source[ =]|ECP_SOURCE=)\s*eia\b", re.IGNORECASE), "selects the real source"),
)


class PublicOutputRefused(ValueError):
    """A public output, or the input it would be built from, may not be real data."""


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def require_synthetic_store(
    root: Path, repo: Path = REPO, env: Mapping[str, str] | None = None
) -> None:
    env = os.environ if env is None else env
    resolved = root.resolve()
    forbidden = [(repo / "data").resolve()]
    forbidden += [Path(env[v]).resolve() for v in REAL_DATA_VARIABLES if env.get(v)]
    if any(_inside(resolved, bad) or _inside(bad, resolved) for bad in forbidden):
        raise PublicOutputRefused("the input is (or contains) the real-data directory")
    try:
        pointer = json.loads((resolved / "published" / "current.json").read_text())
    except FileNotFoundError:
        raise PublicOutputRefused("nothing is published in this store") from None
    except (ValueError, OSError):
        raise PublicOutputRefused("the published pointer cannot be read") from None
    try:
        manifest = json.loads((resolved / str(pointer["manifest_key"])).read_text())
        source = manifest["identity"]["source"]
    except (ValueError, OSError, KeyError, TypeError):
        raise PublicOutputRefused("the published manifest cannot be read") from None
    if source != "synthetic":
        raise PublicOutputRefused("the manifest's source is not synthetic")


@cache
def synthetic_prices() -> frozenset[str]:
    """Every price the synthetic generator can produce over its history, plus the examples."""
    values = set(EXAMPLES)
    day, end = date(1983, 1, 3), date(2024, 12, 31)
    while day <= end:
        for series_id in SERIES:
            price = synthetic_price(series_id, day)
            if price is not None:
                values.add(str(price))
        day += timedelta(days=1)
    return frozenset(values)


def check_prices(text: str, name: str) -> None:
    allowed = synthetic_prices()
    for number, line in enumerate(text.splitlines(), 1):
        if not SERIES_WORDS.search(line):
            continue
        for token in PRICE.findall(line):
            if token not in allowed:
                raise PublicOutputRefused(
                    f"{name}:{number}: a price that the synthetic generator cannot produce"
                )


def has_real_settlement(text: str) -> bool:
    return any(
        hashlib.sha256(token.encode()).hexdigest() in REAL_SETTLEMENT_SHA256
        for token in PRICE.findall(text)
    )


def check_artifact(text: str, name: str) -> None:
    if has_real_settlement(text):
        raise PublicOutputRefused(f"{name}: carries a real settlement")
    if DISCLAIMER not in text or "synthetic" not in text.lower():
        raise PublicOutputRefused(
            f"{name}: lacks the modelled-estimate disclaimer or the synthetic notice"
        )
    check_prices(text, name)


def check_workflow(text: str, name: str) -> None:
    for pattern, problem in WORKFLOW_FORBIDDEN:
        if pattern.search(text):
            raise PublicOutputRefused(f"{name}: {problem}")


def public_files(repo: Path = REPO) -> list[Path]:
    """The README, the Pages workflow(s) and everything in the snapshot output directories."""
    files = [repo / "README.md"]
    files += sorted((repo / ".github" / "workflows").glob("pages*.y*ml"))
    for directory in PUBLIC_DIRS:
        files += sorted(p for p in (repo / directory).rglob("*") if p.is_file())
    return [p for p in files if p.exists()]


def real_price_strings(data_dir: Path) -> set[str]:
    """The price strings of the local real-data cache (every published gold current file), minus
    those the synthetic generator also produces, so a coincidence is not a leak."""
    import io

    import polars as pl

    found: set[str] = set()
    for path in data_dir.rglob("current.parquet"):
        frame = pl.read_parquet(io.BytesIO(path.read_bytes()))
        if "price" in frame.columns:
            found |= {f"{v:.2f}" for v in frame["price"].drop_nulls().cast(pl.Float64)}
    return found - synthetic_prices()
