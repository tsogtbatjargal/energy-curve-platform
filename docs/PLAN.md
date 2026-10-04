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

Points are labelled `Spot, C1–C4` (contract positions). Real data supports nothing further, so the curves stop there. Every point is a modelled estimate. Method: [ADR-0002](adr/0002-curve-method.md).

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
| Postgres 17 | `market` schema (rebuildable from published artifacts: versions, prices, revisions, curves, attempts) and `app` schema (owned by Postgres: outbox, alert rules/state/history, notifications; backed up separately) — [ADR-0013](adr/0013-m3-serving-architecture.md) |
| Valkey (Redis-compatible) | Response cache scoped to dataset version, Pub/Sub for alerts and updates |
| FastAPI + w2ui | API, server-sent events, UI |

**Profiles ([ADR-0004](adr/0004-network-profiles.md)):**

- **local:** app, Postgres and Valkey run in podman compose (ports bound to 127.0.0.1); filesystem artifact store; offline fixtures.
- **batch (always on):** no VPC for Lambda; Fargate in public subnets with no inbound rules; S3 gateway endpoint; no NAT.
- **demo-day (about 48 h, then destroyed):**
  - API on Fargate behind an ALB.
  - RDS Postgres 17 and ElastiCache Valkey 9.1 in private subnets.
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

## Requirements before workload deployment

These block the first `apply` of any stack other than bootstrap (M4 onward). They are tracked here so they cannot be quietly dropped.

**R1. IAM policies unknown at plan time block deployment unless reviewed and approved before apply.**
*Status: implemented ([ADR-0017](adr/0017-iam-approval-fingerprint-gate.md)): `scripts/iam_approval.py`, run by `scripts/policy_gate.py --stack <name>`; approvals in `policy/approvals/iam_unknown.json`.*
A check after deployment would find unsafe permissions only once they are live, so it cannot be the gate. Before M4 applies:
- For workload stacks, an IAM policy whose JSON is unknown at plan time is a gate **failure**, not a warning.
- **The only exception** is an approval committed through a reviewed PR in `policy/approvals/iam_unknown.json`, recording:
  - stack and resource address
  - a **fingerprint** of the policy, its dependencies and its inputs (below)
  - reviewer, approval date and an expiry date, at most 30 days later
  - reason

  The gate recomputes the fingerprint on every plan. Any mismatch, or an expired approval, fails the gate.

**Why the policy expression alone is not enough.** A probe of `terraform show -json` (Terraform 1.15) found:
- A policy set from a data source shows up only as a reference, e.g. `["data.aws_iam_policy_document.p.json"]`, so the policy's statements are not in that expression.
- `locals` are absent from the plan JSON.
- Literal values inside function calls are absent too: for `jsonencode({... Action = [...] ...})`, only the references survive.

So a hash of the plan JSON alone would not change when the actions changed.

**The fingerprint** is the SHA-256 of a canonical JSON document containing:
1. **Identity:** stack, resource address, resource type, attribute name, and the fingerprint format version.
2. **Dependency closure from the plan JSON.** Start at the policy attribute's `references` and follow them transitively through `configuration`. Include the full `expressions` of every resource and data source reached (for example the `aws_iam_policy_document` statements), and every module call reached (`source`, `version_constraint`, input `expressions`).
3. **Inputs:**
   - the plan's `variables` values for every `var.*` in the closure
   - each module call's input expressions
   - the exact resolved version of every registry module in the closure, from `.terraform/modules/modules.json`
4. **Source:** the SHA-256 of every `*.tf` and `*.tf.json` file in the stack root and in every local module directory in the closure, plus `.terraform.lock.hcl`. This covers locals, literals inside function calls, and provider versions, which the plan JSON omits. It is deliberately conservative: any edit to the stack's Terraform invalidates its approvals, and re-approving is cheap.

**Gate rules beyond the fingerprint:**
- A **sensitive** variable anywhere in the closure fails the gate outright. IAM policies must not depend on secrets, and their values are not hashed into approvals.
- Every unknown value in the closure must be an `arn`, `id` or `name` attribute of a resource in the same stack. The check uses the plan's `relevant_attributes`; anything else fails.
- Plan JSON can hold sensitive values in plain text (`variables`), so plans and fingerprint inputs are never uploaded as CI artifacts or written to logs.

**Reviewer checklist:** read the source diff for the closure files. Actions and principals are constants, there is no `*` action, and there is no `*` resource paired with a write action.

**Approval flow:** the approval PR is reviewed and merged first. Then the apply job runs in the protected `prod` environment, which needs a reviewer's approval before any change is made. Post-apply checks (re-plan, Access Analyzer `validate-policy`) are optional defence in depth, not the gate.

**Tests.** Each case below changes one input and must fail the gate, unless marked "passes":
- an unknown policy with no approval
- the matching fingerprint (passes)
- a changed `aws_iam_policy_document` statement
- a changed local
- a changed literal inside `jsonencode`
- a changed module source or resolved module version
- a changed input variable value feeding the policy
- a changed provider lock
- an edit to any file in the stack (conservative by design)
- a sensitive variable in the closure, even with an approval
- an unknown value that is not a same-stack `arn`, `id` or `name`
- an expired approval
- an approval recorded for a different stack or address

**R2. A permissions boundary on everything the deploy role creates (ADR-0010).**
- A managed policy `ecp-workload-boundary` exists.
- `ecp-gha-deploy` may call `iam:CreateRole`, `iam:PutRolePermissionsBoundary`, `iam:AttachRolePolicy` and `iam:PutRolePolicy` only when `iam:PermissionsBoundary` equals that policy, and may never edit or delete the boundary.
- A Rego rule denies any `aws_iam_role` in the `batch` or `demo` stacks without `permissions_boundary`.
- `aws iam simulate-principal-policy` shows that creating a role without the boundary is denied, and so is attaching `AdministratorAccess`.

**R3. Third-party price data stays out of public outputs until redistribution rights are confirmed (ADR-0011).**
- **Sources:** EIA serves WTI and Brent spot from Refinitiv (an LSEG business) and futures from NYMEX (CME Group). EIA's reuse policy excludes material licensed from third parties, and no redistribution grant was found.
- **Rules until rights are confirmed:**
  - No real price values in the public repo. Test fixtures copy EIA's response *structure* but use synthetic values.
  - Real data lives only in a git-ignored local cache.
  - Before M6 publishes the GitHub Pages snapshot, decide what may be shown publicly: synthetic data, derived parameters only, or confirmed-licensed data.

## Milestones

| # | Deliverable | Hours |
| --- | --- | --- |
| M0 | Identity Center admin, budget, Terraform state bucket, GitHub OIDC role | 2–3 |
| M1 | CI (lint, Pytest, gitleaks, conftest, trivy, actionlint), Rego policies | 2–3 |
| M2 | EIA ingestion, Bronze/Silver/Gold on local filesystem, curve engine, tests | 6–8 |
| M3 | Four PRs reviewed one at a time (ADR-0013). M3a: compose, migrations, importer, outbox, backup/restore. M3b: API, cache, SSE. M3c: crossing alerts via the outbox. M3d: w2ui, replay, Playwright (including mobile smoke) | 20–28 (revisable) |
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
