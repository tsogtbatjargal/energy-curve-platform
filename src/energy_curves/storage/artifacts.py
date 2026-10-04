"""Artifact store: the seam between local files (M2) and S3 (M4, ADR-0019).

Writes are atomic: a reader never sees a half-written artifact under its final key. Publishing
needs one more primitive, a conditional write (`put_if`), so that two writers cannot both move
the dataset pointer: locally the single-writer lock already serializes runs; on S3 the pointer is
written with If-Match / If-None-Match.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol


class PreconditionFailed(RuntimeError):
    """A conditional write lost: the object changed (or appeared) since it was read."""


class ArtifactStore(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def get_tagged(self, key: str) -> tuple[bytes, str] | None: ...
    def put_if(self, key: str, data: bytes, *, tag: str | None) -> str: ...


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalArtifactStore:
    """Files under a root directory. Tags are content hashes. `put_if` is not atomic against a
    concurrent writer; local runs are serialized by the single-writer lock instead."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError(f"key escapes the store root: {key}")
        return p

    def put(self, key: str, data: bytes) -> str:
        """Atomically write `data` under `key`; return its SHA-256."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha256(data)

    def get(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def list(self, prefix: str) -> list[str]:
        """Keys starting with `prefix`, as S3 lists them: a key prefix, not a directory."""
        start = self.path(prefix.rsplit("/", 1)[0]) if "/" in prefix else self.root
        return sorted(
            k
            for p in start.rglob("*")
            if p.is_file()
            and not p.name.startswith(".")
            and (k := p.relative_to(self.root).as_posix()).startswith(prefix)
        )

    def get_tagged(self, key: str) -> tuple[bytes, str] | None:
        if not self.exists(key):
            return None
        data = self.get(key)
        return data, sha256(data)

    def put_if(self, key: str, data: bytes, *, tag: str | None) -> str:
        current = self.get_tagged(key)
        if (current[1] if current else None) != tag:
            raise PreconditionFailed(f"{key} changed since it was read")
        return self.put(key, data)


class S3ArtifactStore:
    """Objects in an S3 bucket under `prefix/`. Tags are ETags; `put_if` uses S3 conditional
    writes (If-Match, or If-None-Match: * for "must not exist"), which fail with 412 when another
    writer got there first."""

    def __init__(self, client: Any, bucket: str, prefix: str = "") -> None:
        self.client, self.bucket = client, bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _key(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"key escapes the store prefix: {key}")
        return self.prefix + key

    def put(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        return sha256(data)

    def get(self, key: str) -> bytes:
        body: bytes = self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def list(self, prefix: str) -> list[str]:
        keys = []
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self._key(prefix)
        )
        for page in pages:
            keys += [o["Key"][len(self.prefix) :] for o in page.get("Contents", [])]
        return sorted(keys)

    def get_tagged(self, key: str) -> tuple[bytes, str] | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.NoSuchKey:
            return None
        return obj["Body"].read(), obj["ETag"]

    def put_if(self, key: str, data: bytes, *, tag: str | None) -> str:
        condition = {"IfMatch": tag} if tag is not None else {"IfNoneMatch": "*"}
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, **condition)
        except self.client.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"PreconditionFailed", "ConditionalRequestConflict", "NoSuchKey", "412"}:
                raise PreconditionFailed(f"{key} changed since it was read ({code})") from exc
            raise
        return sha256(data)
