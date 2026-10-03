"""A real uvicorn server in a thread, and an SSE reader, for end-to-end stream tests."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from energy_curves.api.app import create_app
from energy_curves.api.cache import VersionCache


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def serve(db: str, redis_url: str | None, poll_s: float) -> Iterator[str]:
    """The app on a free loopback port; no background consumer (tests drive passes)."""
    port = free_port()
    app = create_app(
        database_url=db, cache=VersionCache(None), redis_url=redis_url, port=port, poll_s=poll_s
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


def next_event(lines: Iterator[str]) -> dict[str, str]:
    """The next complete event (with an id) from `response.iter_lines()`."""
    event: dict[str, str] = {}
    for line in lines:
        if line == "" and "id" in event:
            return event
        if line.startswith(":"):
            continue  # keep-alive comment
        field, _, value = line.partition(": ")
        if field in {"id", "event", "data", "retry"}:
            event[field] = value
    raise AssertionError("stream ended")
