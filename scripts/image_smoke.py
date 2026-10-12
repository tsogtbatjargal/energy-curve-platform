# ruff: noqa: S101  (a smoke test: its assertions are the checks)
"""Smoke test run inside the built batch image (CI, no network):

    docker run --rm -i --network none --entrypoint python ecp-batch:ci - < scripts/image_smoke.py

It goes through the image's own entry points on a file store and checks two things the
entry-point smoke does not reach: the synthetic negative day (2020-04-20), and the publish repair
of a wrong index record at the pointer's version (#69). Fails with AssertionError.
"""

import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from energy_curves import cloud
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline import publish
from energy_curves.storage.artifacts import LocalArtifactStore

DAY = date(2020, 4, 20)
WTI, BRENT = Decimal("-8.88"), Decimal("22.22")


def run(store: LocalArtifactStore, env: dict[str, str], run_id: str, today: date) -> None:
    cloud.stage(store, run_id, SyntheticSource(), cloud.daily_requests(today))
    assert cloud.task_main(["task", run_id], env=env) == 0, f"{run_id}: the task did not publish"


def price_on(store: LocalArtifactStore, series: str) -> Decimal:
    gold = publish.load_published(store).current
    row = gold.filter((gold["series_id"] == series) & (gold["observation_date"] == DAY))
    assert row.height == 1, f"{series}: no price on {DAY.isoformat()}"
    return Decimal(row["price"][0])


def main() -> None:
    root = tempfile.mkdtemp(prefix="ecp-smoke-")
    env = {"ECP_SOURCE": "synthetic", "ECP_STORE_URL": f"file://{root}"}
    store = LocalArtifactStore(Path(root))

    run(store, env, "smoke-1", date(2020, 4, 24))  # the window reaches 2020-04-20
    assert (price_on(store, "RWTC"), price_on(store, "RBRTE")) == (WTI, BRENT), (
        "the 2020-04-20 values are not the synthetic ones"
    )
    first = publish.read_pointer(store)
    assert first is not None and first.dataset_version == 1

    # Version 1's record is wrong (stores written before ADR-0019 could leave one). The next
    # publish must repair it, not only a missing one; otherwise version 1 would read as "stale".
    stale = {"dataset_version": 1, "logical_input_id": "stale"}
    store.put(publish.version_key(1), json.dumps(stale).encode())
    run(store, env, "smoke-2", date(2020, 5, 8))  # another window: a second logical input

    second = publish.read_pointer(store)
    assert second is not None and second.dataset_version == 2
    record = json.loads(store.get(publish.version_key(1)))
    assert record == first.__dict__, "version 1's index record was not repaired"
    assert publish.published_logical_ids(store) == {
        first.logical_input_id: 1,
        second.logical_input_id: 2,
    }, "the index does not list both inputs"
    print(json.dumps({"smoke": "ok", "versions": 2}))


if __name__ == "__main__":
    main()
