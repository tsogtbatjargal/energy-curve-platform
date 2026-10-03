"""Finding 5: a batch missing a requested series is quarantined; calendar gaps are not.

Thresholds come from real WTI/Brent history: holiday differences reach 2 missing days in 5-10 day
windows, 3 in 30 days, and 5 (about 8%) in 90 days.
"""

from datetime import date
from pathlib import Path

from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import POINTER_KEY
from energy_curves.pipeline.runner import FetchRequest, run_ingest

JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]


def ingest(tmp: Path, requests, **source_kw):  # type: ignore[no-untyped-def]
    return run_ingest(tmp, SyntheticSource(**source_kw), requests, source_name="synthetic")


def test_requested_series_missing_entirely_is_quarantined(tmp_path: Path) -> None:
    result = ingest(tmp_path, JAN, omit=lambda sid, d: sid == "RBRTE")
    assert result.status == "quarantined"
    assert result.quality["incomplete"] == [
        "RBRTE: no rows on 23 of 23 dates with data from other requested series"
    ]
    assert not (tmp_path / POINTER_KEY).exists()


def test_series_truncated_mid_window_is_quarantined(tmp_path: Path) -> None:
    result = ingest(tmp_path, JAN, omit=lambda sid, d: sid == "RBRTE" and d.day > 15)
    assert result.status == "quarantined"
    assert result.quality["incomplete"][0].startswith("RBRTE: no rows on 12 of 23")


def test_holiday_differences_are_allowed(tmp_path: Path) -> None:
    holidays = {date(2024, 1, 1), date(2024, 1, 15), date(2024, 1, 26)}  # 3 missing days
    result = ingest(tmp_path, JAN, omit=lambda sid, d: sid == "RBRTE" and d in holidays)
    assert result.status == "published"
    assert result.quality["incomplete"] == []


def test_four_missing_days_in_a_month_is_incomplete(tmp_path: Path) -> None:
    gaps = {date(2024, 1, d) for d in (2, 9, 16, 23)}
    result = ingest(tmp_path, JAN, omit=lambda sid, d: sid == "RBRTE" and d in gaps)
    assert result.status == "quarantined"


def test_short_window_with_one_series_absent_is_incomplete(tmp_path: Path) -> None:
    three_days = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 2), date(2024, 1, 4))]
    result = ingest(tmp_path, three_days, omit=lambda sid, d: sid == "RBRTE")
    assert result.status == "quarantined"


def test_short_holiday_window_is_allowed(tmp_path: Path) -> None:
    window = [FetchRequest(("RBRTE", "RWTC"), date(2024, 12, 23), date(2024, 12, 27))]
    christmas = {date(2024, 12, 25), date(2024, 12, 26)}  # UK closed both days
    result = ingest(tmp_path, window, omit=lambda sid, d: sid == "RBRTE" and d in christmas)
    assert result.status == "published"


def test_empty_window_is_no_new_data_not_incomplete(tmp_path: Path) -> None:
    ingest(tmp_path, JAN)
    weekend = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 3), date(2024, 2, 4))]
    result = ingest(tmp_path, weekend)
    assert result.status == "no_new_data" and result.quality["incomplete"] == []


def test_discontinued_series_are_not_expected(tmp_path: Path) -> None:
    futures = [
        FetchRequest(("RCLC1", "RCLC2", "RCLC3", "RCLC4"), date(2024, 3, 1), date(2024, 6, 30))
    ]
    assert ingest(tmp_path, futures).status == "published"


def test_completeness_is_judged_within_each_request(tmp_path: Path) -> None:
    """Spot and futures are separate requests with different calendars; only rows within the
    same request count as evidence."""
    lo, hi = date(2024, 3, 1), date(2024, 3, 31)
    requests = [FetchRequest(("RWTC",), lo, hi), FetchRequest(("RCLC1",), lo, hi)]
    assert ingest(tmp_path, requests, omit=lambda sid, d: sid == "RCLC1").status == "published"
