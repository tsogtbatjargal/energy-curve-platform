"""energy-curves command line.

ingest            fetch -> Bronze/Silver/Gold (+ curves when shape parameters exist) -> publish
estimate-shape    estimate s[k, m] from the published Gold history
verify-shape      re-estimate and compare with the stored parameters (tolerance, not bytes)
export-curves     write one as-of date's curves to CSV with the modelled-estimate disclaimer
status            show the published dataset and its quality summary
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import date
from importlib import resources
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
    p = sub.add_parser("write-synthetic-params")
    p.add_argument("--out-dir", help="default: the packaged parameter directory")
    p.set_defaults(func=cmd_write_synthetic_params)
    args = parser.parse_args(argv)
    return int(args.func(args, Settings()))
