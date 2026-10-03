# 13. M3 serving architecture: Postgres, Valkey, outbox, alerts

Date: 2026-10-03 · Status: accepted; amended 2026-10-03 for the PR #5 recovery findings F1–F4

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
| `market` | published artifacts | dataset versions, observations, revisions, curve points, per-version monitored values, pipeline attempts (including failed and quarantined ones) | `db-import --rebuild` re-imports everything from the hash-verified published versions, all or nothing |
| `app` | Postgres | outbox events; from M3c, alert rules, alert state, fired alerts, notification intents | `db-backup` and `db-restore`: checksummed CSV per table, restored atomically, refused if the migration version differs, then reconciled with `market` (see *Restore*) |

Published artifacts can rebuild everything in `market`, but nothing in `app`.

### Imports
- **Serialised.** A transaction-scoped advisory lock means one import at a time.
- **Ordered.** The importer applies published versions strictly as `max + 1`. An out-of-order version is rejected.
- **Idempotent.**
  - The same version with the same manifest hash is `already_imported`.
  - A different manifest hash for an imported version, or a `logical_input_id` already recorded under another version, is a **conflict** and changes nothing.
- **Verified.** Every artifact hash is checked. Staging is validated against the manifest's row counts and key uniqueness, and the dataset must be single-source.
- **Outbox.** The import commits a `dataset_imported` outbox event in the same transaction as the market data.
- **Failure keeps the previous state (F1).** Each version is one all-or-nothing transaction, so a refused version (tampered artifact, conflict, staging validation, out-of-order) leaves the last good version serving.

### Rebuild (F1, F2)
- **All or nothing.** One transaction takes the import lock, deletes `market`, and re-imports every published version and attempt. Any failure rolls back to the previous `market`.
- **DELETE, not TRUNCATE.** Readers keep seeing the previous rows until the commit. TRUNCATE would block them behind an exclusive lock for the whole rebuild.
- **History must match.** The deterministic event id is `uuid5(type:version:manifest_sha256)`. Each version's recorded `dataset_imported` event must match it and its payload (`dataset_version`, `manifest_sha256`, `logical_input_id`). The rebuild must also reach the newest version recorded in `app`. Any mismatch raises `IncompatibleHistory` and changes nothing.

### Recorded history (F2)
`app` records which content each version was, so an import never adopts an event that belongs to other content:
- **Missing event:** created `pending`.
- **Event for the same content:** kept as it is, including `done`.
- **Event for other content:** refused. The check runs before any artifact is read, for new and already-imported versions alike. Adopting such an event (formerly `ON CONFLICT DO NOTHING`) would mark unprocessed content as processed.

### Restore (F3)
`market.dataset_versions` is the ledger of imported versions, and every imported version needs its `dataset_imported` event. In the restore transaction, which also holds the import lock, after the `app` tables are loaded:
- **Missing event** (the backup predates the import): re-created `pending` with the same deterministic id and payload, so the version is still evaluated. Its effects were not in the backup either, so evaluating it again is correct, not a duplicate.
- **Event for other content:** the restore is refused.
- **Event for a version `market` has not imported:** the restore is refused. Run `db-import` first, then restore.

### Outbox
- **Claiming.** Consumers claim events with `FOR UPDATE SKIP LOCKED`, head-of-line by dataset version, so events are processed in order.
- **Atomic effects.** A handler's database effects commit in the same transaction that marks the event `done`. A handler error rolls back its effects, then counts an attempt and schedules a retry with exponential backoff. After the maximum number of attempts the event becomes `dead` and is reported on the Health tab.
- **Crash safety.** A crash after the import commits leaves a durable `pending` event. Alert evaluation is never lost.

### Consumer data contract (F4)
The serving tables (`observations`, `curve_points`) hold only the latest snapshot. A consumer that falls behind would read a later version's values, so consumers never read them:
- **Monitored values.** `market.monitored_values` holds, per `dataset_version`, the latest curve point for each curve and position, with its as-of date (price NULL for a gap). Migration 0003 creates it.
- **Written with the import.** The import appends a version's rows in the same transaction as the version.
- **Append-only.** An UPDATE trigger enforces it. Only a rebuild deletes the rows and re-derives the same values from the artifacts.
- **Consumer API.** `energy_curves.db.monitored.monitored_values(conn, dataset_version)` returns the values of the event's own version. It refuses an unknown version. It also refuses a version imported before migration 0003; run `db-import --rebuild` to fix that.
- **Evaluation pair.** Alert evaluation compares version `v` with version `v - 1`, both read through this API.

### Alerts (M3c; details in ADR-0015)
- **Upward crossing:** fires when `previous <= threshold` and `current > threshold`. It re-arms once a value is `<= threshold` again.
- **New rules** establish a baseline without firing.
- **Missing values** (gaps) neither fire nor re-arm.
- **What is evaluated.** Each changed **monitored value**: the rule's curve and position at the latest available date, as recorded for the event's own dataset version (see *Consumer data contract*). This is not gated on the global `price_changes` count, because a change in shape parameters moves curve prices without changing raw observations.
- **Silence.** Watermark-only updates leave monitored values unchanged, so they can never cross.
- **Atomicity.** Evaluation consumes the outbox event. Alert state, fired alerts and notification intents commit atomically with marking the event done.

### API and UI (M3b, M3d; M3b details in ADR-0014)
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
