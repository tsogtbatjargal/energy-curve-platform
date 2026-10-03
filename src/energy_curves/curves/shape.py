"""Seasonal shape estimation (ADR-0002), Polars implementation.

s[k, m] = median over dates in calendar month m of ln(C_k / spot), within the window, using
WTI spot (RWTC) and WTI futures positions C1-C4 (RCLC1-RCLC4).

Precision contract (shared with the M5 PySpark implementation):
- ln and median are computed in IEEE-754 float64.
- Each s[k, m] is then quantized to 10 decimal places (round half even) from the shortest
  round-trip repr of the float, and stored as a decimal string. The curve engine uses only the
  quantized value, so curve prices do not depend on float formatting.
- Two implementations agree when keys and n_obs match exactly and |s_a - s_b| <= 1e-9
  (one unit in the 10th decimal place, covering a rounding-boundary flip).
- Non-positive prices stay in Silver/Gold and are excluded here only: ln(C/S) is undefined.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

import polars as pl

from energy_curves.catalog import WTI_FUTURES

METHOD_VERSION = "shape-v1"
WINDOW = (date(2014, 1, 1), date(2024, 4, 5))
MIN_OBS = 15
S_QUANTUM = Decimal("1e-10")
PARITY_TOLERANCE = Decimal("1e-9")
POSITIONS = tuple(f"C{k}" for k in range(1, 5))

PARAMS_SCHEMA = {
    "position": pl.Utf8,
    "month": pl.Int8,
    "s": pl.Decimal(20, 10),
    "n_obs": pl.Int64,
}


class ShapeEstimationError(ValueError):
    pass


@dataclass(frozen=True)
class ShapeResult:
    params: pl.DataFrame
    excluded: pl.DataFrame  # rows dropped from estimation, with reasons
    input_sha256: str
    params_sha256: str
    window: tuple[date, date]
    method_version: str = METHOD_VERSION


def quantize_s(value: float) -> Decimal:
    if not math.isfinite(value):
        raise ShapeEstimationError(f"non-finite shape value {value}")
    return Decimal(repr(value)).quantize(S_QUANTUM, rounding=ROUND_HALF_EVEN)


def canonical_params_text(params: pl.DataFrame) -> str:
    rows = params.sort(["position", "month"]).iter_rows(named=True)
    return "".join(f"{r['position']},{r['month']},{r['s']},{r['n_obs']}\n" for r in rows)


def _input_sha256(obs: pl.DataFrame) -> str:
    rows = obs.sort(["series_id", "observation_date"]).iter_rows(named=True)
    text = "".join(f"{r['series_id']},{r['observation_date']},{r['price']}\n" for r in rows)
    return hashlib.sha256(text.encode()).hexdigest()


def estimate_shape(
    observations: pl.DataFrame, window: tuple[date, date] = WINDOW, min_obs: int = MIN_OBS
) -> ShapeResult:
    """`observations` needs series_id, observation_date, price (Decimal)."""
    start, end = window
    if "source" in observations.columns:
        sources = (
            observations.filter(
                pl.col("series_id").is_in(["RWTC", *WTI_FUTURES])
                & pl.col("observation_date").is_between(start, end)
            )["source"]
            .unique()
            .to_list()
        )
        if len(sources) > 1:
            raise ShapeEstimationError(f"inputs come from more than one source: {sorted(sources)}")
    obs = observations.filter(
        pl.col("series_id").is_in(["RWTC", *WTI_FUTURES])
        & pl.col("observation_date").is_between(start, end)
    ).select("series_id", "observation_date", "price")

    spot = obs.filter(pl.col("series_id") == "RWTC").select(
        "observation_date", pl.col("price").alias("spot")
    )
    fut = obs.filter(pl.col("series_id").is_in(WTI_FUTURES)).with_columns(
        ("C" + pl.col("series_id").str.slice(-1)).alias("position")
    )
    pairs = fut.join(spot, on="observation_date", how="inner")

    bad = pairs.filter((pl.col("spot") <= 0) | (pl.col("price") <= 0))
    excluded = bad.select(
        "observation_date",
        "position",
        pl.when(pl.col("spot") <= 0)
        .then(pl.lit("non-positive spot"))
        .otherwise(pl.lit("non-positive future"))
        .alias("reason"),
    ).sort(["observation_date", "position"])

    good = pairs.filter((pl.col("spot") > 0) & (pl.col("price") > 0)).with_columns(
        (pl.col("price").cast(pl.Float64) / pl.col("spot").cast(pl.Float64)).log().alias("lr"),
        pl.col("observation_date").dt.month().cast(pl.Int8).alias("month"),
    )
    stats = (
        good.group_by("position", "month")
        .agg(pl.col("lr").median().alias("s_float"), pl.len().cast(pl.Int64).alias("n_obs"))
        .sort("position", "month")
    )

    expected = {(p, m) for p in POSITIONS for m in range(1, 13)}
    found = {(r["position"], r["month"]) for r in stats.iter_rows(named=True)}
    if missing := sorted(expected - found):
        raise ShapeEstimationError(f"no observations for {missing[:5]}... ({len(missing)} groups)")
    if thin := stats.filter(pl.col("n_obs") < min_obs).height:
        raise ShapeEstimationError(f"{thin} position/month groups have fewer than {min_obs} obs")

    params = pl.DataFrame(
        [
            {
                "position": r["position"],
                "month": r["month"],
                "s": quantize_s(r["s_float"]),
                "n_obs": r["n_obs"],
            }
            for r in stats.iter_rows(named=True)
        ],
        schema=PARAMS_SCHEMA,
    )
    return ShapeResult(
        params=params,
        excluded=excluded,
        input_sha256=_input_sha256(obs),
        params_sha256=hashlib.sha256(canonical_params_text(params).encode()).hexdigest(),
        window=window,
    )


CSV_HEADER = "position,month,s,n_obs\n"


def params_to_csv(params: pl.DataFrame) -> str:
    return CSV_HEADER + canonical_params_text(params)


def params_from_csv(text: str) -> pl.DataFrame:
    lines = text.splitlines()
    if not lines or lines[0] + "\n" != CSV_HEADER:
        raise ShapeEstimationError("unexpected shape parameter CSV header")
    rows = []
    for line in lines[1:]:
        position, month, s, n_obs = line.split(",")
        rows.append(
            {"position": position, "month": int(month), "s": Decimal(s), "n_obs": int(n_obs)}
        )
    return pl.DataFrame(rows, schema=PARAMS_SCHEMA)


def params_sha256(params: pl.DataFrame) -> str:
    return hashlib.sha256(canonical_params_text(params).encode()).hexdigest()


def compare_params(
    a: pl.DataFrame, b: pl.DataFrame, tolerance: Decimal = PARITY_TOLERANCE
) -> list[str]:
    """Differences between two parameter sets, compared as sorted data, not file bytes."""
    key = ["position", "month"]
    left, right = a.sort(key), b.sort(key)
    if left.select(key).to_dicts() != right.select(key).to_dicts():
        return ["parameter keys differ"]
    problems = []
    for ra, rb in zip(left.iter_rows(named=True), right.iter_rows(named=True), strict=True):
        where = f"{ra['position']}/{ra['month']:02d}"
        if ra["n_obs"] != rb["n_obs"]:
            problems.append(f"{where}: n_obs {ra['n_obs']} != {rb['n_obs']}")
        if abs(Decimal(ra["s"]) - Decimal(rb["s"])) > tolerance:
            problems.append(f"{where}: s {ra['s']} vs {rb['s']} exceeds {tolerance}")
    return problems
