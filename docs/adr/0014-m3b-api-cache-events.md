# 14. M3b: local read API, version-keyed cache and live events

Date: 2026-10-03 · Status: accepted

## Context
M3b serves the imported data locally (ADR-0013) and relays updates to browsers as ADR-0009 describes. Three properties needed explicit decisions:
- what a response's dataset version is allowed to label;
- what happens when Valkey is down or a notification is lost;
- how the local-only app refuses other origins.

Verified on 2026-10-03 against the locked dependencies:
- FastAPI 0.142.2 ships `fastapi.sse` (`EventSourceResponse`, `ServerSentEvent` with `id`, `event`, `retry`, `raw_data`), with a 15-second keep-alive comment.
- redis-py 8.1.0 provides both the sync client and the asyncio Pub/Sub used here.

## Decisions

### Endpoints (read-only)
| Endpoint | Returns |
|---|---|
| `GET /api/curves` | per curve, the latest available point of each position, labelled "latest available" |
| `GET /api/history/curves?curve_id&position&start&end` | one curve position over time |
| `GET /api/history/prices?series_id&start&end` | observed prices over time |
| `GET /api/export/curves.csv`, `GET /api/export/history.csv` | the same data as CSV |
| `GET /api/health` | published versions; failed and quarantined attempts; outbox counts and dead events |
| `GET /api/events` | server-sent `dataset_updated` events |

- **Envelope.** Every data response carries `dataset_version`, `source`, `synthetic`, `dataset_created_at`, `imported_at` and the disclaimer "Modelled estimate, not market quotes."
- **Curve fields.** Each curve and each point carries its actual `as_of` date and `data_age_days`. Points also carry `estimate_type`, `status` and `gap_reason`.
- **Prices** are decimal strings, never floats.
- **Status codes.** Nothing imported yet is `503`. An unknown curve or series is `404`. A bad position or a `start` after `end` is `422`.
- **"Latest available"** uses the same rule as the monitored values (ADR-0013): the point at each position's latest as-of date, including a gap.
- **No docs UI.** FastAPI's docs UI is off because it loads scripts from a CDN. The schema stays at `/api/openapi.json`.

### One snapshot per response
Each request reads the current version and its data in one `REPEATABLE READ, READ ONLY` transaction. An import that commits mid-request can therefore never put version N + 1 rows under a version N label, or into a version N cache key. A test commits version 2 between the two reads and asserts the response is entirely version 1. Under `READ COMMITTED` it fails.

### Cache keyed by dataset version
- **Key:** `ecp:api:<schema>:v<version>:<response>:<hash of parameters>`.
- **Never invalidated.** A version's data never changes: F2 refuses different content for a recorded version, and a rebuild re-derives identical rows. A new version simply uses new keys, and old entries expire after a day.
- **Shape changes.** `<schema>` is bumped when a response's shape changes, so an old shape is never served.
- **Age is not cached.** It is computed per request from the cached as-of date.
- **Valkey is optional.** Any Valkey error serves the request from Postgres (`X-Cache: bypass`) and skips the cache for 5 seconds. An outage then costs one 250 ms timeout, not one per request. `X-Cache` is `hit`, `miss` or `bypass`.
- **Not cached:** Health. Its attempts and outbox state change without a new dataset version.

### Live events
- **Publish after commit.** `db-import` publishes `{"type": "dataset_updated", "dataset_version": N}` to the Valkey channel `ecp:events` after each version commits, through the importer's `on_committed` hook. A rebuild publishes once, with its newest version. Publishing is best effort: a Valkey failure is logged and the import still succeeds.
- **Valkey only wakes streams.** Pub/Sub is at-most-once, so the message is a hint. Each SSE stream reads the current version from Postgres, both when woken and every 5 seconds. A lost message or an outage delays an event but never loses it.
- **Subscribe first.** A stream subscribes before its first read of the version, so a publish between that read and the subscription cannot be missed.
- **Event id = dataset version.** A stream emits an event only when the current version differs from the last id it sent. Duplicate wake-ups for one version are dropped.
- **Reconnects.** A reconnecting browser sends `Last-Event-ID`. If versions were published meanwhile, it gets the current one at once. Events are coalesced: missing versions 4 and 5 yields one event for 5. The event means "re-fetch"; the data always comes from the HTTP API.
- **Retry hint:** 3 seconds.
- **Open question for M3c.** Alert events will need their own ids. M3c decides whether to share this stream with a typed id or use a second event type with its own id sequence.

### Local-only HTTP
- **Bind.** `energy-curves serve` binds `127.0.0.1` and has no `--host` option.
- **Host.** The `Host` header must name `127.0.0.1` or `localhost`, otherwise `400`. This blocks DNS rebinding: a foreign name that resolves to loopback.
- **Origin.** A present `Origin` must be this app's own origin (`http://127.0.0.1:<port>` or `http://localhost:<port>`), otherwise `403`. Another local app on another port is another origin. `Origin: null` is refused.
- **CORS.** There is no CORS middleware, so no response carries `Access-Control-Allow-Origin`, and preflights are refused.
- **CSRF.** M3b has no state-changing endpoints. CSRF tokens arrive with the alert-rule endpoints in M3c (ADR-0013).

### Exports (ADR-0011)
- **Leading comment lines.** A CSV starts with `# Modelled estimate, not market quotes.`, then `# SYNTHETIC DATA` when the source is synthetic, then the dataset version, source and creation time.
- **Filename.** It includes the version, for example `curves-v3.csv`.

## Consequences
- An event can be late but never lost, and state is always read from Postgres (ADR-0009).
- Valkey can be stopped at any time. Reads get slower, never wrong.
- **Tests.** CI runs a pinned Valkey 9.1.2 service next to Postgres, with `ECP_REQUIRE_VALKEY=1`, so cache and event tests fail rather than skip. The end-to-end event tests run a real uvicorn server:
  - with polling at 60 seconds, an event can only arrive through the publish;
  - with Valkey down, polling still delivers.
