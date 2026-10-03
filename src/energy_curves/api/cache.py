"""Response cache in Valkey, keyed by dataset version (ADR-0009, ADR-0014).

An entry's identity is the dataset history it was read from, not just its version number: two
databases (or a database reset and re-imported from another store) can both be at version 1 with
different content, and may share one Valkey. The key therefore carries the version and its
manifest SHA-256. Every served row comes from that version's hash-verified artifacts, which the
manifest pins, so equal keys mean equal data. Within one database a recorded version never
changes content (ADR-0013, F2) and a rebuild re-derives identical rows, so entries are never
invalidated; a new version uses new keys, and old ones expire. The key also carries the response
name, its parameters and CACHE_SCHEMA, so a change to a response's shape never serves the old
shape.

Valkey is an optimisation, never a dependency. Any Valkey error makes the caller read Postgres,
and the cache is bypassed for `cooldown` seconds so an outage costs one timeout, not one per
request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import redis

log = logging.getLogger(__name__)

CACHE_SCHEMA = 2  # 2: keys carry the manifest SHA-256
HIT, MISS, BYPASS = "hit", "miss", "bypass"


class VersionCache:
    def __init__(
        self,
        client: redis.Redis | None,
        *,
        prefix: str = "ecp:api",
        ttl_s: int = 86_400,
        cooldown_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._prefix = f"{prefix}:{CACHE_SCHEMA}"
        self._ttl_s = ttl_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._down_until = 0.0

    @classmethod
    def from_url(cls, url: str, **kw: Any) -> VersionCache:
        client = redis.Redis.from_url(url, socket_connect_timeout=0.25, socket_timeout=0.25)
        return cls(client, **kw)

    def key(
        self, dataset_version: int, manifest_sha256: str, name: str, params: dict[str, Any]
    ) -> str:
        digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        return f"{self._prefix}:v{dataset_version}:{manifest_sha256}:{name}:{digest}"

    def get_or_load(
        self,
        dataset_version: int,
        manifest_sha256: str,
        name: str,
        params: dict[str, Any],
        load: Callable[[], Any],
    ) -> tuple[Any, str]:
        """Return (value, hit | miss | bypass). `load` must return JSON-safe data read in the
        same snapshot as the version and manifest."""
        if self._client is None or self._clock() < self._down_until:
            return load(), BYPASS
        key = self.key(dataset_version, manifest_sha256, name, params)
        try:
            cached = self._client.get(key)
        except redis.RedisError as exc:
            self._trip(exc)
            return load(), BYPASS
        if cached is not None:
            return json.loads(cached), HIT
        value = load()
        try:
            self._client.set(key, json.dumps(value), ex=self._ttl_s)
        except redis.RedisError as exc:
            self._trip(exc)
            return value, BYPASS
        return value, MISS

    def _trip(self, exc: Exception) -> None:
        self._down_until = self._clock() + self._cooldown_s
        log.warning("cache unavailable, reading Postgres: %s", type(exc).__name__)
