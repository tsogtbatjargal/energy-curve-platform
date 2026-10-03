from decimal import Decimal

import pytest

from energy_curves.pipeline.parsing import Rejected, parse_price, parse_row


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("61.25", "61.25"),
        ("-37.63", "-37.63"),  # real WTI settlement on 2020-04-20; valid, kept
        ("0", "0"),
        ("100", "100"),
        ("70.123456", "70.123456"),
        (71, "71"),
    ],
)
def test_valid_numeric_strings_parse(raw: object, expected: str) -> None:
    assert parse_price(raw) == Decimal(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "NA",
        "--",
        "1,234.50",
        "NaN",
        "Infinity",
        "-Infinity",
        "1e3",
        " 61.25",
        "61.25 ",
        "+5",
        ".5",
        "5.",
        "01.5",
        "61.25.1",
        "0x1A",
    ],
)
def test_malformed_strings_rejected(raw: str) -> None:
    with pytest.raises(Rejected, match="malformed numeric string"):
        parse_price(raw)


@pytest.mark.parametrize("raw", [None, True, 61.25, [1], {"v": 1}])
def test_non_string_values_rejected(raw: object) -> None:
    with pytest.raises(Rejected):
        parse_price(raw)


def test_excess_precision_rejected() -> None:
    with pytest.raises(Rejected, match="decimal places"):
        parse_price("1.1234567")


def row(**overrides: object) -> dict[str, object]:
    base = {"period": "2026-09-25", "series": "RWTC", "value": "61.25", "units": "$/BBL"}
    return {**base, **overrides}


def test_row_parses() -> None:
    obs = parse_row(row())
    assert (obs.series_id, str(obs.observation_date), obs.price) == (
        "RWTC",
        "2026-09-25",
        Decimal("61.25"),
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"series": "XXXX"}, "unknown series"),
        ({"units": "$/GAL"}, "unit"),
        ({"period": "2026-13-01"}, "unparseable period"),
        ({"period": "2026-09"}, "unparseable period"),
        ({"period": "20260925"}, "not a daily date"),
        ({"value": "n/a"}, "malformed"),
    ],
)
def test_row_rejections(overrides: dict[str, object], reason: str) -> None:
    with pytest.raises(Rejected, match=reason):
        parse_row(row(**overrides))
