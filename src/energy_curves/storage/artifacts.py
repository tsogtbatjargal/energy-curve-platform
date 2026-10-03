"""Artifact store: the seam between local files (M2) and S3 (M4).

Writes are atomic (temp file + rename), so a crash never leaves a half-written artifact
under its final key.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Protocol


class ArtifactStore(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def path(self, key: str) -> Path: ...


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalArtifactStore:
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
