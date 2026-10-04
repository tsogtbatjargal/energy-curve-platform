"""Manifests and the published-dataset pointer.

Order of operations for a run: write every artifact, then the run manifest, then publish by
replacing `published/current.json`. A crash anywhere before the replace leaves the previous
dataset as the published one. Publication requires the manifest's base version to equal the
current version, so an older run can never move the dataset backward.

The pointer write is the commit point, and it is conditional (ADR-0019): it succeeds only if the
pointer is still the one this publish read (If-Match on its tag, or "must not exist" for the first
version). Two runs racing to publish cannot both win, with or without the local lock. The version
record under published/versions/ is an index written after the commit; the pointer itself is
authoritative for its own version, so a crash between the two loses nothing.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import polars as pl

from energy_curves.pipeline.medallion import GOLD_SCHEMA, REVISION_SCHEMA, empty
from energy_curves.storage.artifacts import ArtifactStore, PreconditionFailed, sha256

POINTER_KEY = "published/current.json"
VERSIONS_PREFIX = "published/versions/"
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
        return Published(None, None, empty(GOLD_SCHEMA), empty(REVISION_SCHEMA))
    manifest = read_manifest(store, pointer)
    return Published(
        pointer,
        manifest,
        read_artifact(store, manifest, "gold_current"),
        read_artifact(store, manifest, "gold_revisions"),
    )


def version_key(version: int) -> str:
    return f"{VERSIONS_PREFIX}{version:08d}.json"


def published_logical_ids(store: ArtifactStore) -> dict[str, int]:
    """logical_input_id -> dataset_version for every published version.

    Records below the pointer come from the index. The pointer is authoritative for its own
    version: its record may be missing (a crash after the commit) or stale (stores written before
    ADR-0019 wrote the record first, so a crash could leave one for a version never published).
    Records above the pointer are ignored for the same reason.
    """
    pointer = read_pointer(store)
    if pointer is None:
        return {}
    out = {}
    for key in store.list(VERSIONS_PREFIX):
        if key.endswith(".json"):
            p = json.loads(store.get(key))
            if p["dataset_version"] < pointer.dataset_version:
                out[p["logical_input_id"]] = p["dataset_version"]
    out[pointer.logical_input_id] = pointer.dataset_version
    return out


def publish(store: ArtifactStore, manifest_key: str, manifest: dict[str, Any]) -> Pointer:
    tagged = store.get_tagged(POINTER_KEY)
    current = Pointer(**json.loads(tagged[0])) if tagged else None
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
    if tagged and not store.exists(version_key(current_version)):
        store.put(version_key(current_version), tagged[0])  # repair: lost after its commit
    try:
        store.put_if(POINTER_KEY, doc, tag=tagged[1] if tagged else None)  # the commit
    except PreconditionFailed as exc:
        raise PublishConflict(f"another run published version {pointer.dataset_version}") from exc
    store.put(version_key(pointer.dataset_version), doc)
    return pointer
