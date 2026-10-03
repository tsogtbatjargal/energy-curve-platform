"""Config-driven curve engine (ADR-0002).

Outright: Spot = anchor spot; C_k = spot * exp(s[k, month(as_of)]).
Spread:   long_position - short_position, from the already-rounded outright prices, so the
          displayed spread always equals displayed long minus displayed short.

Precision contract:
- Decimal arithmetic in a 34-significant-digit context (IEEE 754 decimal128). Decimal.exp is
  correctly rounded, so results do not depend on platform floating point.
- s[k, m] is the 10-decimal-place value from the shape parameters.
- Outright prices are quantized to 4 decimal places, round half even. Spreads are exact
  differences of those values.

Dates: a curve is built only for dates with an anchor spot. A spread needs both legs on the same
date; otherwise it is a gap with a reason. Nothing is forward-filled. A non-positive anchor is a
gap: a multiplicative shape on a negative price has no meaning.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal
from importlib import resources
from typing import Any

import polars as pl

from energy_curves.catalog import POSITIONS

PRICE_QUANTUM = Decimal("0.0001")
CTX = Context(prec=34, rounding=ROUND_HALF_EVEN)
ESTIMATE_TYPE = "modelled"
DISCLAIMER = "Modelled estimate, not market quotes"

CURVE_SCHEMA: dict[str, Any] = {
    "curve_id": pl.Utf8,
    "kind": pl.Utf8,
    "as_of_date": pl.Date,
    "position": pl.Enum(list(POSITIONS)),  # sorts Spot, C1, C2, C3, C4
    "price": pl.Decimal(18, 4),
    "status": pl.Utf8,
    "gap_reason": pl.Utf8,
    "estimate_type": pl.Utf8,
    "shape_source": pl.Utf8,
    "method_version": pl.Utf8,
    "shape_method_version": pl.Utf8,
    "params_sha256": pl.Utf8,
}


class MixedSourceError(ValueError):
    """Curve inputs come from more than one source."""


@dataclass(frozen=True)
class CurveConfig:
    method_version: str
    outrights: list[dict[str, str]]
    spreads: list[dict[str, str]]


def load_config(text: str | None = None) -> CurveConfig:
    if text is None:
        text = resources.files("energy_curves.curves").joinpath("curves.toml").read_text()
    doc = tomllib.loads(text)
    return CurveConfig(doc["method_version"], doc.get("outright", []), doc.get("spread", []))


def shape_factors(params: pl.DataFrame) -> dict[tuple[str, int], Decimal]:
    return {
        (r["position"], int(r["month"])): CTX.exp(Decimal(r["s"]))
        for r in params.iter_rows(named=True)
    }


def outright_point(spot: Decimal, factor: Decimal) -> Decimal:
    return CTX.multiply(spot, factor).quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def build_curves(
    current: pl.DataFrame,
    params: pl.DataFrame,
    *,
    params_sha256: str,
    shape_method_version: str,
    config: CurveConfig | None = None,
) -> pl.DataFrame:
    """Curve points for every date that has at least one anchor spot."""
    sources = current["source"].unique().to_list() if "source" in current.columns else []
    if len(sources) > 1:
        raise MixedSourceError(f"curve inputs come from several sources: {sorted(sources)}")
    config = config or load_config()
    factors = shape_factors(params)
    anchors = {c["anchor"] for c in config.outrights}
    spots: dict[str, dict[date, Decimal]] = {a: {} for a in anchors}
    for r in current.filter(pl.col("series_id").is_in(anchors)).iter_rows(named=True):
        spots[r["series_id"]][r["observation_date"]] = Decimal(r["price"])
    all_dates = sorted({d for by_date in spots.values() for d in by_date})

    base = {
        "estimate_type": ESTIMATE_TYPE,
        "method_version": config.method_version,
        "shape_method_version": shape_method_version,
        "params_sha256": params_sha256,
    }
    rows: list[dict[str, Any]] = []
    built: dict[tuple[str, date], dict[str, Decimal] | str] = {}
    for curve in config.outrights:
        for d in all_dates:
            spot = spots[curve["anchor"]].get(d)
            if spot is None or spot <= 0:
                gap = (
                    f"no {curve['anchor']} spot on {d}"
                    if spot is None
                    else f"non-positive {curve['anchor']} spot ({spot}); shape undefined"
                )
                built[(curve["id"], d)] = gap
                rows += _gap_rows(curve["id"], "outright", d, gap, curve["shape_source"], base)
                continue
            points = {"Spot": spot.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)}
            for pos in POSITIONS[1:]:
                points[pos] = outright_point(spot, factors[(pos, d.month)])
            built[(curve["id"], d)] = points
            rows += [
                {
                    "curve_id": curve["id"],
                    "kind": "outright",
                    "as_of_date": d,
                    "position": pos,
                    "price": price,
                    "status": "ok",
                    "gap_reason": None,
                    "shape_source": curve["shape_source"],
                    **base,
                }
                for pos, price in points.items()
            ]
    for spread in config.spreads:
        source = f"{spread['long']} minus {spread['short']}"
        for d in all_dates:
            long, short = built[(spread["long"], d)], built[(spread["short"], d)]
            if isinstance(long, str) or isinstance(short, str):
                reason = "; ".join(x for x in (long, short) if isinstance(x, str))
                rows += _gap_rows(spread["id"], "spread", d, reason, source, base)
                continue
            rows += [
                {
                    "curve_id": spread["id"],
                    "kind": "spread",
                    "as_of_date": d,
                    "position": pos,
                    "price": long[pos] - short[pos],
                    "status": "ok",
                    "gap_reason": None,
                    "shape_source": source,
                    **base,
                }
                for pos in POSITIONS
            ]
    return pl.DataFrame(rows, schema=CURVE_SCHEMA).sort("curve_id", "as_of_date", "position")


def _gap_rows(
    curve_id: str, kind: str, d: date, reason: str, source: str, base: dict[str, str]
) -> list[dict[str, Any]]:
    return [
        {
            "curve_id": curve_id,
            "kind": kind,
            "as_of_date": d,
            "position": pos,
            "price": None,
            "status": "gap",
            "gap_reason": reason,
            "shape_source": source,
            **base,
        }
        for pos in POSITIONS
    ]


def to_csv(curves: pl.DataFrame) -> str:
    """CSV export with the modelled-estimate disclaimer as a leading comment line."""
    return f"# {DISCLAIMER}\n" + curves.write_csv()
