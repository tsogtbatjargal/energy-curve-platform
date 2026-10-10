"""Seasonal shape estimation on PySpark: the Glue job's logic (ADR-0002, ADR-0007, ADR-0023).

    s[k, m] = median over dates in calendar month m of ln(C_k / spot), inside the window,

the same statistic as `energy_curves.curves.shape` (the Polars reference), quantized by the shared
`shape_core` contract. This file imports only PySpark, the standard library and two standard-library
modules of the package, so it runs on Glue 5.1 (Python 3.11, Spark 3.5.6) without Polars.

The history is generated inside Spark from the seeded synthetic price function (synthetic data only;
nothing is read from outside the job). Remote output paths are exercised in M5b.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from energy_curves.curves.shape_core import (
    CSV_HEADER,
    MIN_OBS,
    POSITIONS,
    WINDOW,
    ShapeEstimationError,
    canonical_text,
    quantize_s,
    text_sha256,
)
from energy_curves.synthetic_prices import SHAPE_SERIES, history

FUTURES = tuple(s for s in SHAPE_SERIES if s != "RWTC")


@dataclass(frozen=True)
class ShapeOutcome:
    params: list[tuple[str, int, Decimal, int]]  # (position, month, s, n_obs), sorted
    excluded: list[tuple[date, str, str]]  # (date, position, reason), sorted
    params_sha256: str
    window: tuple[date, date]


def history_df(spark: Any, start: date, end: date, partitions: int = 4) -> Any:
    """The synthetic history for [start, end], generated in the executors, one task per slice."""
    from pyspark.sql import types as T

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    def day_rows(day: date) -> list[tuple[str, date, float]]:  # nested: pickled by value
        return [(sid, d, float(price)) for sid, d, price in history(SHAPE_SERIES, day, day)]

    rdd = spark.sparkContext.parallelize(days, partitions).flatMap(day_rows)
    schema = T.StructType(
        [
            T.StructField("series_id", T.StringType()),
            T.StructField("observation_date", T.DateType()),
            T.StructField("price", T.DoubleType()),
        ]
    )
    return spark.createDataFrame(rdd, schema)


def estimate_shape_spark(
    observations: Any, window: tuple[date, date] = WINDOW, min_obs: int = MIN_OBS
) -> ShapeOutcome:
    """`observations` needs series_id, observation_date, price (double). Raises the same errors,
    with the same messages, as the Polars reference."""
    from pyspark.sql import functions as F

    start, end = window
    obs = observations.filter(
        F.col("series_id").isin(["RWTC", *FUTURES])
        & F.col("observation_date").between(F.lit(start), F.lit(end))
    ).select("series_id", "observation_date", "price")

    spot = obs.filter(F.col("series_id") == "RWTC").select(
        "observation_date", F.col("price").alias("spot")
    )
    fut = obs.filter(F.col("series_id").isin(list(FUTURES))).withColumn(
        "position", F.concat(F.lit("C"), F.substring(F.col("series_id"), -1, 1))
    )
    pairs = fut.join(spot, on="observation_date", how="inner")

    bad = pairs.filter((F.col("spot") <= 0) | (F.col("price") <= 0))
    excluded = [
        (r["observation_date"], r["position"], r["reason"])
        for r in bad.select(
            "observation_date",
            "position",
            F.when(F.col("spot") <= 0, F.lit("non-positive spot"))
            .otherwise(F.lit("non-positive future"))
            .alias("reason"),
        )
        .orderBy("observation_date", "position")
        .collect()
    ]

    good = (
        pairs.filter((F.col("spot") > 0) & (F.col("price") > 0))
        .withColumn("lr", F.log(F.col("price") / F.col("spot")))
        .withColumn("month", F.month("observation_date"))
    )
    stats = (
        good.groupBy("position", "month")
        .agg(F.median("lr").alias("s_float"), F.count(F.lit(1)).alias("n_obs"))
        .collect()
    )

    expected = {(p, m) for p in POSITIONS for m in range(1, 13)}
    found = {(r["position"], r["month"]) for r in stats}
    if missing := sorted(expected - found):
        raise ShapeEstimationError(f"no observations for {missing[:5]}... ({len(missing)} groups)")
    if thin := sum(1 for r in stats if r["n_obs"] < min_obs):
        raise ShapeEstimationError(f"{thin} position/month groups have fewer than {min_obs} obs")

    params = sorted(
        ((r["position"], r["month"], quantize_s(r["s_float"]), int(r["n_obs"])) for r in stats),
        key=lambda row: (row[0], row[1]),
    )
    return ShapeOutcome(
        params=params,
        excluded=excluded,
        params_sha256=text_sha256(canonical_text(params)),
        window=window,
    )


def _write(spark: Any, text: str, path: str) -> None:
    """One-element text file under `path` (Spark writes `path/part-00000`)."""
    spark.sparkContext.parallelize([text.rstrip("\n")], 1).saveAsTextFile(path)


def main(argv: list[str] | None = None, spark: Any = None) -> str:
    """Generate the history, estimate the shape, write the parameters and their SHA-256 under
    --output. Returns the SHA-256."""
    parser = argparse.ArgumentParser(prog="shape_job")
    parser.add_argument("--start", type=date.fromisoformat, default=date(1983, 1, 3))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 4, 5))
    parser.add_argument("--window-start", type=date.fromisoformat, default=WINDOW[0])
    parser.add_argument("--window-end", type=date.fromisoformat, default=WINDOW[1])
    parser.add_argument("--partitions", type=int, default=8)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if spark is None:
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
    outcome = estimate_shape_spark(
        history_df(spark, args.start, args.end, args.partitions),
        (args.window_start, args.window_end),
    )
    output = args.output.rstrip("/")
    _write(spark, CSV_HEADER + canonical_text(outcome.params), f"{output}/shape_params.csv")
    _write(spark, outcome.params_sha256, f"{output}/shape_params.sha256")
    return outcome.params_sha256


if __name__ == "__main__":
    main()
