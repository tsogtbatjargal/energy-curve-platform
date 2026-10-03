"""Manifests and the published-dataset pointer.

Order of operations for a run: write every artifact, then the run manifest, then publish by
atomically replacing `published/current.json`. A crash anywhere before the replace leaves the
previous dataset as the published one. Publication requires the manifest's base version to equal
the current version (an optimistic check that S3 conditional writes enforce in M4), so an older
run can never move the dataset backward.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import polars as pl

from energy_curves.pipeline.medallion import REVISION_SCHEMA, SILVER_SCHEMA, empty
from energy_curves.storage.artifacts import ArtifactStore, sha256

POINTER_KEY = "published/current.json"
MANIFEST_SCHEMA_VERSION = 1


class PublishConflict(RuntimeError):
    """The published version moved since this run started."""


class IntegrityError(RuntimeError):
    """An artifact's bytes do not match the hash recorded in its manifest."""


@dataclass(frozen=True)
class Pointer:
    dataset_version: int
    logical_input_id: str
    manifest_key: str
    manifest_sha256: str


def dumps(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, indent=2, default=str).encode()


def parquet_bytes(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


def read_pointer(store: ArtifactStore) -> Pointer | None:
    if not store.exists(POINTER_KEY):
        return None
    return Pointer(**json.loads(store.get(POINTER_KEY)))


def read_manifest(store: ArtifactStore, pointer: Pointer) -> dict[str, Any]:
    raw = store.get(pointer.manifest_key)
    if sha256(raw) != pointer.manifest_sha256:
        raise IntegrityError(f"{pointer.manifest_key} does not match the published hash")
    manifest: dict[str, Any] = json.loads(raw)
    return manifest


def read_artifact(store: ArtifactStore, manifest: dict[str, Any], name: str) -> pl.DataFrame:
    meta = manifest["artifacts"][name]
    raw = store.get(meta["key"])
    if sha256(raw) != meta["sha256"]:
        raise IntegrityError(f"{meta['key']} does not match its manifest hash")
    return pl.read_parquet(io.BytesIO(raw))


@dataclass(frozen=True)
class Published:
    pointer: Pointer | None
    manifest: dict[str, Any] | None
    current: pl.DataFrame
    revisions: pl.DataFrame

    @property
    def version(self) -> int:
        return self.pointer.dataset_version if self.pointer else 0


def load_published(store: ArtifactStore) -> Published:
    pointer = read_pointer(store)
    if pointer is None:
        return Published(None, None, empty(SILVER_SCHEMA), empty(REVISION_SCHEMA))
    manifest = read_manifest(store, pointer)
    return Published(
        pointer,
        manifest,
        read_artifact(store, manifest, "gold_current"),
        read_artifact(store, manifest, "gold_revisions"),
    )


def published_logical_ids(store: ArtifactStore) -> dict[str, int]:
    """logical_input_id -> dataset_version for versions up to the current pointer.

    publish() writes the version record before the pointer, so a crash between the two leaves a
    record for a version that was never published. Records above the pointer are ignored.
    """
    pointer = read_pointer(store)
    root = store.path("published/versions")
    if pointer is None or not root.is_dir():
        return {}
    out = {}
    for f in root.glob("*.json"):
        p = json.loads(f.read_bytes())
        if p["dataset_version"] <= pointer.dataset_version:
            out[p["logical_input_id"]] = p["dataset_version"]
    return out


def publish(store: ArtifactStore, manifest_key: str, manifest: dict[str, Any]) -> Pointer:
    current = read_pointer(store)
    current_version = current.dataset_version if current else 0
    if manifest["base_dataset_version"] != current_version:
        raise PublishConflict(
            f"run was based on version {manifest['base_dataset_version']}, "
            f"but version {current_version} is published"
        )
    if manifest["dataset_version"] != current_version + 1:
        raise PublishConflict("dataset_version must be exactly one more than the current version")
    manifest_sha = sha256(store.get(manifest_key))
    pointer = Pointer(
        manifest["dataset_version"], manifest["logical_input_id"], manifest_key, manifest_sha
    )
    doc = dumps(pointer.__dict__)
    store.put(f"published/versions/{pointer.dataset_version:08d}.json", doc)
    store.put(POINTER_KEY, doc)
    return pointer
