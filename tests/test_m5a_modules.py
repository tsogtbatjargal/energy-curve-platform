"""M5a (ADR-0023): what the Glue job may import, and one definition of the shared code.

The Glue job (ADR-0007) imports only PySpark and the standard library, so the price generator and
the shape contract it shares with the Polars side live in two standard-library-only modules.
"""

import ast
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "energy_curves"
STDLIB = set(sys.stdlib_module_names) | {"__future__"}
SHARED = {"energy_curves.catalog"}
MODULES = {
    "src/energy_curves/synthetic_prices.py": SHARED,
    "src/energy_curves/curves/shape_core.py": SHARED,
    "jobs/glue/shape_job.py": SHARED
    | {"energy_curves.synthetic_prices", "energy_curves.curves.shape_core", "pyspark"},
}


def imported(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{a.name}" for a in node.names}
    return names


@pytest.mark.parametrize("relative", sorted(MODULES))
def test_the_module_imports_only_the_standard_library_and_what_it_is_allowed(
    relative: str,
) -> None:
    allowed = MODULES[relative]
    outside = {
        n
        for n in imported(ROOT / relative)
        if n.split(".")[0] not in STDLIB
        and not any(n == a or n.startswith(a + ".") or a.startswith(n + ".") for a in allowed)
    }
    assert outside == set(), outside


def test_no_module_the_job_needs_imports_polars_or_the_http_client() -> None:
    for relative in MODULES:
        names = imported(ROOT / relative)
        assert not {n for n in names if n.split(".")[0] in {"polars", "httpx", "boto3"}}
        assert "energy_curves.ingestion.eia" not in names


def test_the_generator_has_one_definition() -> None:
    from energy_curves import synthetic_prices
    from energy_curves.ingestion import synthetic

    assert synthetic.synthetic_price is synthetic_prices.synthetic_price
    assert synthetic_prices.synthetic_price.__module__ == "energy_curves.synthetic_prices"
    assert synthetic.NEGATIVE_DAY == synthetic_prices.NEGATIVE_DAY == date(2020, 4, 20)
    assert synthetic.FUTURES_END == synthetic_prices.FUTURES_END == date(2024, 4, 5)


def test_the_shape_contract_has_one_definition() -> None:
    from energy_curves.curves import shape, shape_core

    assert shape.quantize_s is shape_core.quantize_s
    assert shape.ShapeEstimationError is shape_core.ShapeEstimationError
    assert shape.PARITY_TOLERANCE == shape_core.PARITY_TOLERANCE == Decimal("1e-9")
    assert shape.WINDOW == shape_core.WINDOW == (date(2014, 1, 1), date(2024, 4, 5))
    assert shape.MIN_OBS == shape_core.MIN_OBS == 15
    assert shape.METHOD_VERSION == shape_core.METHOD_VERSION == "shape-v1"


def test_the_history_covers_the_shape_series_and_stops_futures_at_the_last_date() -> None:
    from energy_curves.synthetic_prices import FUTURES_END, SHAPE_SERIES, history

    rows = list(history(SHAPE_SERIES, date(2024, 4, 3), date(2024, 4, 9)))
    assert {r[0] for r in rows} == set(SHAPE_SERIES)
    assert all(r[1].weekday() < 5 for r in rows)
    futures_days = {r[1] for r in rows if r[0].startswith("RCLC")}
    assert max(futures_days) == FUTURES_END
    assert max(r[1] for r in rows if r[0] == "RWTC") == date(2024, 4, 9)
    assert all(isinstance(r[2], Decimal) for r in rows)


def test_history_is_a_pure_function_of_the_arguments() -> None:
    from energy_curves.synthetic_prices import SHAPE_SERIES, history

    a = list(history(SHAPE_SERIES, date(2019, 1, 1), date(2019, 3, 31)))
    assert a == list(history(SHAPE_SERIES, date(2019, 1, 1), date(2019, 3, 31)))
    assert a == sorted(a, key=lambda r: (r[1], r[0]))


def test_the_canonical_text_is_sorted_and_its_hash_is_stable() -> None:
    from energy_curves.curves.shape_core import canonical_text, text_sha256

    rows = [("C2", 1, Decimal("0.0081234567"), 21), ("C1", 12, Decimal("0.0042"), 20)]
    text = canonical_text(rows)
    assert text == "C1,12,0.0042,20\nC2,1,0.0081234567,21\n"
    assert text_sha256(text) == text_sha256(canonical_text(reversed(rows)))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_quantize_s_refuses_a_non_finite_value(bad: float) -> None:
    from energy_curves.curves.shape_core import ShapeEstimationError, quantize_s

    with pytest.raises(ShapeEstimationError):
        quantize_s(bad)


def test_quantize_s_rounds_half_even_from_the_shortest_repr() -> None:
    from energy_curves.curves.shape_core import quantize_s

    assert quantize_s(0.00123456789049) == Decimal("0.0012345679")
    assert quantize_s(0.5e-10) == Decimal("0E-10")  # exactly half rounds to the even digit
    assert quantize_s(1.5e-10) == Decimal("2E-10")
