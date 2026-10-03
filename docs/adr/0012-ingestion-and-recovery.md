# 12. Ingestion, identity, and recovery (local)

Date: 2026-10-03 · Status: accepted

## Decision

**Parsing.** EIA returns numbers as JSON strings.
- A string matching `-?(0|[1-9]\d*)(\.\d+)?` parses to `Decimal`. Up to 6 decimal places are allowed; the Silver price type is `Decimal(18, 6)`.
- Rejected with a reason, never coerced: empty strings, `NA`, thousands separators, `NaN`, `Infinity`, exponents, signs other than a leading `-`, padding, floats, booleans and nulls.
- The same key twice in one batch with different prices is a conflict, and both rows are rejected.

**Quarantine.** Any rejected row quarantines the whole batch. The run still writes Bronze, Silver, the rejected rows and `quality.json`, but publishes nothing, so the last good dataset stays live.

**Calendar.** Weekends, holidays and a source with nothing new are not failures: such a run ends as `no_new_data`. A series whose last observation is more than 5 business days old produces a warning. Series past their `discontinued` date (EIA futures after 2024-04-05) are never expected.

**Identity.**
- `logical_input_id` hashes the source, the requests, every Bronze page hash, the transform version and the shape-parameter hash.
- `attempt_id` identifies one execution.
- Re-running a published logical input returns `already_published`.
- Data identical to what is already published produces no new version.
- Re-sending an older retrieval cannot revert a later revision.

**Revisions.** When a later accepted retrieval changes a key's price, the current value is replaced once. The old row moves to `gold_revisions` with `superseded_at_version` and `superseded_by_logical_input_id`.

**Publishing.** Artifacts are written atomically (temp file plus rename), in this order:
1. Bronze, then Silver, then Gold (and curves), all under `runs/<logical_input_id>/`.
2. `manifest.json`, recording the SHA-256 of every artifact.
3. `published/current.json`, replaced atomically.

Publishing requires `base_dataset_version` to equal the current version and the new version to be exactly one higher, so an older run can never move the dataset backward. Readers check every hash.

**Recovery.** A crash before the pointer is replaced leaves the previous dataset published and readable, and records a `failed` attempt. A retry recomputes and publishes. Tests inject crashes after Bronze, after the artifacts and after the manifest.

**Single writer.** Local runs take an exclusive `flock` on `data/.pipeline.lock`. A second run fails at once with `PipelineLocked` and writes nothing. The kernel releases the lock when the holder exits, including on SIGKILL, so it can never go stale. Tests use a separate process as the competing writer. Concurrency tests that need S3 conditional writes come in M4.

**Retries.** 429 and 5xx responses and transport errors are retried up to 5 attempts with full jitter (base 0.5 s, cap 8 s), honouring `Retry-After`. 401 and 403 fail at once, and so does any other 4xx.

**Pagination.** Requests sort by period, then series, and page by offset. A row appearing on two pages, a change in the reported total, or a row count different from the total all fail the fetch.

**Secrets.** The API key is sent only as a request parameter. It is removed from response bodies before they are stored or hashed (current API versions no longer echo it), and never appears in Bronze metadata or logs.
