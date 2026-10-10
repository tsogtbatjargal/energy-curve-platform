# M4c: the first scheduled run (2026-10-10)

The schedule `ecp-batch-daily` (`cron(0 12 * * ? *)`, America/Toronto, enabled in plan 3) started the first run at 12:00:16 Toronto time. Read-only checks, repeated by an independent reviewer. The data is synthetic or derived from public EIA series; no account IDs, bucket names or addresses appear here.

## The execution
- **Name:** Scheduler passed a UUID (36 characters) through `<aws.scheduler.execution-id>`. It fits `^[A-Za-z0-9_-]{1,80}$` and differs from the manual run `m4c-first-20261009`. The documentation example shows a 16-character hex ID; the real format is a UUID, and the name equals the pipeline's `run_id`.
- **Universal target:** `StartExecution` accepted the `Name` in the input. No error, no retry, no extra IAM beyond `states:StartExecution` on the scheduler role.
- **Result:** `SUCCEEDED` in about 57 seconds (input `{}`, output `{"pipeline":{"exit_code":0}}`); the Stage and Pipeline states exited cleanly. The Lambda log shows one clean invocation.

## What it published
- **Version 2, watermark only.** The pipeline logged `status: published`, `dataset_version: 2`. The quality gate passed with 16 accepted and 0 rejected; the last observation of both series is 2026-10-09.
- **Merge counters:** `inserted 0`, `revised 0`, `price_changes 0`, `seen 16`. `unchanged` and `stale` are 0 by design: `seen` means "same price, later retrieval: watermark advanced, no revision", and all 16 rows are `seen` (`medallion.py`).
- **Independent reviewer, run 2 against run 1:** `gold/curves.parquet` and `gold/revisions.parquet` have identical SHA-256 hashes in both runs. Only `current.parquet` and the silver files differ, carrying the new retrieval time. `current.json` points at version 2.

## The alarm
`ecp-batch-failures` is `OK`. Its only state change is the first `INSUFFICIENT_DATA` to `OK` on 2026-10-08; it has not fired.

## Not shown by one run
That the execution name differs from day to day rests on Scheduler's documented per-invocation ID plus the difference from the manual name; it is confirmed on the second scheduled run.
