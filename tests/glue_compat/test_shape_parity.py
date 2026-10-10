"""M5a (ADR-0007, ADR-0023): the PySpark shape job reproduces the Polars reference.

Both run on the same generated history. Keys and `n_obs` must be identical; `s` within 1e-9
(ADR-0002); and, as the stronger check, the canonical parameter text and its SHA-256 are equal. A
rounding-boundary flip is the only allowed difference, and it is reported rather than hidden.
"""

import hashlib
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from energy_curves.curves import shape
from energy_curves.curves.shape_core import (
    MIN_OBS,
    PARITY_TOLERANCE,
    WINDOW,
    ShapeEstimationError,
    canonical_text,
    text_sha256,
)
from energy_curves.synthetic_prices import NEGATIVE_DAY, SHAPE_SERIES, history

pytestmark = pytest.mark.glue_compat

FULL = (date(1983, 1, 3), date(2024, 4, 5))
SLICE = (date(2014, 1, 1), date(2014, 12, 31))


@pytest.fixture(scope="module")
def spark():  # type: ignore[no-untyped-def]
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder.master("local[2]")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "4")
        .getOrCreate()
    )
    yield session
    session.stop()


def polars_frame(rows: list[tuple[str, date, Decimal]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"series_id": pl.Utf8, "observation_date": pl.Date, "price": pl.Decimal(12, 2)},
        orient="row",
    )


def spark_frame(spark: Any, rows: list[tuple[str, date, Decimal]]) -> Any:
    from pyspark.sql import types as T

    schema = T.StructType(
        [
            T.StructField("series_id", T.StringType()),
            T.StructField("observation_date", T.DateType()),
            T.StructField("price", T.DoubleType()),
        ]
    )
    return spark.createDataFrame([(s, d, float(p)) for s, d, p in rows], schema)


def both(spark: Any, rows: list[tuple[str, date, Decimal]], window: tuple[date, date] = WINDOW):
    import shape_job

    reference = shape.estimate_shape(polars_frame(rows), window)
    result = shape_job.estimate_shape_spark(spark_frame(spark, rows), window)
    return reference, result


def as_polars_rows(reference: Any) -> list[tuple[str, int, Decimal, int]]:
    return [
        (r["position"], r["month"], r["s"], r["n_obs"])
        for r in reference.params.sort(["position", "month"]).iter_rows(named=True)
    ]


def assert_parity(reference: Any, result: Any) -> None:
    left, right = as_polars_rows(reference), result.params
    assert [(p, m, n) for p, m, _, n in left] == [(p, m, n) for p, m, _, n in right]
    assert len(left) == 48
    flips = [(p, m) for (p, m, a, _), (_, _, b, _) in zip(left, right, strict=True) if a != b]
    for (p, m, a, _), (_, _, b, _) in zip(left, right, strict=True):
        assert abs(a - b) <= PARITY_TOLERANCE, (p, m, a, b)
    # The stronger check: identical text. A flip inside the tolerance is reported, not ignored.
    assert not flips, f"rounding-boundary flips within 1e-9: {flips}"
    assert result.params_sha256 == reference.params_sha256


# --- the generated history ----------------------------------------------------------------------


def test_the_spark_job_reproduces_the_polars_shape_on_a_one_year_slice(spark) -> None:  # type: ignore[no-untyped-def]
    import shape_job

    rows = list(history(SHAPE_SERIES, *SLICE))
    reference = shape.estimate_shape(polars_frame(rows), SLICE, min_obs=15)
    result = shape_job.estimate_shape_spark(spark_frame(spark, rows), SLICE, min_obs=15)
    assert_parity(reference, result)


def test_the_spark_job_reproduces_the_polars_shape_on_the_full_history(spark) -> None:  # type: ignore[no-untyped-def]
    import shape_job

    rows = list(history(SHAPE_SERIES, *FULL))
    assert 50_000 < len(rows) < 70_000  # 1983 to 2024-04-05: 10,765 business days, 5 series
    reference = shape.estimate_shape(polars_frame(rows))
    result = shape_job.estimate_shape_spark(spark_frame(spark, rows))
    assert_parity(reference, result)


def test_a_history_generated_inside_spark_equals_the_one_generated_here(spark) -> None:  # type: ignore[no-untyped-def]
    import shape_job

    df = shape_job.history_df(spark, *SLICE, partitions=3)
    got = sorted((r.series_id, r.observation_date, r.price) for r in df.collect())
    want = sorted((s, d, float(p)) for s, d, p in history(SHAPE_SERIES, *SLICE))
    assert got == want


def test_generation_inside_spark_then_estimation_equals_the_polars_reference(spark) -> None:  # type: ignore[no-untyped-def]
    import shape_job

    result = shape_job.estimate_shape_spark(shape_job.history_df(spark, *FULL, partitions=8))
    reference = shape.estimate_shape(polars_frame(list(history(SHAPE_SERIES, *FULL))))
    assert_parity(reference, result)


def test_the_negative_price_day_is_excluded_by_both_with_the_same_reason(spark) -> None:  # type: ignore[no-untyped-def]
    rows = list(history(SHAPE_SERIES, *FULL))
    reference, result = both(spark, rows)
    want = [
        (r["observation_date"], r["position"], r["reason"])
        for r in reference.excluded.iter_rows(named=True)
    ]
    assert want == result.excluded
    assert {d for d, _, _ in want} == {NEGATIVE_DAY}
    assert {reason for _, _, reason in want} == {"non-positive spot"}
    assert len(want) == 4


def test_two_runs_give_identical_parameter_bytes(spark) -> None:  # type: ignore[no-untyped-def]
    import shape_job

    a = shape_job.estimate_shape_spark(shape_job.history_df(spark, *SLICE, partitions=2), SLICE)
    b = shape_job.estimate_shape_spark(shape_job.history_df(spark, *SLICE, partitions=5), SLICE)
    assert canonical_text(a.params) == canonical_text(b.params)
    assert a.params_sha256 == b.params_sha256 == text_sha256(canonical_text(a.params))


# --- edge cases both engines must treat alike ---------------------------------------------------


def crafted(days_per_month: dict[int, int], bump: float = 0.0) -> list[tuple[str, date, Decimal]]:
    rows = []
    for month, count in days_per_month.items():
        for i in range(count):
            d = date(2014, month, i + 1)
            cent = Decimal("0.01")  # prices are two-decimal values, as the Decimal(12, 2) column is
            spot = (Decimal("50.00") + Decimal(i) / 10).quantize(cent)
            rows.append(("RWTC", d, spot))
            for k in range(1, 5):
                rows.append(
                    (
                        f"RCLC{k}",
                        d,
                        (spot + k + Decimal(str(bump * i)) * (k + i % 3)).quantize(cent),
                    )
                )
    return rows


def test_odd_and_even_group_sizes_agree(spark) -> None:  # type: ignore[no-untyped-def]
    counts = {m: 15 + (m % 2) + m % 5 for m in range(1, 13)}  # 15..21, odd and even
    reference, result = both(
        spark, crafted(counts, bump=0.013), (date(2014, 1, 1), date(2014, 12, 31))
    )
    assert_parity(reference, result)


def test_a_group_below_the_minimum_raises_the_same_error_in_both(spark) -> None:  # type: ignore[no-untyped-def]
    counts = {m: 16 for m in range(1, 13)} | {7: MIN_OBS - 1}
    with pytest.raises(ShapeEstimationError) as polars_error:
        shape.estimate_shape(polars_frame(crafted(counts)), (date(2014, 1, 1), date(2014, 12, 31)))
    import shape_job

    with pytest.raises(ShapeEstimationError) as spark_error:
        shape_job.estimate_shape_spark(
            spark_frame(spark, crafted(counts)), (date(2014, 1, 1), date(2014, 12, 31))
        )
    assert str(polars_error.value) == str(spark_error.value)


def test_a_missing_position_month_raises_the_same_error_in_both(spark) -> None:  # type: ignore[no-untyped-def]
    counts = {m: 16 for m in range(1, 13) if m != 5}
    window = (date(2014, 1, 1), date(2014, 12, 31))
    with pytest.raises(ShapeEstimationError) as polars_error:
        shape.estimate_shape(polars_frame(crafted(counts)), window)
    import shape_job

    with pytest.raises(ShapeEstimationError) as spark_error:
        shape_job.estimate_shape_spark(spark_frame(spark, crafted(counts)), window)
    assert str(polars_error.value) == str(spark_error.value)


def test_a_window_that_cuts_a_month_agrees(spark) -> None:  # type: ignore[no-untyped-def]
    rows = crafted({m: 28 for m in range(1, 13)})
    window = (date(2014, 1, 1), date(2014, 12, 14))  # December keeps 14 days: below the minimum
    with pytest.raises(ShapeEstimationError) as polars_error:
        shape.estimate_shape(polars_frame(rows), window)
    import shape_job

    with pytest.raises(ShapeEstimationError) as spark_error:
        shape_job.estimate_shape_spark(spark_frame(spark, rows), window)
    assert str(polars_error.value) == str(spark_error.value)
    reference, result = both(spark, rows, (date(2014, 1, 1), date(2014, 12, 15)))  # 15 days: enough
    assert_parity(reference, result)


def test_a_non_positive_future_is_excluded_by_both(spark) -> None:  # type: ignore[no-untyped-def]
    rows = crafted({m: 20 for m in range(1, 13)})
    rows = [
        (s, d, Decimal("-1.00") if (s, d) == ("RCLC2", date(2014, 3, 4)) else p) for s, d, p in rows
    ]
    reference, result = both(spark, rows, (date(2014, 1, 1), date(2014, 12, 31)))
    want = [
        (r["observation_date"], r["position"], r["reason"])
        for r in reference.excluded.iter_rows(named=True)
    ]
    assert want == result.excluded == [(date(2014, 3, 4), "C2", "non-positive future")]
    assert_parity(reference, result)


def test_inputs_from_other_series_are_ignored(spark) -> None:  # type: ignore[no-untyped-def]
    rows = crafted({m: 18 for m in range(1, 13)})
    extra = [("RBRTE", d, Decimal("99.00")) for s, d, _ in rows if s == "RWTC"]
    window = (date(2014, 1, 1), date(2014, 12, 31))
    reference, _ = both(spark, rows, window)
    import shape_job

    with_brent = shape_job.estimate_shape_spark(spark_frame(spark, rows + extra), window)
    assert as_polars_rows(reference) == with_brent.params


# --- the job's entry point ----------------------------------------------------------------------


def test_main_writes_the_parameters_and_their_hash(spark, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    import shape_job

    out = tmp_path / "out"
    digest = shape_job.main(
        ["--start", "2014-01-01", "--end", "2014-12-31", "--window-start", "2014-01-01",
         "--window-end", "2014-12-31", "--output", str(out)],
        spark=spark,
    )  # fmt: skip
    text = (out / "shape_params.csv" / "part-00000").read_text()
    assert text.startswith("position,month,s,n_obs\n") and text.count("\n") == 49
    assert hashlib.sha256(text.split("\n", 1)[1].encode()).hexdigest() == digest
    assert (out / "shape_params.sha256" / "part-00000").read_text().strip() == digest
    reference = shape.estimate_shape(polars_frame(list(history(SHAPE_SERIES, *SLICE))), SLICE)
    assert digest == reference.params_sha256


# --- the built bundle, shipped the way Glue ships it ---------------------------------------------

REFERENCE_SHA256 = "4a111aa831712d152a387c8c8dcd9687132241ea0488a05dbdc925eacf253af4"


def submit(tmp_path: Path, master: str, py_files: list[Path]) -> subprocess.CompletedProcess[str]:
    """Run jobs/glue/shape_job.py under spark-submit with an interpreter that has no site-packages,
    so the package can only come from --py-files (as on a Glue executor)."""
    import pyspark

    spark_home = Path(pyspark.__file__).parent
    isolated = tmp_path / "python-isolated"
    isolated.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    isolated.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "SPARK_HOME": str(spark_home),
        "PYSPARK_PYTHON": str(isolated),
        "PYSPARK_DRIVER_PYTHON": str(isolated),
    }
    if "JAVA_HOME" in os.environ:
        env["JAVA_HOME"] = os.environ["JAVA_HOME"]
    command = [
        str(spark_home / "bin" / "spark-submit"),
        "--master", master,
        "--conf", "spark.ui.enabled=false",
        *(["--py-files", ",".join(map(str, py_files))] if py_files else []),
        str(Path(__file__).parents[2] / "jobs" / "glue" / "shape_job.py"),
        "--partitions", "8",
        "--output", str(tmp_path / "out"),
    ]  # fmt: skip
    return subprocess.run(  # noqa: S603
        command, env=env, capture_output=True, text=True, timeout=600, check=False
    )


def test_the_built_bundle_gives_the_reference_hash_on_separate_executors(tmp_path: Path) -> None:
    """The cloud-shaped run: the job script and only the built zip, two executor processes (each
    with its own Python worker), no package on the path. It must print the reference hash."""
    import build_glue_bundle

    bundle = tmp_path / "energy_curves_m5.zip"
    build_glue_bundle.write(bundle)
    done = submit(tmp_path, "local-cluster[2,1,1024]", [bundle])
    assert done.returncode == 0, done.stderr[-3000:]
    out = tmp_path / "out"
    assert (out / "shape_params.sha256" / "part-00000").read_text().strip() == REFERENCE_SHA256
    text = (out / "shape_params.csv" / "part-00000").read_text()
    assert hashlib.sha256(text.split("\n", 1)[1].encode()).hexdigest() == REFERENCE_SHA256


def test_without_the_bundle_the_same_run_cannot_import_the_package(tmp_path: Path) -> None:
    """The control: the isolation is real, so the test above proves the zip carries the package."""
    done = submit(tmp_path, "local[2]", [])
    assert done.returncode != 0
    assert "No module named 'energy_curves'" in done.stdout + done.stderr


def test_the_reference_hash_is_what_the_polars_reference_gives_on_the_full_history() -> None:
    reference = shape.estimate_shape(polars_frame(list(history(SHAPE_SERIES, *FULL))))
    assert reference.params_sha256 == REFERENCE_SHA256
