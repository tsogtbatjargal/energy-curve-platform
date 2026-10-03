# 13. M3 serving architecture: Postgres, Valkey, outbox, alerts

Date: 2026-10-03 · Status: accepted

## Context
M3 adds a local app that serves the published Gold data with live updates and threshold alerts. Everything runs locally (podman compose); no AWS changes. Facts verified on 2026-10-03:
- **ElastiCache (ca-central-1)** supports Valkey 7.2, 8.0, 8.1, 8.2, 9.0 and 9.1 (`describe-cache-engine-versions`).
- **RDS (ca-central-1)** supports PostgreSQL 17.5 to 17.11.
- **Redis licensing.** Redis 7.2 and earlier: BSD-3. Redis 8.0 onward: the user's choice of RSALv2, SSPLv1 or **AGPLv3**. AGPLv3 is an OSI-approved open-source licence, with copyleft that also covers network use. Valkey is a BSD-3 fork of Redis 7.2.4.

## Decisions

### Runtime and data access
- **Cache:** Valkey, accessed with the standard `redis` Python client. The local image is pinned by digest to `valkey/valkey:9.1.2`, in the 9.1 family ElastiCache supports.
- **Database:** Postgres pinned by digest to `postgres:17.11`.
- **Ports:** every published port binds to `127.0.0.1`.
- **Database access:** psycopg 3 with parameterised SQL and no ORM. Bulk data goes through `COPY` into temporary staging tables, which are validated and then published inside one transaction.

### Migrations
Numbered plain SQL files (`NNNN_name.sql`) with a small runner:
- **Contiguous numbering.** Versions start at 1 with no gaps.
- **Checksums.** A SHA-256 of each file's exact bytes is recorded. An edited, missing or unknown applied migration stops the run.
- **Lock.** A Postgres advisory lock, taken with a timeout, so concurrent runners serialise and each migration is applied exactly once.
- **Transactions.** Each migration and its record commit in one transaction, so a failing migration leaves no trace and can be re-run once fixed.

### Data ownership
Postgres has two schemas with different owners.

| Schema | Owner | Contents | Recovery |
|---|---|---|---|
| `market` | published artifacts | dataset versions, observations, revisions, curve points, pipeline attempts (including failed and quarantined ones) | `db-rebuild-market` truncates and re-imports from the hash-verified published versions |
| `app` | Postgres | outbox events; from M3c, alert rules, alert state, fired alerts, notification intents | `db-backup` and `db-restore`: checksummed CSV per table, restored atomically, refused if the migration version differs |

Published artifacts can rebuild everything in `market`, but nothing in `app`.

### Imports
- **Serialised.** A transaction-scoped advisory lock means one import at a time.
- **Ordered.** The importer applies published versions strictly as `max + 1`. An out-of-order version is rejected.
- **Idempotent.**
  - The same version with the same manifest hash is `already_imported`.
  - A different manifest hash for an imported version, or a `logical_input_id` already recorded under another version, is a **conflict** and changes nothing.
- **Verified.** Every artifact hash is checked. Staging is validated against the manifest's row counts and key uniqueness, and the dataset must be single-source.
- **Outbox.** The import commits a `dataset_imported` outbox event in the same transaction as the market data.

### Outbox
- **Claiming.** Consumers claim events with `FOR UPDATE SKIP LOCKED`, head-of-line by dataset version, so events are processed in order.
- **Atomic effects.** A handler's database effects commit in the same transaction that marks the event `done`. A handler error rolls back its effects, then counts an attempt and schedules a retry with exponential backoff. After the maximum number of attempts the event becomes `dead` and is reported on the Health tab.
- **Crash safety.** A crash after the import commits leaves a durable `pending` event. Alert evaluation is never lost.

### Alerts (M3c)
- **Upward crossing:** fires when `previous <= threshold` and `current > threshold`. It re-arms once a value is `<= threshold` again.
- **New rules** establish a baseline without firing.
- **Missing values** (gaps) neither fire nor re-arm.
- **What is evaluated.** Each changed **monitored value**: the rule's curve and position at the latest available date. This is not gated on the global `price_changes` count, because a change in shape parameters moves curve prices without changing raw observations.
- **Silence.** Watermark-only updates leave monitored values unchanged, so they can never cross.
- **Atomicity.** Evaluation consumes the outbox event. Alert state, fired alerts and notification intents commit atomically with marking the event done.

### API and UI (M3b, M3d)
- **Labels.** Curves are labelled "latest available", with their actual observation date and the data's age.
- **Health tab:** published versions; failed and quarantined attempts; dead outbox events.
- **Local-only HTTP protection:** bind to `127.0.0.1`; validate `Host` and `Origin`; CSRF tokens on state-changing endpoints; no permissive CORS.

### Data shown (ADR-0011)
Real EIA data is shown locally only. Screenshots, video and CI artefacts use synthetic data and synthetic parameters. The "SYNTHETIC DATA" badge appears in the UI and in exports.

## Delivery
Four PRs, each implemented and reviewed one at a time. Estimated 20–28 hours, subject to revision.
- **M3a:** compose, migrations, `market` and `app` schemas, importer, outbox, backup and restore.
- **M3b:** API, cache, SSE.
- **M3c:** alerts.
- **M3d:** UI, replay, Playwright, including at least one mobile smoke test.
