# energy-curve-platform

Builds short forward curves for WTI and Brent crude from public EIA data, publishes them through an S3 medallion pipeline on AWS (Lambda, Fargate, Step Functions, Glue, Redshift Serverless), and serves them in a small live web app with threshold alerts (FastAPI, Postgres, Redis Pub/Sub, w2ui).

Status: planning complete, implementation starting. See [docs/PLAN.md](docs/PLAN.md) and [docs/adr](docs/adr/).

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
