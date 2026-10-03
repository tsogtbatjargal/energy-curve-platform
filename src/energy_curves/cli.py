"""energy-curves command line.

  db-migrate        apply numbered SQL migrations (locked, checksummed, transactional)
  db-import         import published versions into Postgres (--rebuild: re-import market)
  db-status         migration version, dataset version, outbox and attempt counts
  db-backup DIR     back up the Postgres-owned app schema; db-restore DIR restores it
  process-events    evaluate pending dataset_imported events for alerts (db-import runs it too)
  alerts-prune      delete fired alerts older than --keep-days (raises the alert log's floor)
  serve             local API, CSV export and live events on 127.0.0.1 (--port, default 8000)

ingest            fetch -> Bronze/Silver/Gold (+ curves when shape parameters exist) -> publish
estimate-shape    estimate s[k, m] from the published Gold history
verify-shape      re-estimate and compare with the stored parameters (tolerance, not bytes)
export-curves     write one as-of date's curves to CSV with the modelled-estimate disclaimer
status            show the published dataset and its quality summary
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

import polars as pl

from energy_curves.catalog import SPOT_SERIES, WTI_FUTURES
from energy_curves.config import Settings
from energy_curves.curves import engine, shape
from energy_curves.ingestion.eia import EiaClient
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.logging_setup import configure_logging
from energy_curves.pipeline.publish import dumps, load_published, parquet_bytes, read_artifact
from energy_curves.pipeline.runner import FetchRequest, ShapeInput, Source, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

SHAPE_DIR = "shape"


def _source(name: str, settings: Settings) -> Source:
    if name == "synthetic":
        return SyntheticSource()
    key = settings.eia_api_key.get_secret_value() if settings.eia_api_key else ""
    return EiaClient(key, settings.eia_base_url)


PACKAGED_PARAMS = "synthetic-shape-v1"


def packaged_params_dir() -> Path:
    return Path(str(resources.files("energy_curves.curves").joinpath("params")))


def load_shape(data_dir: Path, source: str) -> ShapeInput | None:
    """Local parameters if estimated; otherwise the packaged synthetic set for synthetic runs.

    Real-derived parameters exist only in the local data directory (ADR-0011). The repository
    ships parameters estimated from the synthetic source.
    """
    store = LocalArtifactStore(data_dir)
    if store.exists(f"{SHAPE_DIR}/meta.json"):
        meta = json.loads(store.get(f"{SHAPE_DIR}/meta.json"))
        params = pl.read_parquet(store.path(f"{SHAPE_DIR}/params.parquet"))
    elif source == "synthetic":
        base = packaged_params_dir()
        meta = json.loads((base / f"{PACKAGED_PARAMS}.json").read_text())
        params = shape.params_from_csv((base / f"{PACKAGED_PARAMS}.csv").read_text())
    else:
        return None
    if meta["params_sha256"] != shape.params_sha256(params):
        raise SystemExit("shape parameters do not match their recorded hash")
    return ShapeInput(params, meta["params_sha256"], meta["method_version"], meta["origin"])


def synthetic_params(work_dir: Path) -> tuple[shape.ShapeResult, dict[str, object]]:
    """Estimate shape parameters from the synthetic source only."""
    lo, hi = shape.WINDOW
    run_ingest(
        work_dir,
        SyntheticSource(),
        [FetchRequest(SPOT_SERIES[:1], lo, hi), FetchRequest(WTI_FUTURES, lo, hi)],
        source_name="synthetic",
    )
    result = shape.estimate_shape(load_published(LocalArtifactStore(work_dir)).current)
    meta = {
        "origin": "synthetic",
        "generated_by": "energy-curves write-synthetic-params",
        "method_version": result.method_version,
        "window": [d.isoformat() for d in result.window],
        "input_sha256": result.input_sha256,
        "params_sha256": result.params_sha256,
    }
    return result, meta


def cmd_write_synthetic_params(args: argparse.Namespace, settings: Settings) -> int:
    out = Path(args.out_dir) if args.out_dir else packaged_params_dir()
    with tempfile.TemporaryDirectory() as tmp:
        result, meta = synthetic_params(Path(tmp))
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{PACKAGED_PARAMS}.csv").write_text(shape.params_to_csv(result.params))
    (out / f"{PACKAGED_PARAMS}.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta, indent=2))
    return 0


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    series = list(SPOT_SERIES) + (list(WTI_FUTURES) if args.with_futures else [])
    requests = [
        FetchRequest(tuple(s for s in series if s in group), args.start, args.end)
        for group in (SPOT_SERIES, WTI_FUTURES)
        if any(s in group for s in series)
    ]
    result = run_ingest(
        settings.data_dir,
        _source(args.source, settings),
        requests,
        source_name=args.source,
        shape=load_shape(settings.data_dir, args.source),
    )
    print(json.dumps(result.__dict__, indent=2, default=str))
    return 0 if result.status != "quarantined" else 1


def cmd_estimate_shape(args: argparse.Namespace, settings: Settings) -> int:
    store = LocalArtifactStore(settings.data_dir)
    published = load_published(store)
    result = shape.estimate_shape(published.current)  # refuses mixed sources
    origin = published.current["source"].unique().to_list()
    store.put(f"{SHAPE_DIR}/params.parquet", parquet_bytes(result.params))
    store.put(f"{SHAPE_DIR}/excluded.parquet", parquet_bytes(result.excluded))
    meta = {
        "method_version": result.method_version,
        "window": [d.isoformat() for d in result.window],
        "input_sha256": result.input_sha256,
        "params_sha256": result.params_sha256,
        "excluded_rows": result.excluded.height,
        "source_dataset_version": published.version,
        "origin": origin[0] if len(origin) == 1 else "mixed",
    }
    store.put(f"{SHAPE_DIR}/meta.json", dumps(meta))
    print(json.dumps(meta, indent=2))
    return 0


def cmd_verify_shape(args: argparse.Namespace, settings: Settings) -> int:
    stored = load_shape(settings.data_dir, "local-only")
    if stored is None:
        print("no stored shape parameters", file=sys.stderr)
        return 2
    fresh = shape.estimate_shape(load_published(LocalArtifactStore(settings.data_dir)).current)
    problems = shape.compare_params(stored.params, fresh.params)
    print("\n".join(problems) or f"shape parameters reproduce within {shape.PARITY_TOLERANCE}")
    return 1 if problems else 0


def cmd_export_curves(args: argparse.Namespace, settings: Settings) -> int:
    store = LocalArtifactStore(settings.data_dir)
    published = load_published(store)
    if published.manifest is None or "gold_curves" not in published.manifest["artifacts"]:
        print("published dataset has no curves; run estimate-shape then ingest", file=sys.stderr)
        return 2
    curves = read_artifact(store, published.manifest, "gold_curves")
    as_of = args.as_of or curves["as_of_date"].max()
    text = engine.to_csv(curves.filter(pl.col("as_of_date") == as_of))
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text, end="")
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    published = load_published(LocalArtifactStore(settings.data_dir))
    if published.manifest is None:
        print("nothing published yet")
        return 0
    m = published.manifest
    print(
        json.dumps(
            {
                "dataset_version": m["dataset_version"],
                "logical_input_id": m["logical_input_id"],
                "quality": m["quality"],
                "merge": m["merge"],
                "artifacts": sorted(m["artifacts"]),
            },
            indent=2,
        )
    )
    return 0


def cmd_db_migrate(args: argparse.Namespace, settings: Settings) -> int:
    from energy_curves.db.migrate import migrate

    print(json.dumps({"applied": migrate(settings.database_url)}))
    return 0


def cmd_db_import(args: argparse.Namespace, settings: Settings) -> int:
    import redis

    from energy_curves.api.events import publish_dataset_updated
    from energy_curves.db import importer

    client = redis.Redis.from_url(
        settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
    )
    notified: list[int] = []

    def notify(version: int) -> None:  # after commit; best effort (ADR-0014)
        if publish_dataset_updated(client, version):
            notified.append(version)

    run = importer.rebuild_market if args.rebuild else importer.import_pending
    results, attempts = run(settings.database_url, settings.data_dir, notify)
    # Evaluate alerts right away; a failure leaves the events pending for the next pass.
    events = _process_events(settings, client)
    print(
        json.dumps(
            {
                "versions": [r.__dict__ for r in results],
                "attempts_synced": attempts,
                "notified": notified,
                "events": events,
            }
        )
    )
    return 0


def _process_events(settings: Settings, client: Any) -> dict[str, int]:
    from energy_curves.api.events import publish_alerts_wakeup
    from energy_curves.db.alerts import process_events

    stats = process_events(settings.database_url, lambda: publish_alerts_wakeup(client))
    return {"done": stats.done, "retried": stats.retried, "dead": stats.dead}


def cmd_process_events(args: argparse.Namespace, settings: Settings) -> int:
    import redis

    client = redis.Redis.from_url(
        settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
    )
    print(json.dumps(_process_events(settings, client)))
    return 0


def cmd_alerts_prune(args: argparse.Namespace, settings: Settings) -> int:
    from datetime import UTC, datetime, timedelta

    import psycopg

    from energy_curves.db.alerts import prune

    before = datetime.now(UTC) - timedelta(days=args.keep_days)
    with psycopg.connect(settings.database_url) as conn:
        print(json.dumps({"pruned": prune(conn, before)}))
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from energy_curves.api.app import create_app
    from energy_curves.api.cache import VersionCache

    app = create_app(
        database_url=settings.database_url,
        cache=VersionCache.from_url(settings.redis_url),
        redis_url=settings.redis_url,
        port=args.port,
    )
    # Loopback only, by design (ADR-0013): there is no --host option.
    uvicorn.run(app, host="127.0.0.1", port=args.port, server_header=False, proxy_headers=False)
    return 0


def cmd_db_status(args: argparse.Namespace, settings: Settings) -> int:
    import psycopg

    from energy_curves.db.migrate import current_version
    from energy_curves.db.outbox import counts

    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute("SELECT max(dataset_version) FROM market.dataset_versions").fetchone()
        attempts = conn.execute(
            "SELECT status, count(*) FROM market.pipeline_attempts GROUP BY status"
        ).fetchall()
        doc = {
            "schema_version": current_version(conn),
            "dataset_version": row[0] if row else None,
            "outbox": counts(conn),
            "attempts": {status: n for status, n in attempts},
        }
    print(json.dumps(doc, indent=2))
    return 0


def cmd_db_backup(args: argparse.Namespace, settings: Settings) -> int:
    from energy_curves.db.backup import backup

    manifest = backup(settings.database_url, Path(args.target))
    print(json.dumps({t: m["rows"] for t, m in manifest["tables"].items()}))
    return 0


def cmd_db_restore(args: argparse.Namespace, settings: Settings) -> int:
    from energy_curves.db.backup import restore

    print(json.dumps(restore(settings.database_url, Path(args.source))))
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    key = settings.eia_api_key.get_secret_value() if settings.eia_api_key else ""
    configure_logging(settings.log_level, secrets=[key])
    parser = argparse.ArgumentParser(prog="energy-curves")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--source", choices=["synthetic", "eia"], default="synthetic")
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--with-futures", action="store_true")
    p.set_defaults(func=cmd_ingest)
    sub.add_parser("estimate-shape").set_defaults(func=cmd_estimate_shape)
    sub.add_parser("verify-shape").set_defaults(func=cmd_verify_shape)
    p = sub.add_parser("export-curves")
    p.add_argument("--as-of", type=date.fromisoformat)
    p.add_argument("--out")
    p.set_defaults(func=cmd_export_curves)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("db-migrate").set_defaults(func=cmd_db_migrate)
    p = sub.add_parser("db-import", help="import published versions into Postgres")
    p.add_argument(
        "--rebuild",
        action="store_true",
        help="re-import market from the published versions, all or nothing",
    )
    p.set_defaults(func=cmd_db_import)
    sub.add_parser("db-status").set_defaults(func=cmd_db_status)
    sub.add_parser(
        "process-events", help="evaluate pending dataset_imported events (alerts)"
    ).set_defaults(func=cmd_process_events)
    p = sub.add_parser("alerts-prune", help="delete fired alerts older than --keep-days")
    p.add_argument("--keep-days", type=int, required=True)
    p.set_defaults(func=cmd_alerts_prune)
    p = sub.add_parser("serve", help="serve the local API on 127.0.0.1")
    p.add_argument("--port", type=int, default=settings.api_port)
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("db-backup", help="back up the app schema (rules, alerts, outbox)")
    p.add_argument("target")
    p.set_defaults(func=cmd_db_backup)
    p = sub.add_parser("db-restore", help="restore the app schema from a db-backup directory")
    p.add_argument("source")
    p.set_defaults(func=cmd_db_restore)
    p = sub.add_parser("write-synthetic-params")
    p.add_argument("--out-dir", help="default: the packaged parameter directory")
    p.set_defaults(func=cmd_write_synthetic_params)
    args = parser.parse_args(argv)
    return int(args.func(args, settings))
