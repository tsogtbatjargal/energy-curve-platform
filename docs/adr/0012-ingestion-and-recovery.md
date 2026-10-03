# 12. Ingestion, identity, and recovery (local)

Date: 2026-10-03 · Status: accepted

## Decision

**Parsing.** EIA returns numbers as JSON strings.
- A string matching `-?(0|[1-9]\d*)(\.\d+)?` parses to `Decimal`. Up to 6 decimal places are allowed; the Silver price type is `Decimal(18, 6)`.
- Rejected with a reason, never coerced: empty strings, `NA`, thousands separators, `NaN`, `Infinity`, exponents, signs other than a leading `-`, padding, floats, booleans and nulls.
- The same key twice in one batch with different prices is a conflict, and both rows are rejected.

**Completeness.** Within each multi-series request, the dates on which the *other* requested series returned data are evidence that the source published. A series is **incomplete**, and the batch quarantined, if either:
- it lacks more of those dates than `max(3, 10%)`, or
- it returned nothing while the others returned at least 3 dates.

Dates after a series' `discontinued` date do not count. A request where nothing returned data is a calendar gap, not an incomplete batch. Single-series requests have no cross-evidence and rely on staleness warnings.

The thresholds come from real WTI/Brent history, where holiday differences peak at 2 days in 5–10 day windows, 3 in 30 days, and 5 (about 8%) in 90 days. Validated with 0 false positives over 6,167 rolling windows.

**One source per store.** A run refuses (`MixedSourceError`) to add observations from a different source than the store already holds, before writing anything. The curve engine and shape estimation also refuse mixed-source inputs, and shape parameters must have the same `origin` as the data.

**Quarantine.** Any rejected row, or an incomplete series, quarantines the whole batch. The run still writes Bronze, Silver, the rejected rows and `quality.json`, but publishes nothing, so the last good dataset stays live.

**Calendar.** Weekends, holidays and a source with nothing new are not failures: such a run ends as `no_new_data`. A series whose last observation is more than 5 business days old produces a warning. Series past their `discontinued` date (EIA futures after 2024-04-05) are never expected.

**Identity (content deduplication).**
- `logical_input_id` hashes the source, the requests, every Bronze page's hash **and retrieval time**, the transform version and the shape-parameter hash.
- `attempt_id` identifies one execution.
- Only an exact replay of the same retrieval (same content, same retrieval time) returns `already_published`.
- A later retrieval with identical content is a new input, because it is new evidence for ordering.

**Retrieval ordering (separate from deduplication).** Each Gold observation keeps two timestamps:
- `retrieved_at`: the retrieval that established the current price (lineage)
- `last_seen_at`: the latest retrieval that has seen the observation, whether or not the price changed

Each incoming row is compared with `last_seen_at`:

| Incoming retrieval | Price | Result |
| --- | --- | --- |
| later | different | **revised** (old row to `gold_revisions`) |
| later | same | `last_seen_at` advances only; no revision |
| not later | different | **stale**: current kept, quality warning |
| not later | same | unchanged |

So T1=50, T2=60, T3=50 ends at 50. And T1=50, T3=50, then a delayed T2=60 stays at 50, because T3 advanced `last_seen_at` past T2.

Runs report `merge.price_changes` (inserted + revised). A run that only advances `last_seen_at` publishes a new dataset version with `price_changes = 0`. Revisions and, from M3, alerts are driven only by price changes, so watermark-only runs produce neither.

Gold written before `last_seen_at` existed takes `retrieved_at` as its watermark. Both stored tables are then conformed to the exact `GOLD_SCHEMA` and `REVISION_SCHEMA` column order and types before any concat, because Polars concatenates by position. Tests cover pre-change stores with both empty and populated revision history.

**Completeness.** Within each multi-series request, the dates on which the *other* requested series returned data are evidence that the source published. A series is **incomplete**, and the batch quarantined, if either:
- it lacks more of those dates than `max(3, 10%)`, or
- it returned nothing while the others returned at least 3 dates.

Dates after a series' `discontinued` date do not count. A request where nothing returned data is a calendar gap, not an incomplete batch. Single-series requests have no cross-evidence and rely on staleness warnings.

The thresholds come from real WTI/Brent history, where holiday differences peak at 2 days in 5–10 day windows, 3 in 30 days, and 5 (about 8%) in 90 days. Validated with 0 false positives over 6,167 rolling windows.

**One source per store.** A run refuses (`MixedSourceError`) to add observations from a different source than the store already holds, before writing anything. The curve engine and shape estimation also refuse mixed-source inputs, and shape parameters must have the same `origin` as the data.

**Quarantine.** Any rejected row, or an incomplete series, quarantines the whole batch. The run still writes Bronze, Silver, the rejected rows and `quality.json`, but publishes nothing, so the last good dataset stays live.

**Calendar.** Weekends, holidays and a source with nothing new are not failures: such a run ends as `no_new_data`. A series whose last observation is more than 5 business days old produces a warning. Series past their `discontinued` date (EIA futures after 2024-04-05) are never expected.

**Identity (content deduplication).**
- `logical_input_id` hashes the source, the requests, every Bronze page's hash **and retrieval time**, the transform version and the shape-parameter hash.
- `attempt_id` identifies one execution.
- Only an exact replay of the same retrieval (same content, same retrieval time) returns `already_published`.
- A later retrieval with identical content is a new input, because it is new evidence for ordering.

**Revisions are ordered per observation, not per run.** A different price replaces the current value only if its `retrieved_at` is **strictly later** than the current row's. The old row then moves to `gold_revisions` with `superseded_at_version` and `superseded_by_logical_input_id`.

An older or equally old retrieval with a different price is **stale**: it is counted in `merge.stale`, reported as a quality warning, and the current value is kept. So replaying an older retrieval, even under a different window and therefore a different `logical_input_id`, cannot revert a correction.

**Publishing.** Artifacts are written atomically (temp file plus rename), in this order:
1. Bronze, then Silver, then Gold (and curves), all under `runs/<logical_input_id>/`.
2. `manifest.json`, recording the SHA-256 of every artifact.
3. `published/current.json`, replaced atomically.

Publishing requires `base_dataset_version` to equal the current version and the new version to be exactly one higher, so an older run can never move the dataset backward. Readers check every hash.

**Recovery.** A crash before the pointer is replaced leaves the previous dataset published and readable, and records a `failed` attempt. A retry recomputes and publishes. Tests inject crashes after Bronze, after the artifacts and after the manifest.

**Single writer.** Local runs take an exclusive `flock` on `data/.pipeline.lock`. A second run fails at once with `PipelineLocked` and writes nothing. The kernel releases the lock when the holder exits, including on SIGKILL, so it can never go stale. Tests use a separate process as the competing writer. Concurrency tests that need S3 conditional writes come in M4.

**Retries.** 429 and 5xx responses and transport errors are retried up to 5 attempts with full jitter (base 0.5 s, cap 8 s), honouring `Retry-After`. 401 and 403 fail at once, and so does any other 4xx.

**Pagination.** Requests sort by period, then series, and page by offset. A row appearing on two pages, a change in the reported total, or a row count different from the total all fail the fetch.

**Secrets.** The API key is sent only as a request parameter. It is removed from response bodies before they are stored or hashed (current API versions no longer echo it), and never appears in Bronze metadata.

Logging: httpx logs every request URL at INFO, key included. So the CLI configures logging through `logging_setup`:
- `httpx` and `httpcore` are held at WARNING.
- A filter on every root handler replaces `api_key=` values, the key itself, and exception text.

A test runs the real CLI in a subprocess against a local fake EIA server with a fake key, and checks both the logs and every stored file.
