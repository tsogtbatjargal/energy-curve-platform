"""Bronze -> Silver -> Gold transformations (Polars) and the quality gate.

Silver: typed observations from one run, plus quarantined rows with reasons.
Gold: the current accepted value per business key (source, series_id, observation_date), and
every superseded value in a revisions table with its lineage.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import polars as pl

from energy_curves.catalog import SERIES
from energy_curves.pipeline.parsing import PRICE_SCALE, Rejected, parse_row

PRICE = pl.Decimal(18, PRICE_SCALE)
KEY = ["source", "series_id", "observation_date"]
SILVER_SCHEMA: dict[str, Any] = {
    "source": pl.Utf8,
    "series_id": pl.Utf8,
    "observation_date": pl.Date,
    "price": PRICE,
    "unit": pl.Utf8,
    "non_positive": pl.Boolean,
    "retrieved_at": pl.Datetime("us", "UTC"),
    "logical_input_id": pl.Utf8,
    "raw_artifact_key": pl.Utf8,
}
REJECTED_SCHEMA: dict[str, Any] = {
    "source": pl.Utf8,
    "raw_artifact_key": pl.Utf8,
    "row_json": pl.Utf8,
    "reason": pl.Utf8,
}
REVISION_SCHEMA: dict[str, Any] = {
    **SILVER_SCHEMA,
    "superseded_at_version": pl.Int64,
    "superseded_by_logical_input_id": pl.Utf8,
}
STALE_BUSINESS_DAYS = 5


class SilverIntegrityError(RuntimeError):
    """A typed Silver frame lost values during construction."""


@dataclass(frozen=True)
class BronzePage:
    key: str
    sha256: str
    retrieved_at: datetime
    rows: list[dict[str, Any]]


@dataclass
class QualityReport:
    accepted: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    last_observation: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.rejected == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "passed" if self.passed else "quarantined",
            "accepted": self.accepted,
            "rejected": self.rejected,
            "reasons": dict(sorted(self.reasons.items())),
            "last_observation": dict(sorted(self.last_observation.items())),
            "warnings": self.warnings,
        }


def to_silver(
    pages: list[BronzePage], *, source: str, logical_input_id: str
) -> tuple[pl.DataFrame, pl.DataFrame]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for page in pages:
        for row in page.rows:
            try:
                obs = parse_row(row)
            except Rejected as exc:
                rejected.append(
                    {
                        "source": source,
                        "raw_artifact_key": page.key,
                        "row_json": json.dumps(row, sort_keys=True),
                        "reason": exc.reason,
                    }
                )
                continue
            accepted.append(
                {
                    "source": source,
                    "series_id": obs.series_id,
                    "observation_date": obs.observation_date,
                    "price": obs.price,
                    "unit": obs.unit,
                    "non_positive": obs.price <= 0,
                    "retrieved_at": page.retrieved_at,
                    "logical_input_id": logical_input_id,
                    "raw_artifact_key": page.key,
                }
            )
    silver = pl.DataFrame(accepted, schema=SILVER_SCHEMA)
    # Polars' row constructor turns values that do not fit a Decimal type into null instead of
    # raising. parse_price rejects such values first; this guards against any other path.
    if silver["price"].null_count():
        raise SilverIntegrityError(f"{silver['price'].null_count()} prices became null in Silver")
    rejected_df = pl.DataFrame(rejected, schema=REJECTED_SCHEMA)

    # Same key twice in one batch: identical values collapse; conflicting values are rejected.
    conflicts = (
        silver.group_by(KEY).agg(pl.col("price").n_unique().alias("n")).filter(pl.col("n") > 1)
    )
    if conflicts.height:
        bad = silver.join(conflicts.select(KEY), on=KEY, how="semi")
        rejected_df = pl.concat(
            [
                rejected_df,
                bad.select(
                    "source",
                    "raw_artifact_key",
                    pl.struct(pl.all())
                    .map_elements(_row_json, return_dtype=pl.Utf8)
                    .alias("row_json"),
                    pl.lit("conflicting duplicate in batch").alias("reason"),
                ),
            ]
        )
        silver = silver.join(conflicts.select(KEY), on=KEY, how="anti")
    silver = silver.unique(subset=KEY, keep="first", maintain_order=True).sort(KEY)
    return silver, rejected_df


def _row_json(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


def quality_check(
    silver: pl.DataFrame,
    rejected: pl.DataFrame,
    requested_series: list[str],
    window_end: date,
    previous_current: pl.DataFrame,
) -> QualityReport:
    report = QualityReport(accepted=silver.height, rejected=rejected.height)
    report.reasons = dict(Counter(rejected["reason"].to_list()))
    known = pl.concat([previous_current.select(silver.columns), silver])
    for sid in sorted(requested_series):
        dates = known.filter(pl.col("series_id") == sid)["observation_date"]
        expected_until = min(window_end, SERIES[sid].discontinued or window_end)
        if dates.is_empty():
            report.warnings.append(f"{sid}: no observations yet")
            continue
        last: date = dates.max()  # type: ignore[assignment]
        report.last_observation[sid] = last.isoformat()
        # Weekends and an unchanged source are normal; only a long gap is worth a warning.
        lag = business_days_after(last, expected_until)
        if lag > STALE_BUSINESS_DAYS:
            report.warnings.append(f"{sid}: last observation {last}, {lag} business days old")
    return report


def business_days_after(start: date, end: date) -> int:
    """Weekdays in (start, end]. Exchange holidays are not modelled; the threshold absorbs them."""
    return sum(
        1 for i in range(1, (end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5
    )


@dataclass(frozen=True)
class MergeStats:
    inserted: int
    revised: int
    unchanged: int
    stale: int = 0

    @property
    def changed(self) -> bool:
        return self.inserted > 0 or self.revised > 0


def merge_gold(
    current: pl.DataFrame,
    revisions: pl.DataFrame,
    silver: pl.DataFrame,
    *,
    dataset_version: int,
    logical_input_id: str,
) -> tuple[pl.DataFrame, pl.DataFrame, MergeStats, list[str]]:
    """Apply Silver to Gold. Revision ordering is per observation: a different price replaces the
    current one only if it was retrieved strictly later. An older or equally old retrieval with a
    different price is stale and leaves the current value alone, whatever the run's identity.
    """
    joined = silver.join(
        current.select([*KEY, "price", "retrieved_at"]), on=KEY, how="left", suffix="_old"
    )
    exists, differs = pl.col("price_old").is_not_null(), pl.col("price") != pl.col("price_old")
    newer = pl.col("retrieved_at") > pl.col("retrieved_at_old")
    drop = ["price_old", "retrieved_at_old"]
    new_rows = joined.filter(~exists).drop(drop)
    changed = joined.filter(exists & differs & newer).drop(drop)
    stale = joined.filter(exists & differs & ~newer)
    unchanged = joined.height - new_rows.height - changed.height - stale.height

    superseded = current.join(changed.select(KEY), on=KEY, how="semi").with_columns(
        pl.lit(dataset_version, pl.Int64).alias("superseded_at_version"),
        pl.lit(logical_input_id).alias("superseded_by_logical_input_id"),
    )
    new_current = (
        pl.concat([current.join(changed.select(KEY), on=KEY, how="anti"), changed, new_rows])
        .select(list(SILVER_SCHEMA))
        .sort(KEY)
    )
    new_revisions = pl.concat([revisions, superseded.select(list(REVISION_SCHEMA))]).sort(
        [*KEY, "superseded_at_version"]
    )
    stats = MergeStats(new_rows.height, changed.height, unchanged, stale.height)
    stale_notes = [
        f"{r['series_id']} {r['observation_date']}: retrieval {r['retrieved_at']} is not newer "
        f"than current {r['retrieved_at_old']}; kept current value"
        for r in stale.head(20).iter_rows(named=True)
    ]
    return new_current, new_revisions, stats, stale_notes


def empty(schema: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def to_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
