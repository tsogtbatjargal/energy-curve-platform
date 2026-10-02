# 3. One job per AWS service

Date: 2026-10-02 · Status: accepted

## Context
Each service should do a job it suits, not appear only to tick a requirement box.

## Decision
| Service | Job | Why this service |
| --- | --- | --- |
| Lambda | Daily EIA fetch to Bronze | Seconds of HTTP I/O, no container needed |
| ECS Fargate task | Polars Silver/Gold transforms and curve build | Pinned container, more memory and time than Lambda, same image runs locally |
| Step Functions (Standard) | Orchestration, retries, failure state | Native synchronous integrations with Lambda, ECS, and the Redshift Data API |
| EventBridge Scheduler | Daily trigger | Managed cron with time zones |
| Glue (PySpark) | Full-history backfill, shape estimation | Distributed engine for history; on demand, not daily |
| Redshift Serverless | Historical analytics | Columnar SQL over history; billed only while active |

## Consequences
- More moving parts than a single container would need. That is accepted, because building and running each service is the point of the project.
- Polars handles the daily path. Spark is kept for history, where a scale-out engine is the right shape (see ADR-0007).
