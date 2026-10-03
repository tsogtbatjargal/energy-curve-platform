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
| Python (Glue job runtime) | **3.11** | Glue job code; CI parity job (M5) |
| Java | **17** | `mise.toml` (`temurin-17`) for local Spark |
| Scala | 2.12.18 | provided by Glue; no Scala code here |

Source: the AWS Glue versions table (checked 2026-10-03). Glue 5.1 is the default version for new jobs.

**Why not Glue 6.0** (Spark 4.1.1, Python 3.13, Scala 2.13)? It is a major Spark upgrade that removes EMRFS and the AWS SDK for Java v1. Staying on the Spark 3.5 line keeps local and Glue behaviour aligned for this project. Moving to 6.0 would be its own decision, with a full parity re-run.

**Why not Glue 5.0** (Spark 3.5.4)? 5.1 is the current default, on the same Spark 3.5 line.

**Rules:**
- The main package runs on Python 3.12 locally. Code shipped to Glue (`jobs/glue/`, M5) must run on **Python 3.11** and import only PySpark and the standard library, not Polars. CI will run the parity test under Python 3.11 with Java 17 and `pyspark==3.5.6`.
- Dependabot ignores `pyspark` completely: patch releases (3.5.7–3.5.9 exist) would diverge from Glue too. PySpark changes only together with `glue_version`, in one PR that re-runs the parity test.

## Consequences
- Two implementations of shape estimation must stay in step; the parity test enforces it.
- At this data size Polars alone would do. Glue is the path that scales out.
