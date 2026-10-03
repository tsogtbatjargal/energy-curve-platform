"""AWS Glue 5.1 compatibility environment (ADR-0007).

Run under the Glue runtime's versions, not just PySpark's:
    UV_PROJECT_ENVIRONMENT=.venv-glue uv run --python 3.11 pytest -m glue_compat
"""

import math
import sys
from datetime import date, timedelta
from decimal import Decimal

import polars as pl
import pytest

pytestmark = pytest.mark.glue_compat

GLUE_PYTHON = (3, 11)
GLUE_SPARK = "3.5.6"
GLUE_JAVA_MAJOR = "17"


@pytest.fixture(scope="module")
def spark():  # type: ignore[no-untyped-def]
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder.master("local[1]")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    yield session
    session.stop()


def test_python_matches_glue() -> None:
    assert sys.version_info[:2] == GLUE_PYTHON, sys.version


def test_pyspark_matches_glue() -> None:
    import pyspark

    assert pyspark.__version__ == GLUE_SPARK


def test_spark_engine_and_java_match_glue(spark) -> None:  # type: ignore[no-untyped-def]
    assert spark.version == GLUE_SPARK
    java = spark.sparkContext._jvm.System.getProperty("java.version")  # noqa: SLF001
    assert java.split(".")[0] == GLUE_JAVA_MAJOR, java


def test_spark_median_log_ratio_matches_polars(spark) -> None:  # type: ignore[no-untyped-def]
    """Seed of the M5 parity test: the shape statistic computed by both engines agrees within
    the ADR-0002 tolerance, including even-sized groups (median of the two middle values)."""
    from pyspark.sql import functions as F

    from energy_curves.curves.shape import PARITY_TOLERANCE, quantize_s

    rows = []
    d = date(2014, 1, 1)
    for i in range(130):  # odd and even group sizes across months
        day = d + timedelta(days=i)
        spot = 50 + (i % 7) * 0.37
        for k in range(1, 5):
            rows.append((day, f"C{k}", spot, spot * math.exp(0.004 * k + 0.0003 * (i % 11))))

    pdf = pl.DataFrame(rows, schema=["day", "position", "spot", "future"], orient="row")
    polars_s = {
        (r["position"], r["month"]): quantize_s(r["s"])
        for r in pdf.with_columns(
            (pl.col("future") / pl.col("spot")).log().alias("lr"),
            pl.col("day").dt.month().alias("month"),
        )
        .group_by("position", "month")
        .agg(pl.col("lr").median().alias("s"))
        .iter_rows(named=True)
    }

    sdf = spark.createDataFrame(rows, ["day", "position", "spot", "future"])
    spark_s = {
        (r["position"], r["month"]): quantize_s(r["s"])
        for r in sdf.withColumn("lr", F.log(F.col("future") / F.col("spot")))
        .withColumn("month", F.month("day"))
        .groupBy("position", "month")
        .agg(F.median("lr").alias("s"))
        .collect()
    }
    assert polars_s.keys() == spark_s.keys()
    for key, value in polars_s.items():
        assert abs(Decimal(value) - Decimal(spark_s[key])) <= PARITY_TOLERANCE, key
