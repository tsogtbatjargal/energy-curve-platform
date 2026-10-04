# 20. M4: cloud entry points, synthetic-only data, and the batch image

Date: 2026-10-04 · Status: accepted

## Context
PLAN.md M4 runs the daily batch in AWS (ADR-0003). Step Functions calls a Lambda that fetches, then an ECS Fargate task that runs the Polars pipeline and publishes to S3, using the store and publishing rules of ADR-0019.

M4 runs on **synthetic data only** (user decision, 2026-10-04). Real-data cloud use, and any change to PLAN.md R3 / ADR-0011 it needs, is a separate later decision.

## Decision

### Two steps, one `run_id`
Step Functions passes its execution name as `run_id` to both steps. It is checked against `^[A-Za-z0-9_-]{1,80}$` before it becomes part of an S3 key.

1. **`cloud.lambda_handler` (Lambda) stages.** It fetches the day's pages and writes them unchanged under `staging/<run_id>/`, then `requests.json`, which lists every page with its SHA-256, request parameters and retrieval time. That file is written last, so its presence means the stage is complete.
2. **`cloud.task_main` (Fargate) replays.** `StagedSource` serves those pages back to the unchanged runner (`run_ingest_store`, ADR-0019):
   - each page's hash is verified;
   - a missing `requests.json` or a request that was not staged is refused.

   Because each page keeps its body and retrieval time, the run's logical input is exactly what one local run of the same fetch computes. So replays are idempotent: an exact replay is `already_published`.

**The daily window** is the spot series (RBRTE, RWTC) over the last 10 days. Late publications and revisions are picked up as in ADR-0012. EIA futures ended on 2024-04-05.

**Exit status:**
- `published`, `already_published` and `no_new_data` exit 0;
- **`quarantined` exits 3,** so the execution fails and the failure alarm fires (PLAN.md: "a CloudWatch alarm fires on a failed run").

### Synthetic only, by construction
- **The only source** the entry points construct is `SyntheticSource`.
- **`ECP_SOURCE`**, set to `synthetic` in the image, must be `synthetic`. Anything else raises `RefusedSource` before any write.
- **No key, no client.** No API key is read, and no `EiaClient` is created; a test makes constructing one fail.
- **Infrastructure follows.** The batch stack (M4c) will have no secret and no key input.

### One image for both steps (`Dockerfile`)
- **Base images:** the AWS Lambda Python 3.12 base (Python 3.12.15, the project's pinned version) with the Lambda runtime, and the uv image used only to export the lock. Both are pinned by their linux/amd64 digests.
- **Locked, verified dependencies:**
  - runtime dependencies come from `uv.lock` (`uv export --locked --no-dev`);
  - they are installed with `pip --require-hashes`, then the project itself;
  - uv and the build directory are removed.
- **How each step starts:**
  - Lambda uses the default `CMD` (`energy_curves.cloud.lambda_handler`);
  - the Fargate task definition overrides the entry point with `python -m energy_curves.cloud` and the command with `task <run_id>`.
- **`USER 1000:1000`.** Lambda always runs functions as its own unprivileged user, but Fargate runs the image's user. trivy's `DS-0002` (root user) flagged the first build.
- **The build context is an allowlist** (`.dockerignore`): `*`, then only `pyproject.toml`, `uv.lock` and `src/`. `data/` (real prices), `.env`, virtualenvs and plan files cannot enter the image. A test checks the allowlist and the digest pins.
- **x86_64, not ARM64.** The CI runners and the development machine are x86_64, so the image builds and is smoke-tested natively, without emulation. ARM64 Fargate would save about $0.05 a month at this usage, which is not worth an emulated build.
- **Size:** about 0.9 GB, mostly the Lambda base. ECR stores it for about $0.09 per image per month, and M4c keeps at most two images. The image also carries API-only dependencies (fastapi, uvicorn, redis, psycopg) that the batch doesn't use. That costs size and attack surface, not correctness; splitting the dependency groups would remove them, and that is tracked.

### Keeping the image current (added after review)
- **OS patches at build time.** The Dockerfile runs `dnf -y upgrade --releasever=latest`. Amazon Linux 2023 locks its repositories to the image's release, so a plain upgrade misses fixes published since.
  - Verified on 2026-10-04: the pinned base had 8 fixed HIGH vulnerabilities (`curl`/`libcurl`, `pcre2`, `rpm`).
  - A plain upgrade left all 8; `--releasever=latest` left 0.
  - The trade-off: the base layer's package versions now depend on the build date. The deployed artifact is identified exactly by its image digest.
- **The image is scanned.** The `image` job runs `trivy image` and fails on any fixed HIGH or CRITICAL vulnerability. `image` is a required check (user decision, 2026-10-04).
- **Dependabot watches both base digests.** Both `FROM` lines are `tag@sha256`, and the `docker` ecosystem in `.github/dependabot.yml` proposes new digests when either tag moves.

## Verification
- **`tests/test_cloud.py`** (both steps; S3 through moto, as in ADR-0019):
  - the staged replay gives the same logical input as one local run;
  - both steps end to end on S3, including the exact replay (`already_published`) and a later fetch, which gives a watermark-only version;
  - the source guard, with nothing written;
  - no EIA client and no key;
  - quarantine exits 3;
  - incomplete, tampered or unknown staging is refused;
  - run IDs, store URLs, the daily window, and the image hygiene checks.
- **Guard proofs.** Disabling the source guard, the staged-hash check, carrying the retrieval time through staging, or the quarantine exit code each fails its tests.
- **The image itself** is smoke-tested locally and in a new CI job, `image`:
  - the Lambda handler through the base image's runtime emulator;
  - then the Fargate task with `--network none`, which publishes version 1.

## Consequences
- **No runner changes.** The Lambda/Fargate split lives entirely in `cloud.py`.
- **Staging can be rewritten** when a Lambda is retried or overlaps another invocation for the same `run_id`.
  - This does not follow ADR-0019's write-once rule, and it fails safe: a mixed state gives a hash mismatch and a `StagingError` in the task, never a wrong publication.
  - `If-None-Match` on `requests.json` would also stop a legitimate retry from completing its stage, so it is not used.
- **Every daily run publishes a new dataset version, accepted for the synthetic demo** (user decision, 2026-10-04).
  - The retrieval time changes each day, so even with unchanged prices each run publishes a watermark-only version: `last_seen_at` moves.
  - That is about 365 versions a year, and `published_logical_ids` and the importer read every version record.
  - Cutting these versions would change idempotency rules that ADR-0012 and ADR-0019 settled; at this scale the cost is trivial. Revisit if the dataset ever runs on real data.
- **Orphaned staging prefixes.** A run that fails between the steps leaves `staging/<run_id>/`. M4c adds an S3 lifecycle rule for `staging/`.
- **Moving to real data later** means changing the source construction, adding the secret, and amending R3/ADR-0011. The synthetic-only tests would then fail, which is intended: the change has to be deliberate.
