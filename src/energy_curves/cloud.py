"""AWS entry points for the daily batch (M4, ADR-0020).

Step Functions runs two steps with the same `run_id` (the execution name):
1. `lambda_handler` (Lambda): fetch the day's pages and stage them, unchanged, under
   `staging/<run_id>/`. `requests.json` is written last and marks the stage complete.
2. `task_main` (ECS Fargate): replay the staged pages through `StagedSource` and run the pipeline
   on S3 with `run_ingest_store`. The replay carries each page's body and retrieval time, so the
   logical input is exactly what a single local run of the same fetch would compute.

Synthetic data only, until real-data cloud use is decided (PLAN.md R3, ADR-0011): the only source
these entry points construct is `SyntheticSource`, `ECP_SOURCE` must be "synthetic", and no API
key is read.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from energy_curves.catalog import SPOT_SERIES
from energy_curves.ingestion.eia import Page
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import dumps
from energy_curves.pipeline.runner import FetchRequest, Source, run_ingest_store
from energy_curves.storage.artifacts import (
    ArtifactStore,
    LocalArtifactStore,
    S3ArtifactStore,
    sha256,
)

log = logging.getLogger(__name__)
SOURCE = "synthetic"  # the only source the cloud runtime accepts (ADR-0020)
LOOKBACK_DAYS = 10  # re-fetch the recent window: late publications and revisions (ADR-0012)
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
EXIT_QUARANTINED = 3  # a quarantined batch fails the execution, so the failure alarm fires


class RefusedSource(RuntimeError):
    """The cloud runtime was asked for a source other than synthetic data."""


class StagingError(RuntimeError):
    """The staged pages are missing, incomplete or do not match their recorded hashes."""


def require_synthetic(env: dict[str, str]) -> None:
    source = env.get("ECP_SOURCE", SOURCE)
    if source != SOURCE:
        raise RefusedSource(
            f"ECP_SOURCE={source!r}: the cloud runtime runs synthetic data only (PLAN.md R3)"
        )


def store_from_url(url: str) -> ArtifactStore:
    """`s3://bucket/prefix` in AWS; `file:///path` for local smoke tests of the image."""
    parsed = urlparse(url)
    if parsed.scheme == "s3" and parsed.netloc:
        import boto3  # type: ignore[import-untyped]

        return S3ArtifactStore(boto3.client("s3"), parsed.netloc, parsed.path.strip("/"))
    if parsed.scheme == "file" and parsed.path:
        return LocalArtifactStore(Path(parsed.path))
    raise ValueError(f"unsupported store URL: {url!r}")


def checked_run_id(run_id: object) -> str:
    if not isinstance(run_id, str) or not RUN_ID.match(run_id):
        raise ValueError("run_id must be 1-80 letters, digits, '-' or '_'")
    return run_id


def daily_requests(today: date) -> list[FetchRequest]:
    """The spot series over the recent window. EIA futures ended on 2024-04-05 (ADR-0012)."""
    return [FetchRequest(tuple(sorted(SPOT_SERIES)), today - timedelta(days=LOOKBACK_DAYS), today)]


def _request_doc(r: FetchRequest) -> dict[str, Any]:
    return {"series_ids": sorted(r.series_ids), "start": r.start.isoformat(),
            "end": r.end.isoformat()}  # fmt: skip


def stage(
    store: ArtifactStore, run_id: str, source: Source, requests: list[FetchRequest]
) -> dict[str, Any]:
    """Write every fetched page under staging/<run_id>/, then the index that completes it."""
    prefix = f"staging/{checked_run_id(run_id)}"
    index: list[dict[str, Any]] = []
    n = 0
    for req in requests:
        pages = []
        for page in source.fetch(req.route, list(req.series_ids), req.start, req.end):
            n += 1
            key = f"{prefix}/page-{n:04d}.json"
            pages.append({"key": key, "sha256": store.put(key, page.body),
                          "params": page.params,
                          "retrieved_at": page.retrieved_at.isoformat()})  # fmt: skip
        index.append({"request": _request_doc(req), "pages": pages})
    store.put(f"{prefix}/requests.json", dumps({"run_id": run_id, "requests": index}))
    return {"run_id": run_id, "pages": n}


class StagedSource:
    """Replays the pages a `stage` call wrote, for exactly the requests it fetched."""

    def __init__(self, store: ArtifactStore, run_id: str) -> None:
        self.store = store
        key = f"staging/{checked_run_id(run_id)}/requests.json"
        if not store.exists(key):
            raise StagingError(f"{key} is missing: the staging step did not complete")
        self.index = json.loads(store.get(key))["requests"]

    def requests(self) -> list[FetchRequest]:
        return [
            FetchRequest(tuple(e["request"]["series_ids"]),
                         date.fromisoformat(e["request"]["start"]),
                         date.fromisoformat(e["request"]["end"]))
            for e in self.index
        ]  # fmt: skip

    def fetch(self, route: str, series_ids: list[str], start: date, end: date) -> Iterator[Page]:
        wanted = {"series_ids": sorted(series_ids), "start": start.isoformat(),
                  "end": end.isoformat()}  # fmt: skip
        entry = next((e for e in self.index if e["request"] == wanted), None)
        if entry is None:
            raise StagingError(f"no staged pages for {wanted}")
        for meta in entry["pages"]:
            body = self.store.get(meta["key"])
            if sha256(body) != meta["sha256"]:
                raise StagingError(f"{meta['key']} does not match its staged hash")
            response = json.loads(body)["response"]
            yield Page(body=body, params=meta["params"],
                       retrieved_at=datetime.fromisoformat(meta["retrieved_at"]),
                       rows=response["data"], total=int(response["total"]))  # fmt: skip


def lambda_handler(
    event: dict[str, Any],
    context: object = None,
    *,
    env: dict[str, str] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Step 1. `event` = {"run_id": <execution name>}; optional "today" (YYYY-MM-DD) for reruns."""
    env = dict(os.environ) if env is None else env
    require_synthetic(env)
    now = clock()
    today = date.fromisoformat(event["today"]) if event.get("today") else now.date()
    store = store_from_url(env["ECP_STORE_URL"])
    result = stage(store, event["run_id"], SyntheticSource(retrieved_at=now), daily_requests(today))
    log.info("staged", extra=result)
    return result


def task_main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    """Step 2: `python -m energy_curves.cloud task <run_id>`. Prints the run result as JSON."""
    from energy_curves.cli import packaged_synthetic_shape  # the packaged, synthetic-only set

    args = sys.argv[1:] if argv is None else argv
    env = dict(os.environ) if env is None else env
    require_synthetic(env)
    if len(args) != 2 or args[0] != "task":
        print("usage: python -m energy_curves.cloud task <run_id>", file=sys.stderr)
        return 2
    store = store_from_url(env["ECP_STORE_URL"])
    staged = StagedSource(store, args[1])
    result = run_ingest_store(store, staged, staged.requests(), source_name=SOURCE,
                              shape=packaged_synthetic_shape())  # fmt: skip
    print(json.dumps({**result.__dict__, "run_id": args[1]}, default=str))
    return EXIT_QUARANTINED if result.status == "quarantined" else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(task_main())
