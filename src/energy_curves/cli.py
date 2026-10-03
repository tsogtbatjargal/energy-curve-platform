"""energy-curves command line.

ingest            fetch -> Bronze/Silver/Gold (+ curves when shape parameters exist) -> publish
estimate-shape    estimate s[k, m] from the published Gold history
verify-shape      re-estimate and compare with the stored parameters (tolerance, not bytes)
export-curves     write one as-of date's curves to CSV with the modelled-estimate disclaimer
status            show the published dataset and its quality summary
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import date
from pathlib import Path

import polars as pl

from energy_curves.catalog import SPOT_SERIES, WTI_FUTURES
from energy_curves.config import Settings
from energy_curves.curves import engine, shape
from energy_curves.ingestion.eia import EiaClient
from energy_curves.ingestion.synthetic import SyntheticSource
from energy_curves.pipeline.publish import dumps, load_published, parquet_bytes, read_artifact
from energy_curves.pipeline.runner import FetchRequest, ShapeInput, Source, run_ingest
from energy_curves.storage.artifacts import LocalArtifactStore

SHAPE_DIR = "shape"


def _source(name: str, settings: Settings) -> Source:
    if name == "synthetic":
        return SyntheticSource()
    key = settings.eia_api_key.get_secret_value() if settings.eia_api_key else ""
    return EiaClient(key, settings.eia_base_url)


def load_shape(data_dir: Path) -> ShapeInput | None:
    store = LocalArtifactStore(data_dir)
    if not store.exists(f"{SHAPE_DIR}/meta.json"):
        return None
    meta = json.loads(store.get(f"{SHAPE_DIR}/meta.json"))
    params = pl.read_parquet(store.path(f"{SHAPE_DIR}/params.parquet"))
    if meta["params_sha256"] != _params_sha(params):
        raise SystemExit("shape parameters do not match their recorded hash")
    return ShapeInput(params, meta["params_sha256"], meta["method_version"])


def _params_sha(params: pl.DataFrame) -> str:
    return hashlib.sha256(shape.canonical_params_text(params).encode()).hexdigest()


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
        shape=load_shape(settings.data_dir),
    )
    print(json.dumps(result.__dict__, indent=2, default=str))
    return 0 if result.status != "quarantined" else 1


def cmd_estimate_shape(args: argparse.Namespace, settings: Settings) -> int:
    store = LocalArtifactStore(settings.data_dir)
    published = load_published(store)
    result = shape.estimate_shape(published.current)
    store.put(f"{SHAPE_DIR}/params.parquet", parquet_bytes(result.params))
    store.put(f"{SHAPE_DIR}/excluded.parquet", parquet_bytes(result.excluded))
    meta = {
        "method_version": result.method_version,
        "window": [d.isoformat() for d in result.window],
        "input_sha256": result.input_sha256,
        "params_sha256": result.params_sha256,
        "excluded_rows": result.excluded.height,
        "source_dataset_version": published.version,
        "sources": sorted(set(published.current["source"].to_list())),
    }
    store.put(f"{SHAPE_DIR}/meta.json", dumps(meta))
    print(json.dumps(meta, indent=2))
    return 0


def cmd_verify_shape(args: argparse.Namespace, settings: Settings) -> int:
    stored = load_shape(settings.data_dir)
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
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
    args = parser.parse_args(argv)
    return int(args.func(args, Settings()))
