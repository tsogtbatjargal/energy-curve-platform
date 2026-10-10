"""The seasonal-shape contract shared by the Polars reference and the PySpark job (ADR-0002/0007).

Standard library only, so the Glue job can import it. Precision contract:
- ln and median are computed in IEEE-754 float64 by each engine.
- Each s[k, m] is quantized to 10 decimal places (round half even) from the shortest round-trip
  repr of the float, and stored as a decimal string.
- Two implementations agree when keys and n_obs match exactly and |s_a - s_b| <= 1e-9.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

METHOD_VERSION = "shape-v1"
WINDOW = (date(2014, 1, 1), date(2024, 4, 5))
MIN_OBS = 15
S_QUANTUM = Decimal("1e-10")
PARITY_TOLERANCE = Decimal("1e-9")
POSITIONS = tuple(f"C{k}" for k in range(1, 5))
CSV_HEADER = "position,month,s,n_obs\n"


class ShapeEstimationError(ValueError):
    pass


def quantize_s(value: float) -> Decimal:
    if not math.isfinite(value):
        raise ShapeEstimationError(f"non-finite shape value {value}")
    return Decimal(repr(value)).quantize(S_QUANTUM, rounding=ROUND_HALF_EVEN)


def canonical_text(rows: Iterable[tuple[str, int, Decimal, int]]) -> str:
    """position,month,s,n_obs lines sorted by (position, month)."""
    return "".join(f"{p},{m},{s},{n}\n" for p, m, s, n in sorted(rows, key=lambda r: (r[0], r[1])))


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
