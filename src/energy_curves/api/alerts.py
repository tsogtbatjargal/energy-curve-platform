"""Alert rules, alert history and the alert stream over HTTP (ADR-0015).

`/api/alerts/events` is separate from `/api/events` (dataset updates), with its own durable,
ordered cursor `<epoch>-<seq>` taken from the alert log in Postgres:

- **No cursor:** the stream starts at the head and first sends `alerts_cursor` (with that id), so
  the browser has a resume point for its next reconnect.
- **Valid cursor** (`Last-Event-ID` on reconnect, or `?after=`): every later alert, in order.
- **Unknown or expired cursor:** `alerts_reset` with the reason and the head's id; the client
  re-fetches `GET /api/alerts`, and the stream continues from the head.
- **While connected:** every poll re-reads the epoch, floor and new alerts in one snapshot, so a
  restore (new epoch) or a prune past the stream's position resets it too.

Valkey only wakes streams (channel `ecp:alerts`); with Valkey down they still poll Postgres.
State-changing requests need the per-process CSRF token in `X-CSRF-Token`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict

from energy_curves.api.events import ALERT_CHANNEL, RETRY_MS, Wakeup
from energy_curves.api.queries import jsonable
from energy_curves.db import alerts

log = logging.getLogger(__name__)

BATCH = 100


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    curve_id: str
    position: str
    threshold: str  # a decimal string: a JSON number would arrive as a float


def _json(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: jsonable(v) for k, v in r.items()} for r in rows]


def alert_event(epoch: uuid.UUID, row: dict[str, Any]) -> ServerSentEvent:
    return ServerSentEvent(
        event="alert_fired",
        id=f"{epoch.hex}-{row['seq']}",
        raw_data=json.dumps({k: jsonable(v) for k, v in row.items()}),
        retry=RETRY_MS,
    )


def marker(event: str, state: alerts.LogState, **data: Any) -> ServerSentEvent:
    return ServerSentEvent(
        event=event,
        id=state.cursor(),
        raw_data=json.dumps({"cursor": state.cursor(), **data}),
        retry=RETRY_MS,
    )


async def alert_stream(
    read: Callable[[int | None], Awaitable[tuple[alerts.LogState, list[dict[str, Any]]]]],
    wait: Callable[[float], Awaitable[None]],
    cursor: str | None,
    poll_s: float,
) -> AsyncIterator[ServerSentEvent]:
    """`read(seq)` returns the log state and the alerts after `seq` from one snapshot
    (`read(None)` only the state)."""
    state, _ = await read(None)
    resume = alerts.resolve(state, cursor)
    epoch, position = state.epoch, resume.start
    if resume.reset:
        yield marker("alerts_reset", state, reason=resume.reset)
    elif resume.fresh:
        yield marker("alerts_cursor", state)
    while True:
        try:
            state, rows = await read(position)
        except psycopg.Error as exc:
            log.warning("alert stream: Postgres unavailable: %s", type(exc).__name__)
            await wait(poll_s)
            continue
        if state.epoch != epoch or position < state.floor:
            reason = "unknown_cursor" if state.epoch != epoch else "expired_cursor"
            epoch, position = state.epoch, state.head
            yield marker("alerts_reset", state, reason=reason)
            continue
        for row in rows:
            position = row["seq"]
            yield alert_event(epoch, row)
        if len(rows) < BATCH:
            await wait(poll_s)


def alerts_router(*, database_url: str, redis_url: str | None, poll_s: float) -> APIRouter:
    router = APIRouter()
    token = secrets.token_urlsafe(32)

    def connect(read_only: bool = False) -> psycopg.Connection:
        conn = psycopg.connect(database_url)
        if read_only:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
        return conn

    def require_csrf(x_csrf_token: str | None = Header(default=None)) -> None:
        if x_csrf_token is None or not secrets.compare_digest(x_csrf_token, token):
            raise HTTPException(403, "missing or invalid CSRF token")

    @router.get("/api/csrf")
    def csrf() -> dict[str, str]:
        return {"token": token}

    @router.get("/api/alerts/rules")
    def list_rules() -> dict[str, Any]:
        with connect() as conn:
            return {"rules": _json(alerts.list_rules(conn))}

    @router.post("/api/alerts/rules", status_code=201, dependencies=[Depends(require_csrf)])
    def create_rule(rule: RuleIn) -> JSONResponse:
        try:
            threshold = alerts.parse_threshold(rule.threshold)
            with connect() as conn:
                created = alerts.create_rule(conn, rule.curve_id, rule.position, threshold)
        except alerts.RuleError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse(_json([created])[0], status_code=201)

    @router.delete(
        "/api/alerts/rules/{rule_id}", status_code=204, dependencies=[Depends(require_csrf)]
    )
    def delete_rule(rule_id: uuid.UUID) -> Response:
        with connect() as conn:
            if not alerts.delete_rule(conn, rule_id):
                raise HTTPException(404, "no such rule")
        return Response(status_code=204)

    @router.get("/api/alerts")
    def history(
        after: str | None = None, limit: int = Query(default=100, ge=1, le=1000)
    ) -> dict[str, Any]:
        """Without `after`: the latest `limit` alerts. With `after`: the alerts after that
        cursor, oldest first. `cursor` is where to resume (pass it to the stream as `?after=`)."""
        with connect(read_only=True) as conn:
            state = alerts.log_state(conn)
            if after is None:
                rows = alerts.latest_alerts(conn, limit)
                return {"cursor": state.cursor(), "reset": None, "alerts": _json(rows)}
            resume = alerts.resolve(state, after)
            rows = [] if resume.reset else alerts.alerts_after(conn, resume.start, limit)
        last = rows[-1]["seq"] if rows else resume.start
        return {"cursor": state.cursor(last), "reset": resume.reset, "alerts": _json(rows)}

    def read(seq: int | None) -> tuple[alerts.LogState, list[dict[str, Any]]]:
        with connect(read_only=True) as conn:
            state = alerts.log_state(conn)
            return state, [] if seq is None else alerts.alerts_after(conn, seq, BATCH)

    async def read_async(seq: int | None) -> tuple[alerts.LogState, list[dict[str, Any]]]:
        return await asyncio.to_thread(read, seq)

    @router.get("/api/alerts/events", response_class=EventSourceResponse)
    async def events(
        after: str | None = None, last_event_id: str | None = Header(default=None)
    ) -> AsyncIterator[ServerSentEvent]:
        wakeup = Wakeup(redis_url, ALERT_CHANNEL)
        try:
            await wakeup.start()  # subscribe before the first read
            cursor = last_event_id if last_event_id is not None else after
            async for event in alert_stream(read_async, wakeup.wait, cursor, poll_s):
                yield event
        finally:
            await wakeup.close()

    return router
