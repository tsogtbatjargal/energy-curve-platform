"""The synthetic price function is pinned before it moves (M5a): its output must not change.

The digest was computed on the generator in `ingestion/synthetic.py` before it was extracted into
`synthetic_prices.py`. It covers every series over a 41-year grid (weekly steps, so weekdays and
weekends alike) and the edge days: the negative WTI settlement, the last futures date and after.
"""

import hashlib
from datetime import date, timedelta

from energy_curves.ingestion.synthetic import synthetic_price

SERIES = ("RWTC", "RBRTE", "RCLC1", "RCLC2", "RCLC3", "RCLC4")
EDGE_DAYS = [
    date(2020, 4, 17), date(2020, 4, 18), date(2020, 4, 20), date(2020, 4, 21),
    date(2024, 4, 4), date(2024, 4, 5), date(2024, 4, 8), date(2024, 12, 31),
]  # fmt: skip
GRID_SHA256 = "59ce40312fa102dc5b5d7e0ffdfd5af6dddf930688b6b3537550fbbee9a46b65"


def test_the_price_function_is_unchanged_over_the_grid() -> None:
    days = [date(1983, 1, 3) + timedelta(days=i) for i in range(0, 15000, 7)] + EDGE_DAYS
    digest = hashlib.sha256()
    priced = 0
    for d in days:
        for sid in SERIES:
            value = synthetic_price(sid, d)
            digest.update(f"{sid}|{d}|{value}\n".encode())
            priced += value is not None
    assert priced == 12892
    assert digest.hexdigest() == GRID_SHA256
