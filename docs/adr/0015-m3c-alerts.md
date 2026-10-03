# 15. M3c: threshold alerts and the durable alert stream

Date: 2026-10-03 · Status: accepted

## Context
ADR-0013 fixed the alert rules: upward crossing, re-arm, baseline for new rules, gaps, silence for watermark-only versions, and evaluation that consumes the outbox event atomically. ADR-0014 gave dataset updates an SSE stream whose event id is the dataset version.

Alerts need more than that:
- one dataset version can fire several alerts;
- a browser that disconnects between two of them must resume exactly after the last one it saw.

The user settled the design on 2026-10-03:
- a **separate alert SSE endpoint**, with its own durable, ordered cursor;
- `/api/events` stays for `dataset_updated`.

## Decisions

### Rules and state (`app.alert_rules`)
- **A rule is `(curve_id, position, threshold)`.**
  - `curve_id` comes from `curves.toml`, so a rule can exist before any data.
  - `position` is one of `Spot`, `C1`…`C4`.
  - `threshold` is a decimal with up to 4 places; negative values are valid for spreads.
- **Deletion is soft** (`deleted_at`), so fired alerts keep their rule.
- **State per rule:**
  - `armed`: NULL while no baseline value exists yet;
  - the last non-gap value, with its as-of date and version;
  - `baseline_version`.

### Evaluation (consumes `dataset_imported`, ADR-0013)
**Values.** The handler reads the event's own version through `monitored_values(conn, v)` (ADR-0013, F4). It never reads the latest snapshot.

**Who is evaluated.** For each live rule with `baseline_version < v`, in rule creation order:

| Monitored value at `v` | Effect |
|---|---|
| Missing or a gap | Nothing: neither fires nor re-arms. |
| First non-gap value (`armed` is NULL) | Sets the baseline, `armed = value <= threshold`; never fires. |
| `value <= threshold` | Re-arms. |
| `value > threshold` and armed | **Fires** and disarms. |
| `value > threshold`, not armed | Nothing. |

Rules for this logic:
- **Equivalence.** It is ADR-0013's "previous ≤ threshold < current" over the last non-gap value.
- **Ordering.** The outbox serialises evaluation in version order, so "previous" is the previous evaluated version.
- **Silence.** An unchanged value can never fire, so watermark-only versions are silent without consulting `price_changes`.
- **Dead events.** A version whose event dies after retries is skipped. The next version is compared with the last value that was evaluated.

**New rules.** A rule takes its baseline at creation, in one snapshot, from the current version's monitored value:
- `baseline_version` = the current version, or 0 when nothing is imported;
- `armed` = `value <= threshold`, or NULL when the value is missing or a gap.

Pending events for versions at or below the baseline therefore never fire the new rule.

**Atomicity and idempotence.**
- Rule state, fired alerts and the event's `done` marker commit in one transaction.
- A handler error rolls all of them back, and the retry starts from the same state.
- `alert_id = uuid5(rule_id, dataset_version)`, with `UNIQUE (rule_id, dataset_version)`, so no path can record a crossing twice.
- Re-import creates no event (ADR-0013), so it never re-fires.

### The alert log is the stream (`app.fired_alerts`, `app.alert_log`)
- **One row per alert.** Each fired alert is a row with `seq bigint GENERATED ALWAYS AS IDENTITY`. This log is the notification intent for the one M3 channel, the local SSE stream. A per-channel intents table arrives with an external channel (M6). This refines ADR-0013's "notification intents".
- **Commit order equals `seq` order.** The handler takes the alert-log advisory lock before inserting, and the outbox already runs one consumer per event type. So a reader can never see `seq` 11 committed while `seq` 10 is still pending. Sequence gaps from rolled-back attempts are harmless, because the cursor means "everything after".
- **`app.alert_log`** is a single row with:
  - `epoch`: a random UUID naming this log's history;
  - `floor`: the highest `seq` removed by pruning.

### Cursor and reconnect semantics (`GET /api/alerts/events`)
**Format.** Event id and cursor are `<epoch>-<seq>`.

**Where the resume point comes from.** From `Last-Event-ID`, which the browser sends on reconnect; otherwise from `?after=<cursor>`, for example the `cursor` returned by `GET /api/alerts`.

**What the stream does:**

| Resume point | Stream behaviour |
|---|---|
| None | Starts at the head. It first sends `event: alerts_cursor` with `id = <epoch>-<head>`, so the browser has a resume point for its next reconnect. |
| Valid: same epoch, `floor ≤ seq ≤ head` | Replays every alert with a larger `seq`, in order, then streams new ones. No reset is sent. |
| **Unknown:** malformed, another epoch, or `seq > head` | `event: alerts_reset` with `{"reason": "unknown_cursor"}`, then continues from the head. |
| **Expired:** `seq < floor`, so alerts after it were pruned | `event: alerts_reset` with `{"reason": "expired_cursor"}`, then continues from the head. |

**On a reset.** The reset carries `id = <epoch>-<head>`. The client re-fetches `GET /api/alerts` and keeps going.

**Why an epoch.** A database other than the one the cursor came from, or a restored older backup, can reuse `seq` values the client has already seen. A bare number would then skip new alerts silently. This is the lesson of the P2 cache collision, applied to cursors.

**Restore rotates the epoch.** `db-restore` sets a new epoch in its transaction, so every cursor issued before the restore is unknown and its client resets.

**Mid-stream changes.** A stream re-reads epoch, floor and new rows in one snapshot on every poll. A restore or prune while it is connected resets it too.

**Delivery mechanics:**
- **Event fields.** `event: alert_fired`; the data is the fired alert as JSON; `retry: 3000`.
- **Wake-ups.** Valkey channel `ecp:alerts`, published after a consumer pass commits. As in ADR-0014, Pub/Sub is only a hint: streams also poll Postgres, so with Valkey down alerts are late, never lost.

### Retention
- **Default:** alerts are kept.
- **Pruning.** `energy-curves alerts-prune --keep-days N` deletes older alerts and raises `floor`, in one transaction.
- **Why the floor is stored.** It makes expiry exact even when the log is empty.

### Running the consumer
Evaluation runs:
- after `db-import` commits (one pass);
- from `energy-curves process-events`;
- every few seconds in a background thread of `serve`, so retries and backoff proceed without an import.

A consumer is serialised per event type (ADR-0013), so these never double-process.

### HTTP (local-only, ADR-0014)
| Endpoint | Purpose |
|---|---|
| `GET /api/alerts/rules`, `POST /api/alerts/rules`, `DELETE /api/alerts/rules/{rule_id}` | list, create and soft-delete rules |
| `GET /api/alerts?after=&limit=` | the fired-alert history, plus the current `cursor` |
| `GET /api/alerts/events` | the alert stream |

**CSRF.** State-changing requests need `X-CSRF-Token` equal to the per-process token from `GET /api/csrf`, compared in constant time. The token is in addition to the Host/Origin guard. A cross-site page can read neither the token nor any response, because there is no CORS. It also cannot send the custom header without a preflight, which is refused.

## Consequences
- **Delivery guarantee.** An alert is delivered at least once to a connected or resuming client, and the client de-duplicates by event id. A client that missed alerts beyond retention, or across a restore, is told so explicitly instead of silently skipping.
- **Required tests.** These add to ADR-0013's alert criteria; they do not replace them:
  - several alerts from one version, with a disconnect between two of them, then a resume that yields exactly the rest, in order;
  - unknown and expired cursors, including after a restore and after pruning;
  - handler failure and retry, with no duplicate or lost alerts;
  - delivery with Valkey down.
