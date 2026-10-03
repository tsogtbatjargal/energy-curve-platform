from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from energy_curves.catalog import POSITIONS
from energy_curves.curves.engine import DISCLAIMER, build_curves, load_config, to_csv
from energy_curves.pipeline.medallion import PRICE

S = {"C1": "0.0100000000", "C2": "0.0200000000", "C3": "0.0300000000", "C4": "0.0400000000"}


def params() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"position": p, "month": m, "s": Decimal(v), "n_obs": 20}
            for p, v in S.items()
            for m in range(1, 13)
        ],
        schema={"position": pl.Utf8, "month": pl.Int8, "s": pl.Decimal(20, 10), "n_obs": pl.Int64},
    )


def gold(rows: list[tuple[str, date, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"series_id": s, "observation_date": d, "price": Decimal(p)} for s, d, p in rows],
        schema={"series_id": pl.Utf8, "observation_date": pl.Date, "price": PRICE},
    )


D1, D2 = date(2026, 9, 28), date(2026, 9, 29)


def build(rows: list[tuple[str, date, str]], **kw) -> pl.DataFrame:  # type: ignore[no-untyped-def]
    return build_curves(
        gold(rows), params(), params_sha256="abc", shape_method_version="shape-v1", **kw
    )


def point(curves: pl.DataFrame, curve: str, d: date, pos: str) -> dict[str, object]:
    return curves.filter(
        (pl.col("curve_id") == curve) & (pl.col("as_of_date") == d) & (pl.col("position") == pos)
    ).row(0, named=True)


def test_precision_against_independent_values() -> None:
    curves = build([("RWTC", D1, "70.00"), ("RBRTE", D1, "74.50")])
    # 70 * e^0.01 = 70.70351170..., 70 * e^0.04 = 72.85675...: 4 dp, half even
    assert point(curves, "WTI", D1, "C1")["price"] == Decimal("70.7035")
    assert point(curves, "WTI", D1, "C4")["price"] == Decimal("72.8568")
    assert point(curves, "WTI", D1, "Spot")["price"] == Decimal("70.0000")


def test_positions_labelled_spot_c1_to_c4_in_order() -> None:
    curves = build([("RWTC", D1, "70.00"), ("RBRTE", D1, "74.50")])
    for (_curve,), group in curves.group_by(["curve_id"]):
        assert group["position"].cast(pl.Utf8).to_list() == list(POSITIONS)


def test_spread_equals_displayed_long_minus_short_exactly() -> None:
    curves = build(
        [
            ("RWTC", D1, "70.13"),
            ("RBRTE", D1, "74.57"),
            ("RWTC", D2, "69.99"),
            ("RBRTE", D2, "75.01"),
        ]
    )
    for d in (D1, D2):
        for pos in POSITIONS:
            spread = point(curves, "BRENT_WTI", d, pos)["price"]
            brent = point(curves, "BRENT", d, pos)["price"]
            wti = point(curves, "WTI", d, pos)["price"]
            assert spread == brent - wti  # type: ignore[operator]


def test_brent_borrows_wti_shape_and_says_so() -> None:
    curves = build([("RWTC", D1, "70.00"), ("RBRTE", D1, "70.00")])
    assert point(curves, "BRENT", D1, "C3")["price"] == point(curves, "WTI", D1, "C3")["price"]
    assert "borrowed" in str(point(curves, "BRENT", D1, "C1")["shape_source"])


def test_missing_leg_is_a_gap_not_forward_filled() -> None:
    curves = build([("RWTC", D1, "70.00"), ("RBRTE", D1, "74.50"), ("RWTC", D2, "71.00")])
    brent = curves.filter((pl.col("curve_id") == "BRENT") & (pl.col("as_of_date") == D2))
    spread = curves.filter((pl.col("curve_id") == "BRENT_WTI") & (pl.col("as_of_date") == D2))
    assert brent["status"].unique().to_list() == ["gap"] and brent["price"].null_count() == 5
    assert brent["gap_reason"][0] == f"no RBRTE spot on {D2}"
    assert spread["status"].unique().to_list() == ["gap"]
    assert spread["gap_reason"][0] == f"no RBRTE spot on {D2}"
    assert point(curves, "WTI", D2, "C1")["status"] == "ok"


def test_non_positive_anchor_is_a_gap() -> None:
    curves = build([("RWTC", D1, "-37.63"), ("RBRTE", D1, "19.33")])
    assert point(curves, "WTI", D1, "C2")["status"] == "gap"
    assert "non-positive RWTC spot" in str(point(curves, "WTI", D1, "C2")["gap_reason"])
    assert point(curves, "BRENT", D1, "C2")["status"] == "ok"
    assert point(curves, "BRENT_WTI", D1, "C2")["status"] == "gap"


def test_every_point_labelled_as_modelled_estimate() -> None:
    curves = build([("RWTC", D1, "70.00"), ("RBRTE", D1, "74.50"), ("RWTC", D2, "71.00")])
    assert curves["estimate_type"].unique().to_list() == ["modelled"]
    assert curves["params_sha256"].unique().to_list() == ["abc"]
    assert curves["method_version"].unique().to_list() == ["curve-v1"]
    assert to_csv(curves).splitlines()[0] == f"# {DISCLAIMER}"


def test_only_dates_with_an_anchor_are_built() -> None:
    curves = build([("RWTC", D1, "70.00")])
    assert curves["as_of_date"].unique().to_list() == [D1]


def test_config_driven() -> None:
    config = load_config(
        'method_version = "x"\n[[outright]]\nid = "WTI"\nanchor = "RWTC"\nshape_source = "test"\n'
    )
    curves = build([("RWTC", D1, "70.00"), ("RBRTE", D1, "74.50")], config=config)
    assert curves["curve_id"].unique().to_list() == ["WTI"]
    assert curves.height == 5


@pytest.mark.parametrize("spot", ["70.00005", "70.00015"])
def test_spot_point_rounds_half_even(spot: str) -> None:
    curves = build([("RWTC", D1, spot)])
    expected = Decimal(spot).quantize(Decimal("0.0001"))  # default context rounding: half even
    assert point(curves, "WTI", D1, "Spot")["price"] == expected
