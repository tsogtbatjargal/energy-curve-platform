# Energy curve platform: plan

Ingest public EIA oil prices, build short forward curves for WTI and Brent, publish them through a medallion pipeline on AWS, and serve them in a small live web app with alerting. Decisions are recorded in [docs/adr](adr/).

## Scope

**Data (EIA, free API key):**

- Daily spot: WTI Cushing, Brent Europe (current, USD/bbl).
- NYMEX WTI futures contracts 1–4, 1983 to 2024-04-05. EIA stopped publishing futures on that date, so they are used only to estimate curve shape.
- Backfill only: the full EIA daily spot and futures history (~20 series) for the PySpark and Redshift work.

**Curves (config-driven, one engine):**

| Curve | Anchor | Shape |
| --- | --- | --- |
| WTI | WTI spot | WTI futures history |
| Brent | Brent spot | WTI futures history (borrowed, labelled) |
| Brent − WTI | derived per tenor | — |

Tenors M0–M4 only, because the data supports nothing further. Method: [ADR-0002](adr/0002-curve-method.md).

**App tabs:** Curves (today's curves, with the as-of date and data age), History, Alerts (threshold rules, live toasts), Pipeline health (runs, freshness, rejected rows, lineage).

**Out of scope for the draft:** gasoline and diesel curves, tenors beyond M4, stochastic models, browser scraping, an always-on hosted app.

## Architecture

| Component | Job |
| --- | --- |
| Lambda | Daily EIA fetch → S3 Bronze (key redacted) |
| ECS Fargate task | Polars: Bronze → Silver → Gold, quality gate, curve build |
| Step Functions | Lambda → Fargate → Redshift load; retries, failure alarm |
| EventBridge Scheduler | Daily trigger |
| Glue (PySpark) | Historical backfill and seasonal-shape estimation, run on demand |
| Redshift Serverless | Curve and price history, analytical queries; on only for sessions |
| S3 | Bronze / Silver / Gold, run manifests, GitHub Pages snapshot source |
| Postgres | App state: runs, quality results, prices, curves, alerts (audit) |
| Redis | Response cache scoped to dataset version, Pub/Sub for alerts and updates |
| FastAPI + w2ui | API, server-sent events, UI |

**Profiles ([ADR-0004](adr/0004-network-profiles.md)):**

- **local:** app, Postgres and Redis run in podman compose; filesystem artifact store; offline fixtures.
- **batch (always on):** no VPC for Lambda; Fargate in public subnets with no inbound rules; S3 gateway endpoint; no NAT.
- **demo-day (about 48 h, then destroyed):**
  - API on Fargate behind an ALB.
  - RDS Postgres and ElastiCache Redis in private subnets.
  - One NAT gateway.
  - Bastion with no inbound ports, reached through SSM Session Manager.

## Data contract

- **Bronze:** the raw response with the API key redacted, request parameters, retrieval time, SHA-256 hash, and schema version.
- **Silver:** typed Parquet with `source, series_id, observation_date, price (decimal), unit, retrieved_at, run_id, raw_artifact_key`.
- **Gold:**
  - latest prices
  - price history
  - curve points: `curve_id, as_of_date, tenor, price, method_version, run_id`
- **Business key:** `(source, series_id, observation_date)`. Re-running the same input produces no duplicate rows and no repeated alerts.
- **Quality gate:** checks schema, units, non-finite values, duplicates, and freshness against the expected publication calendar. A failed batch is quarantined and the last good dataset stays live.

## Reliability and security

- Runs are idempotent (logical input ID separate from attempt ID); retries are bounded with jitter; artifacts are written before the manifest.
- Structured logs carry `run_id`; a CloudWatch alarm fires on a failed run.
- Identity: IAM Identity Center for people; GitHub OIDC role for CI; task and Lambda roles at runtime; root locked ([ADR-0005](adr/0005-identity-and-secrets.md)).
- Secrets: EIA key in Secrets Manager (cloud) or `.env` (local, git-ignored); a GitHub environment secret is used only by the opt-in live smoke test.
- Repo hygiene: gitleaks (pre-commit and CI), push protection, SHA-pinned actions, least-privilege `permissions:`, no `pull_request_target`.
- Policy as code: conftest + Rego against the `terraform plan` JSON, plus trivy ([ADR-0006](adr/0006-policy-as-code.md)).
- Cost: $40 AWS Budget with alerts at 50/80/100%; anything costing over about $1 is applied only with explicit approval.

## Milestones

| # | Deliverable | Hours |
| --- | --- | --- |
| M0 | Identity Center admin, budget, Terraform state bucket, GitHub OIDC role | 2–3 |
| M1 | CI (lint, Pytest, gitleaks, conftest, trivy, actionlint), Rego policies | 2–3 |
| M2 | EIA ingestion, Bronze/Silver/Gold on local filesystem, curve engine, tests | 6–8 |
| M3 | Postgres, Redis, FastAPI, SSE, w2ui tabs, alerts, replay CLI, Playwright | 6–8 |
| M4 | AWS batch: S3, Lambda, Fargate, Step Functions, Scheduler, alarm | 5–7 |
| M5 | Glue PySpark backfill and shape estimation, Polars parity test, Redshift load and queries | 5–7 |
| M6 | Demo-day stack, video, teardown; GitHub Pages snapshot | 4–6 |
| M7 | ADR review, runbook, mock interview and Q&A | 3–4 |

**Estimated AWS spend:** about $15–20 of the $40 budget.

- demo-day stack: about $7
- Redshift sessions: about $5
- Glue: about $2
- batch: about $1–2 per month

These are estimates; recheck against ca-central-1 prices before M4.

## Draft done when

- One command runs the offline demo locally.
- The AWS batch has succeeded at least once.
- The Glue backfill and a Redshift query have each run at least once.
- The demo-day stack has been recorded and destroyed.
- The GitHub Pages snapshot is live.
- CI is green.
