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
# Gold current: Silver columns plus the latest retrieval time at which the observation was seen.
# `retrieved_at` stays the retrieval that established the current price (lineage);
# `last_seen_at` advances whenever a later retrieval confirms or changes it (ordering).
GOLD_SCHEMA: dict[str, Any] = {**SILVER_SCHEMA, "last_seen_at": pl.Datetime("us", "UTC")}
REVISION_SCHEMA: dict[str, Any] = {
    **GOLD_SCHEMA,
    "superseded_at_version": pl.Int64,
    "superseded_by_logical_input_id": pl.Utf8,
}
STALE_BUSINESS_DAYS = 5
# Completeness (calibrated on real WTI/Brent history, where holiday differences reach 2 missing
# days in 5-10 day windows, 3 in 30 days, and 5 = ~8% in 90 days).
INCOMPLETE_MIN_DAYS = 3
INCOMPLETE_SHARE = 0.10


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
    incomplete: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.rejected == 0 and not self.incomplete

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "passed" if self.passed else "quarantined",
            "accepted": self.accepted,
            "rejected": self.rejected,
            "reasons": dict(sorted(self.reasons.items())),
            "last_observation": dict(sorted(self.last_observation.items())),
            "warnings": self.warnings,
            "incomplete": self.incomplete,
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


def completeness_issues(
    silver: pl.DataFrame, requests: list[tuple[tuple[str, ...], date, date]]
) -> list[str]:
    """Series that are missing from a batch, judged within each request.

    Evidence is the set of dates on which any other series in the same request returned data
    (inside this series' active period). A series is incomplete when it lacks more of those dates
    than legitimate calendar differences explain, or returns nothing while the others returned
    at least INCOMPLETE_MIN_DAYS dates. A request where nothing returned data is a calendar gap,
    not an incomplete batch. Single-series requests carry no cross-evidence; staleness warnings
    cover them.
    """
    issues = []
    for series_ids, start, end in requests:
        if len(series_ids) < 2:
            continue
        rows = silver.filter(
            pl.col("series_id").is_in(series_ids)
            & pl.col("observation_date").is_between(start, end)
        )
        dates = {
            sid: set(rows.filter(pl.col("series_id") == sid)["observation_date"].to_list())
            for sid in series_ids
        }
        for sid in sorted(series_ids):
            active_until = min(end, SERIES[sid].discontinued or end)
            evidence = {
                d for other in series_ids if other != sid for d in dates[other] if d <= active_until
            }
            if not evidence:
                continue
            missing = len(evidence - dates[sid])
            allowed = max(INCOMPLETE_MIN_DAYS, INCOMPLETE_SHARE * len(evidence))
            absent = not dates[sid] and len(evidence) >= INCOMPLETE_MIN_DAYS
            if missing > allowed or absent:
                issues.append(
                    f"{sid}: no rows on {missing} of {len(evidence)} dates with data from other "
                    "requested series"
                )
    return issues


def quality_check(
    silver: pl.DataFrame,
    rejected: pl.DataFrame,
    requested_series: list[str],
    window_end: date,
    previous_current: pl.DataFrame,
    requests: list[tuple[tuple[str, ...], date, date]] | None = None,
) -> QualityReport:
    report = QualityReport(accepted=silver.height, rejected=rejected.height)
    report.incomplete = completeness_issues(silver, requests or [])
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
    seen: int = 0  # same price, later retrieval: watermark advanced, no revision

    @property
    def price_changes(self) -> int:
        """Inserted or revised prices: the only changes that may drive revisions or alerts."""
        return self.inserted + self.revised

    @property
    def state_changed(self) -> bool:
        return self.price_changes > 0 or self.seen > 0

    def as_dict(self) -> dict[str, int]:
        return {**self.__dict__, "price_changes": self.price_changes}


def with_last_seen(df: pl.DataFrame) -> pl.DataFrame:
    """Gold written before `last_seen_at` existed: its best watermark is `retrieved_at`."""
    if "last_seen_at" in df.columns:
        return df
    return df.with_columns(pl.col("retrieved_at").alias("last_seen_at"))


def merge_gold(
    current: pl.DataFrame,
    revisions: pl.DataFrame,
    silver: pl.DataFrame,
    *,
    dataset_version: int,
    logical_input_id: str,
) -> tuple[pl.DataFrame, pl.DataFrame, MergeStats, list[str]]:
    """Apply Silver to Gold with per-observation retrieval ordering.

    Ordering compares each incoming retrieval with the observation's `last_seen_at`: the latest
    retrieval that has seen it, whether or not the price changed. Content deduplication (exact
    replays) happens earlier, on the run identity, and is independent of this.

    - new key: inserted
    - later retrieval, different price: revised (old row to revisions)
    - later retrieval, same price: watermark advanced only (no revision, no price change)
    - not-later retrieval, different price: stale, current kept
    - not-later retrieval, same price: unchanged
    """
    current, revisions = with_last_seen(current), with_last_seen(revisions)
    joined = silver.join(
        current.select([*KEY, "price", "last_seen_at"]), on=KEY, how="left", suffix="_old"
    )
    exists = pl.col("price_old").is_not_null()
    differs = pl.col("price") != pl.col("price_old")
    later = pl.col("retrieved_at") > pl.col("last_seen_at")
    drop = ["price_old", "last_seen_at"]
    new_rows = joined.filter(~exists).drop(drop)
    changed = joined.filter(exists & differs & later).drop(drop)
    seen = joined.filter(exists & ~differs & later).select([*KEY, "retrieved_at"])
    stale = joined.filter(exists & differs & ~later)
    unchanged = joined.height - new_rows.height - changed.height - seen.height - stale.height

    superseded = current.join(changed.select(KEY), on=KEY, how="semi").with_columns(
        pl.lit(dataset_version, pl.Int64).alias("superseded_at_version"),
        pl.lit(logical_input_id).alias("superseded_by_logical_input_id"),
    )
    kept = current.join(changed.select(KEY), on=KEY, how="anti")
    kept = (
        kept.join(seen.rename({"retrieved_at": "seen_at"}), on=KEY, how="left")
        .with_columns(pl.coalesce("seen_at", "last_seen_at").alias("last_seen_at"))
        .drop("seen_at")
    )
    incoming = pl.concat([changed, new_rows]).with_columns(
        pl.col("retrieved_at").alias("last_seen_at")
    )
    new_current = pl.concat(
        [kept.select(list(GOLD_SCHEMA)), incoming.select(list(GOLD_SCHEMA))]
    ).sort(KEY)
    new_revisions = pl.concat([revisions, superseded.select(list(REVISION_SCHEMA))]).sort(
        [*KEY, "superseded_at_version"]
    )
    stats = MergeStats(new_rows.height, changed.height, unchanged, stale.height, seen.height)
    stale_notes = [
        f"{r['series_id']} {r['observation_date']}: retrieval {r['retrieved_at']} is not later "
        f"than last seen {r['last_seen_at']}; kept current value"
        for r in stale.head(20).iter_rows(named=True)
    ]
    return new_current, new_revisions, stats, stale_notes


def empty(schema: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def to_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
