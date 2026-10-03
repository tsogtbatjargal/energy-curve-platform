import math
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import polars as pl
import pytest

from energy_curves.curves.shape import (
    ShapeEstimationError,
    compare_params,
    estimate_shape,
    quantize_s,
)

WINDOW = (date(2014, 1, 1), date(2015, 12, 31))
SPOT = Decimal("50.00")


def true_s(position: int, month: int) -> float:
    return 0.005 * position + 0.002 * position * math.cos(2 * math.pi * month / 12)


def future_price(position: int, month: int) -> Decimal:
    raw = Decimal(repr(float(SPOT) * math.exp(true_s(position, month))))
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def observations(start: date = WINDOW[0], end: date = WINDOW[1]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            rows.append({"series_id": "RWTC", "observation_date": d, "price": SPOT})
            for k in range(1, 5):
                rows.append(
                    {
                        "series_id": f"RCLC{k}",
                        "observation_date": d,
                        "price": future_price(k, d.month),
                    }
                )
        d += timedelta(days=1)
    return rows


def frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows, schema={"series_id": pl.Utf8, "observation_date": pl.Date, "price": pl.Decimal(18, 6)}
    )


def expected_s(position: int, month: int) -> Decimal:
    return quantize_s(math.log(float(future_price(position, month)) / float(SPOT)))


def test_recovers_constructed_shape_exactly() -> None:
    result = estimate_shape(frame(observations()), WINDOW)
    assert result.params.height == 48
    for r in result.params.iter_rows(named=True):
        k = int(r["position"][1])
        assert r["s"] == expected_s(k, r["month"]), (r["position"], r["month"])
    assert result.excluded.is_empty()


def test_labels_and_order() -> None:
    params = estimate_shape(frame(observations()), WINDOW).params
    assert params["position"].unique(maintain_order=True).to_list() == ["C1", "C2", "C3", "C4"]
    assert params["month"].to_list()[:12] == list(range(1, 13))


def test_non_positive_rows_excluded_from_estimation_only() -> None:
    rows = observations()
    day = date(2014, 3, 12)
    rows = [
        {**r, "price": Decimal("-5.00")}
        if r["series_id"] == "RWTC" and r["observation_date"] == day
        else r
        for r in rows
    ]
    rows = [
        {**r, "price": Decimal("0")}
        if r["series_id"] == "RCLC2" and r["observation_date"] == date(2014, 3, 13)
        else r
        for r in rows
    ]
    obs = frame(rows)
    baseline = estimate_shape(frame(observations()), WINDOW)
    result = estimate_shape(obs, WINDOW)

    assert result.excluded.to_dicts() == [
        {"observation_date": day, "position": p, "reason": "non-positive spot"}
        for p in ("C1", "C2", "C3", "C4")
    ] + [{"observation_date": date(2014, 3, 13), "position": "C2", "reason": "non-positive future"}]
    march = {
        (r["position"]): r["n_obs"]
        for r in result.params.filter(pl.col("month") == 3).iter_rows(named=True)
    }
    base_march = {
        (r["position"]): r["n_obs"]
        for r in baseline.params.filter(pl.col("month") == 3).iter_rows(named=True)
    }
    assert march == {p: base_march[p] - (2 if p == "C2" else 1) for p in base_march}
    assert obs.filter(pl.col("price") <= 0).height == 2  # input data itself untouched


def test_outside_window_ignored() -> None:
    inside = estimate_shape(frame(observations()), WINDOW)
    extra = observations(date(2016, 1, 1), date(2016, 1, 31))
    extra = [{**r, "price": Decimal("999.00")} for r in extra]
    both = estimate_shape(frame(observations() + extra), WINDOW)
    assert compare_params(inside.params, both.params) == []
    assert inside.input_sha256 == both.input_sha256


def test_missing_groups_rejected() -> None:
    with pytest.raises(ShapeEstimationError, match="no observations"):
        estimate_shape(frame(observations(date(2014, 1, 1), date(2014, 1, 31))), WINDOW)


def test_thin_groups_rejected() -> None:
    rows = [r for r in observations() if r["observation_date"].day <= 10]  # type: ignore[union-attr]
    with pytest.raises(ShapeEstimationError, match="fewer than 15"):
        estimate_shape(frame(rows), WINDOW)


def test_deterministic_and_order_independent() -> None:
    rows = observations()
    a = estimate_shape(frame(rows), WINDOW)
    b = estimate_shape(frame(list(reversed(rows))), WINDOW)
    assert a.params.equals(b.params)
    assert (a.params_sha256, a.input_sha256) == (b.params_sha256, b.input_sha256)


def test_quantize_rounds_half_even_and_rejects_non_finite() -> None:
    assert quantize_s(0.12345678905) in (Decimal("0.1234567890"), Decimal("0.1234567891"))
    assert quantize_s(1 / 3) == Decimal("0.3333333333")
    for bad in (math.nan, math.inf):
        with pytest.raises(ShapeEstimationError):
            quantize_s(bad)


# --- comparison used for golden and Polars/Spark parity checks ---


def params_frame(s_c1_jan: str = "0.0100000000", n: int = 20) -> pl.DataFrame:
    rows = [
        {"position": f"C{k}", "month": m, "s": Decimal("0.0100000000"), "n_obs": 20}
        for k in range(1, 5)
        for m in range(1, 13)
    ]
    rows[0] = {**rows[0], "s": Decimal(s_c1_jan), "n_obs": n}
    return pl.DataFrame(
        rows,
        schema={"position": pl.Utf8, "month": pl.Int8, "s": pl.Decimal(20, 10), "n_obs": pl.Int64},
    )


def test_compare_ignores_row_order() -> None:
    p = params_frame()
    assert compare_params(p, p.reverse()) == []


def test_compare_tolerance_boundary() -> None:
    assert compare_params(params_frame(), params_frame("0.0100000010")) == []  # 1e-9: within
    problems = compare_params(params_frame(), params_frame("0.0100000020"))  # 2e-9: outside
    assert problems and "C1/01" in problems[0]


def test_compare_counts_must_match_exactly() -> None:
    assert compare_params(params_frame(), params_frame(n=21)) == ["C1/01: n_obs 20 != 21"]


def test_compare_keys_must_match() -> None:
    assert compare_params(params_frame(), params_frame().head(47)) == ["parameter keys differ"]
