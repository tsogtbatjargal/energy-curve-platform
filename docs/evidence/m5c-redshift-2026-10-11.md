# M5c: Redshift Serverless, the first load and session (2026-10-11)

The Glue job was hardened (M5c-0), the Redshift stack was applied in the workload account (M5c-1), and one Redshift session loaded the warehouse and ran the five queries (M5c-2). Everything below was read from the repository, the CI logs and the session's own output; identifiers are masked, and all data is synthetic (R3).

## M5c-0: the run-scoped Glue output
- **Change (#70):** a run writes only `m5/shape/<JOB_RUN_ID>/`, then `_complete.json` last; the job deletes nothing. The plan was the script object only (one update, 13 no-ops, 0 drift); applied, then fresh plan 0 changes.
- **The run:** `jr_4a055be4…` SUCCEEDED, 64 s, 129 DPU-seconds, no error, attempt 0.
- **Keys:** the run prefix held exactly `shape_params.csv/part-00000` (1,091 bytes), `shape_params.sha256/part-00000` (65) and `_complete.json` (466). The legacy pair was unchanged; no other key under `m5/`.
- **Record:** schema 1, run id equal to the run, `params_sha256` `4a111aa8…f253af4`, 48 rows, sizes equal to the objects, arguments equal to the job's, no account, bucket or URL; written after both outputs.
- **Parity:** the `.sha256` object equals that hash, 48 data rows, and `compare_params` against the Polars reference returned no differences.
- **Logs:** no AccessDenied or ERROR in this run's streams; `--JOB_RUN_ID` is no longer among the ignored arguments.

## M5c-1: the Redshift stack (#71)
- **Apply:** 18 creates from an empty state (plan SHA-256 `8b33e8f9…`), "18 added, 0 changed, 0 destroyed"; state serial 0 to 2. The plan check and the policy gate passed, and the wiring reference sets Terraform recorded matched what the plan check assumed.
- **Seven read-back entries:** the next plan showed 18 no-ops and 7 drift entries, all the provider's read-back: `tags` null to `{}` on seven resources, `inline_policy` empty to the role's own policy on each role, `log_exports` null to `[]` on the namespace. A refresh-only plan (SHA-256 `e714f38f…`) had exactly those entries; applied (serial 2 to 3); the next plan: 18 no-ops, 0 drift.
- **Verified from AWS (read-only):** workgroup AVAILABLE, base 8, max 8, private, enhanced VPC routing off, 3 subnets; namespace AVAILABLE with only `ecp-redshift-copy`; one usage limit (serverless-compute, daily, 4, deactivate); no internet gateway, NAT or default route; 0 rules on both security groups; both roles bounded; the service-linked role exists.
- **Admin exception:** the namespace reports AWS's default admin name `admin` (Security Hub RedshiftServerless.5). No password or secret was set, and there is no network path to a password login; access is IAM only through the Data API. Accepted by the owner and recorded in ADR-0023.
- **Budget read:** one `describe-budget` (management account): limit 40.0 USD a month, actual 0.0, no forecast.

## M5c-2: the session (#72)
- **Dry run** (22:27 Toronto, as the workload administrator; the loader prints its identity first: member account, not root): shape run `jr_4a055be4…`, hash `4a111aa83171…`, dataset version 4. Verified: record, sizes, the hash twice, 48 rows, the manifest and the three gold files, and exactly one listing match per COPY source. Rows: shape 48, observations 18, revisions 0, curve points 135.
- **Real load:** the same command with the run id. All four COPYs succeeded and the SELECT back verified the shape hash and the three row counts. **COPY works with no internet path** in this VPC (enhanced VPC routing off, no gateway, NAT or endpoint), so the fallback (enhanced VPC routing plus an S3 gateway endpoint and an outbound rule) is **not needed**.
- **Statements through the Data API:** `current_user` is `IAMR:ecp-redshift-data-api`; the superuser check returned false. The usage-view query (`SYS_SERVERLESS_USAGE`) was **denied** (`permission denied for relation`), as expected for a non-superuser; no GRANT was made. The cost was read from CloudWatch instead.
- **The five queries**, each run on Redshift and, on the exact files the loader verified, on a local Postgres loaded with the same DDL. The canonical result normalizes numbers, dates and floats (9 places); the hash is the first 16 hex digits of its SHA-256.

| Query | Rows | Result hash | Redshift = local |
|---|---|---|---|
| `latest_curve_per_series` | 15 | `e26ebb08742de0b2` | equal |
| `wti_brent_spread_by_tenor` | 45 | `5572203d555c3635` | equal |
| `contango_backwardation_days` | 2 | `ffd78e5b80948dab` | equal |
| `revisions_per_month` | 0 | `4f53cda18c2baa0c` | equal (trivially) |
| `rolling_percentile` | 45 | `28d307f30c6b95f0` | equal |

- **`ecp_revisions` is empty** (the live store has no revised prices), so `revisions_per_month` matches only trivially; it shows that the empty parquet loads and the query runs, not that revisions are counted correctly.
- **Cost (CloudWatch, `AWS/Redshift-Serverless`, workgroup dimension, 5-minute periods around the session):** `ComputeCapacity` 8 RPU in both periods; `ComputeSeconds` 71 and 20, 91 RPU-seconds if the periods add (CloudWatch describes the metric as accumulated over 30 minutes, so 71 if they overlap). At $0.4125 per RPU-hour that is about $0.01. With the 60-second minimum each time compute resumes (480 RPU-seconds, $0.055 at 8 RPU) the session's cost is bounded by about $0.11 for two resumes. Either way far below the $1.10 estimated for a 20-minute session, and well inside the M5 allotment. The usage limit took no action.

## State
The stack stays in place and idle: no compute runs between sessions and storage is under 1 GB. The loaded tables stay. A second session needs its own go. The Glue job's `s3:DeleteObject` narrowing remains deferred.

## Update (2026-10-16)
The stack described here was destroyed on 2026-10-16 (17 resources, verified read-only: no namespace, workgroup, snapshot or recovery point, and the 10.43 VPC gone). `AWSServiceRoleForRedshift` is kept, outside the state and present in IAM; a re-create needs an import of that role first. See the dated note in [ADR-0023](../adr/0023-m5-glue-backfill-and-redshift.md).
