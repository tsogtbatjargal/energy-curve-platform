# 7. Polars for the daily path, PySpark on Glue for history

Date: 2026-10-02 · Status: accepted

## Context
The daily batch is a few rows per series. The backfill is the full EIA daily spot and futures history, about 20 series from the 1980s: hundreds of thousands of rows. That is small for Spark, but it is the workload that would grow.

## Decision
- Polars runs the daily transforms inside the Fargate task.
- PySpark on Glue runs the backfill and the seasonal-shape estimation.
- A parity test runs both implementations on the same fixture and requires identical Silver output and parameters.
- The local PySpark version is pinned to match the chosen Glue runtime; verify this in M5.

## Consequences
- Two implementations of the Silver transform must stay in step, and the parity test enforces that.
- At this data size Polars alone would be enough. Glue is the path that scales out.
