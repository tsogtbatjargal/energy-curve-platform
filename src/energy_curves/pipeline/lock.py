"""Single-writer lock for local pipeline runs.

Uses an OS advisory lock (flock). The kernel releases it when the holder exits, including on
SIGKILL, so a crashed run never leaves a stale lock behind. S3-based coordination replaces this
in M4.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class PipelineLocked(RuntimeError):
    """Another pipeline run holds the writer lock."""


@contextmanager
def single_writer(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PipelineLocked(f"another run holds {lock_path}") from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
