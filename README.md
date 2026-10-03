# energy-curve-platform

Builds short forward curves for WTI and Brent crude from public EIA data, publishes them through an S3 medallion pipeline on AWS (Lambda, Fargate, Step Functions, Glue, Redshift Serverless), and serves them in a small live web app with threshold alerts (FastAPI, Postgres, Redis Pub/Sub, w2ui).

Status: M0–M1 (AWS bootstrap, CI and policy gates) done; M2 (local pipeline and curve engine) in review. See [docs/PLAN.md](docs/PLAN.md) and [docs/adr](docs/adr/).

## Setup

Requires [mise](https://mise.jdx.dev) and podman (or Docker).

```bash
mise install                  # pinned Python, uv, Terraform, OPA, conftest, gitleaks, trivy, Java
cp .env.example .env          # add your EIA API key; .env is git-ignored
uv sync
uv run pre-commit install
uv run pytest
```

Get a free EIA API key at <https://www.eia.gov/opendata/register.php>.

## Offline demo (synthetic data, no API key)

Every price in this demo is synthetic (ADR-0011), and every curve point is a modelled estimate, not a market quote.

```bash
export DATA_DIR=$(mktemp -d)
uv run energy-curves ingest --source synthetic --start 2014-01-01 --end 2024-04-05 --with-futures
uv run energy-curves estimate-shape
uv run energy-curves ingest --source synthetic --start 2024-04-06 --end 2024-04-30
uv run energy-curves verify-shape
uv run energy-curves export-curves --as-of 2024-04-30
```

To use real EIA data, run the same commands with `--source eia`. This needs `EIA_API_KEY` set, and the data is written to the git-ignored `data/` folder. Real prices are third-party data and are never committed.
