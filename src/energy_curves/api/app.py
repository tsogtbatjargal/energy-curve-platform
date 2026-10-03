"""Local read API: curves, history, health, CSV export and live events (ADR-0013, ADR-0014).

Every data response carries `dataset_version`, its source (`synthetic` flag), the disclaimer,
and per curve the actual as-of date and the data's age. Responses are cached in Valkey under the
dataset version and its manifest hash; the age is computed per request, so a cached response
never reports a stale age. The app is local-only: `serve` binds 127.0.0.1, the Host header must
name a loopback host (DNS-rebinding guard), a cross-origin Origin is refused, and there is no
CORS.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.datastructures import Headers
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from energy_curves.api import queries
from energy_curves.api.alerts import alerts_router
from energy_curves.api.cache import BYPASS, VersionCache
from energy_curves.api.events import Wakeup, dataset_events, parse_last_event_id
from energy_curves.catalog import POSITIONS
from energy_curves.db.alerts import curve_ids

log = logging.getLogger(__name__)

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
Position = Literal["Spot", "C1", "C2", "C3", "C4"]


class LocalOnly:
    """Refuse a Host that is not loopback (DNS rebinding) and any cross-origin Origin."""

    def __init__(self, app: ASGIApp, *, port: int, hosts: frozenset[str] = LOCAL_HOSTS) -> None:
        self.app = app
        self.hosts = hosts
        self.origins = {f"http://{h}:{port}" for h in hosts}
        if port == 80:
            self.origins |= {f"http://{h}" for h in hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            hostname = headers.get("host", "").rsplit(":", 1)[0]
            origin = headers.get("origin")
            refusal = None
            if hostname not in self.hosts:
                refusal = JSONResponse({"detail": "invalid Host header"}, status_code=400)
            elif origin is not None and origin not in self.origins:
                refusal = JSONResponse({"detail": "cross-origin request refused"}, 403)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)


CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:;"
    " font-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self';"
    " frame-ancestors 'none'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
]
STATIC = Path(__file__).parent / "static"


class SecurityHeaders:
    """CSP and friends on every HTTP response, refusals included (ADR-0016). Scripts load from
    this origin only; inline styles are allowed because w2ui sets them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *SECURITY_HEADERS]
            await send(message)

        await self.app(scope, receive, with_headers)


def _today() -> date:
    return datetime.now(UTC).date()


def create_app(
    *,
    database_url: str,
    cache: VersionCache,
    redis_url: str | None,
    port: int,
    today: Callable[[], date] = _today,
    poll_s: float = 5.0,
    consumer_interval_s: float | None = None,
) -> FastAPI:
    """`consumer_interval_s` runs the alert consumer in a background thread (ADR-0015), so
    retries proceed without an import; `serve` turns it on, tests drive passes themselves."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        thread = None
        if consumer_interval_s:
            thread = threading.Thread(
                target=_consume,
                args=(database_url, redis_url, consumer_interval_s, stop),
                name="alert-consumer",
                daemon=True,
            )
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                thread.join(10)

    app = FastAPI(
        title="energy-curves (local)",
        docs_url=None,  # the docs UI loads scripts from a CDN
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(LocalOnly, port=port)
    app.add_middleware(SecurityHeaders)  # added last: outermost, so refusals carry it too
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/catalog")
    def catalog() -> dict[str, list[str]]:
        """The curves and positions a rule or a history query can name."""
        return {"curves": sorted(curve_ids()), "positions": list(POSITIONS)}

    app.include_router(alerts_router(database_url=database_url, redis_url=redis_url, poll_s=poll_s))

    @contextmanager
    def snapshot() -> Iterator[psycopg.Connection]:
        with psycopg.connect(database_url) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            yield conn

    def versioned(
        name: str, params: dict[str, Any], load: Callable[[psycopg.Connection], Any]
    ) -> tuple[queries.Current, Any, str]:
        """Read the current version and its data in one snapshot, through the cache."""
        with snapshot() as conn:
            current = queries.current(conn)
            if current is None:
                raise HTTPException(503, "no dataset version imported yet")
            data, status = cache.get_or_load(
                current.dataset_version, current.manifest_sha256, name, params, lambda: load(conn)
            )
        return current, data, status

    def respond(current: queries.Current, body: dict[str, Any], status: str) -> JSONResponse:
        return JSONResponse(
            {**current.envelope(), **body},
            headers={"X-Cache": status, "X-Dataset-Version": str(current.dataset_version)},
        )

    def with_age(curves: list[dict[str, Any]], on: date) -> list[dict[str, Any]]:
        def age(as_of: str) -> int:
            return (on - date.fromisoformat(as_of)).days

        return [
            {
                **c,
                "data_age_days": age(c["as_of"]),
                "points": [{**p, "data_age_days": age(p["as_of"])} for p in c["points"]],
            }
            for c in curves
        ]

    def check_range(start: date | None, end: date | None) -> None:
        if start and end and start > end:
            raise HTTPException(422, "start is after end")

    def require(found: bool, what: str) -> None:
        if not found:
            raise HTTPException(404, f"unknown {what}")

    @app.get("/api/curves")
    def curves() -> JSONResponse:
        current, data, status = versioned("curves", {}, queries.latest_curves)
        on = today()
        body = {"label": "latest available", "today": on.isoformat(), "curves": with_age(data, on)}
        return respond(current, body, status)

    def load_curve_history(
        curve_id: str, position: str, start: date | None, end: date | None
    ) -> tuple[queries.Current, dict[str, Any], str]:
        check_range(start, end)
        params = _str({"curve_id": curve_id, "position": position, "start": start, "end": end})

        def load(conn: psycopg.Connection) -> dict[str, Any]:
            return {
                "known": queries.curve_exists(conn, curve_id),
                "points": queries.curve_history(conn, curve_id, position, start, end),
            }

        current, data, status = versioned("curve_history", params, load)
        require(data["known"], f"curve {curve_id}")
        return current, {**params, "points": data["points"]}, status

    @app.get("/api/history/curves")
    def curve_history(
        curve_id: str,
        position: Position,
        start: date | None = None,
        end: date | None = None,
    ) -> JSONResponse:
        return respond(*load_curve_history(curve_id, position, start, end))

    @app.get("/api/history/prices")
    def price_history(
        series_id: str, start: date | None = None, end: date | None = None
    ) -> JSONResponse:
        check_range(start, end)
        params = _str({"series_id": series_id, "start": start, "end": end})

        def load(conn: psycopg.Connection) -> dict[str, Any]:
            return {
                "known": queries.series_exists(conn, series_id),
                "points": queries.price_history(conn, series_id, start, end),
            }

        current, data, status = versioned("price_history", params, load)
        require(data["known"], f"series {series_id}")
        return respond(current, {**params, "points": data["points"]}, status)

    @app.get("/api/export/curves.csv")
    def export_curves() -> Response:
        current, data, status = versioned("curves", {}, queries.latest_curves)
        rows = [
            {"curve_id": c["curve_id"], "kind": c["kind"], **p}
            for c in with_age(data, today())
            for p in c["points"]
        ]
        return _csv(current, f"curves-v{current.dataset_version}.csv", rows, status)

    @app.get("/api/export/history.csv")
    def export_history(
        curve_id: str,
        position: Position,
        start: date | None = None,
        end: date | None = None,
    ) -> Response:
        current, body, status = load_curve_history(curve_id, position, start, end)
        rows = [{"curve_id": curve_id, "position": position, **p} for p in body["points"]]
        name = f"history-{curve_id}-{position}-v{current.dataset_version}.csv"
        return _csv(current, name, rows, status)

    @app.get("/api/health")
    def health() -> JSONResponse:
        with snapshot() as conn:
            current = queries.current(conn)
            body = queries.health(conn)
        head = current.envelope() if current else {"dataset_version": None}
        return JSONResponse({**head, **body}, headers={"X-Cache": BYPASS})

    async def current_version() -> int | None:
        try:
            async with await psycopg.AsyncConnection.connect(database_url) as conn:
                cur = await conn.execute("SELECT max(dataset_version) FROM market.dataset_versions")
                row = await cur.fetchone()
                return row[0] if row else None
        except psycopg.Error as exc:
            log.warning("events: Postgres unavailable: %s", type(exc).__name__)
            return None

    @app.get("/api/events", response_class=EventSourceResponse)
    async def events(
        last_event_id: str | None = Header(default=None),
    ) -> AsyncIterator[ServerSentEvent]:
        wakeup = Wakeup(redis_url)
        try:
            await wakeup.start()  # subscribe before the first read of the version
            async for event in dataset_events(
                current_version, wakeup.wait, parse_last_event_id(last_event_id), poll_s
            ):
                yield event
        finally:
            await wakeup.close()

    return app


def _consume(dsn: str, redis_url: str | None, interval_s: float, stop: threading.Event) -> None:
    import redis

    from energy_curves.api.events import publish_alerts_wakeup
    from energy_curves.db.alerts import process_events

    client = (
        redis.Redis.from_url(redis_url, socket_connect_timeout=0.25, socket_timeout=0.25)
        if redis_url
        else None
    )
    wake = (lambda: publish_alerts_wakeup(client)) if client else None
    while True:
        try:
            process_events(dsn, wake)
        except Exception:  # noqa: BLE001 - keep consuming; events stay pending and are retried
            log.exception("alert consumer pass failed")
        if stop.wait(interval_s):
            return


def _str(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if isinstance(v, date) else v for k, v in params.items()}


def _csv(
    current: queries.Current, filename: str, rows: list[dict[str, Any]], status: str
) -> Response:
    """CSV with the disclaimer, and the SYNTHETIC DATA marker when it applies, as leading
    comment lines, so the label travels with the file (ADR-0011)."""
    out = io.StringIO()
    out.write(f"# {queries.DISCLAIMER}\n")
    if current.synthetic:
        out.write("# SYNTHETIC DATA\n")
    out.write(
        f"# dataset_version={current.dataset_version} source={current.source}"
        f" dataset_created_at={queries.jsonable(current.created_at)}\n"
    )
    if rows:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Cache": status,
            "X-Dataset-Version": str(current.dataset_version),
        },
    )
