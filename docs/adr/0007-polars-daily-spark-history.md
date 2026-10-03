# 7. Polars for the daily path, PySpark on Glue for history

Date: 2026-10-02 · Revised 2026-10-03 · Status: accepted

## Context
The daily batch is a few rows per series. The backfill and shape estimation cover the full EIA history: hundreds of thousands of rows. That is small for Spark, but it is the workload that would grow.

## Decision
- Polars runs the daily transforms and the reference shape estimation (`curves/shape.py`).
- PySpark on AWS Glue runs the historical backfill and must reproduce the shape parameters.
- A parity test runs both on the same input and compares them as sorted data: keys and `n_obs` exactly, and `s` within 1e-9 (ADR-0002).

## Compatibility target: AWS Glue 5.1

| Component | Version | Where it is pinned |
| --- | --- | --- |
| AWS Glue | **5.1** (`glue_version = "5.1"`) | Terraform Glue job (M5) |
| Apache Spark / PySpark | **3.5.6** | `pyproject.toml` dev dependency `pyspark==3.5.6` |
| Python (Glue job runtime) | **3.11** | `requires-python >= 3.11`; CI `glue-compat` job (`UV_PYTHON=3.11`) |
| Java | **17** | `mise.toml` (`temurin-17`) for local Spark |
| Scala | 2.12.18 | provided by Glue; no Scala code here |

Source: the AWS Glue versions table (checked 2026-10-03). Glue 5.1 is the default version for new jobs.

**Why not Glue 6.0** (Spark 4.1.1, Python 3.13, Scala 2.13)? It is a major Spark upgrade that removes EMRFS and the AWS SDK for Java v1. Staying on the Spark 3.5 line keeps local and Glue behaviour aligned for this project. Moving to 6.0 would be its own decision, with a full parity re-run.

**Why not Glue 5.0** (Spark 3.5.4)? 5.1 is the current default, on the same Spark 3.5 line.

**Rules:**
- **Environment.** The compatibility environment is the Glue runtime, not just its PySpark version. The CI `glue-compat` job installs Python **3.11** and Java 17 and syncs the locked dependencies, including `pyspark==3.5.6`. It then runs the whole test suite plus the `glue_compat` tests, which assert Python 3.11, PySpark and the Spark engine at 3.5.6, and Java 17, and compare a Spark and a Polars median of `ln(Ck/Spot)` within 1e-9. Run it locally with `UV_PROJECT_ENVIRONMENT=.venv-glue uv run --python 3.11 pytest -m glue_compat`.
- **Code.** The main package supports Python 3.11+ (developed on 3.12). Glue job code (`jobs/glue/`, M5) must import only PySpark and the standard library, not Polars.
- **Version updates.** Dependabot ignores PySpark *version* updates (major, minor and patch: 3.5.7–3.5.9 exist but Glue runs 3.5.6). PySpark changes only together with `glue_version`, in one PR that re-runs the parity test.
- **Security monitoring stays on.**
  - Dependabot alerts and security updates are enabled on the repository. They were found disabled on 2026-10-03 and turned on.
  - The ignore rule lists only version-update types.
  - The `dependency-audit` workflow scans `uv.lock` with trivy on every PR, on `main`, and weekly. Fixable HIGH or CRITICAL findings fail it.
  - A PySpark vulnerability fixed only after 3.5.6 forces an explicit decision: wait for a Glue release, mitigate, or change the target.

## Consequences
- Two implementations of shape estimation must stay in step; the parity test enforces it.
- At this data size Polars alone would do. Glue is the path that scales out.
