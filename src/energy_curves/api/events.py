"""Live `dataset_updated` events over server-sent events (ADR-0009, ADR-0014).

- **Publish after commit.** `db-import` publishes to Valkey only once a version has committed,
  so a listener that re-fetches always finds the version it was told about.
- **Valkey only wakes streams up.** Pub/Sub is at-most-once, so its message is a hint. Each
  stream reads the current version from Postgres, both when woken and every `poll_s` seconds,
  so a lost message or a Valkey outage delays an event but never loses it.
- **Event id = dataset version.** A stream emits an event only when the current version differs
  from the last id it sent, so duplicates (several wake-ups for one version) are dropped. A
  reconnecting browser sends `Last-Event-ID`; if versions were published meanwhile it gets the
  current one at once, and re-fetches state.
- **Coalesced.** One event carries the current version; a client that missed versions 4 and 5
  gets one event for 5. The event says "re-fetch", the data comes from the HTTP API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import redis
import redis.asyncio as aioredis
from fastapi.sse import ServerSentEvent

log = logging.getLogger(__name__)

CHANNEL = "ecp:events"
ALERT_CHANNEL = "ecp:alerts"  # wakes alert streams (ADR-0015)
EVENT = "dataset_updated"
RETRY_MS = 3000


def publish_dataset_updated(client: redis.Redis, dataset_version: int) -> bool:
    """Best effort, after the import committed. Returns False if Valkey was unreachable;
    streams still pick the version up by polling Postgres."""
    message = json.dumps({"type": EVENT, "dataset_version": dataset_version})
    return _publish(client, CHANNEL, message)


def publish_alerts_wakeup(client: redis.Redis) -> bool:
    """Best effort, after a consumer pass committed. The alert log in Postgres is the stream;
    this only makes waiting alert streams look now instead of at their next poll."""
    return _publish(client, ALERT_CHANNEL, json.dumps({"type": "alerts_changed"}))


def _publish(client: redis.Redis, channel: str, message: str) -> bool:
    try:
        client.publish(channel, message)
    except redis.RedisError as exc:
        log.warning("could not publish to %s: %s", channel, exc)
        return False
    return True


class Wakeup:
    """Per-stream Valkey subscription. `start` subscribes before the stream first reads the
    version, so a publish between that read and the subscription cannot be missed. `wait`
    returns on a published message or after the timeout; while Valkey is unreachable it just
    sleeps, and resubscribes on the next call."""

    def __init__(self, url: str | None, channel: str = CHANNEL) -> None:
        self._url = url
        self._channel = channel
        self._client: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None

    async def start(self) -> bool:
        if self._url is None:
            return False
        try:
            if self._pubsub is None:
                self._client = aioredis.Redis.from_url(self._url, socket_connect_timeout=0.25)
                self._pubsub = self._client.pubsub()
                await self._pubsub.subscribe(self._channel)
        except (redis.RedisError, OSError) as exc:
            log.warning("event wake-up unavailable, polling Postgres: %s", type(exc).__name__)
            await self.close()
            return False
        return True

    async def wait(self, timeout: float) -> None:
        if not await self.start():
            await asyncio.sleep(timeout)
            return
        pubsub = self._pubsub
        if pubsub is None:  # start() succeeded, so this cannot happen; keeps mypy honest
            return
        try:
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        except (redis.RedisError, OSError) as exc:
            log.warning("event wake-up lost, polling Postgres: %s", type(exc).__name__)
            await self.close()
            await asyncio.sleep(timeout)

    async def close(self) -> None:
        pubsub, client, self._pubsub, self._client = self._pubsub, self._client, None, None
        try:
            if pubsub is not None:
                await pubsub.aclose()  # type: ignore[no-untyped-call]
            if client is not None:
                await client.aclose()
        except (redis.RedisError, OSError):
            pass


def parse_last_event_id(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


async def dataset_events(
    current_version: Callable[[], Awaitable[int | None]],
    wait: Callable[[float], Awaitable[None]],
    last_event_id: int | None,
    poll_s: float,
) -> AsyncIterator[ServerSentEvent]:
    last = last_event_id
    while True:
        version = await current_version()
        if version is not None and version != last:
            last = version
            yield ServerSentEvent(
                event=EVENT,
                id=str(version),
                raw_data=json.dumps({"dataset_version": version}),
                retry=RETRY_MS,
            )
        await wait(poll_s)
