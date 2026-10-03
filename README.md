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
# AWS Glue 5.1 compatibility (Python 3.11, Java 17, PySpark 3.5.6), in a separate environment:
UV_PROJECT_ENVIRONMENT=.venv-glue uv run --python 3.11 pytest -m ""
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

## Local serving stack (M3)

Postgres 17.11 and Valkey 9.1.2 run in podman compose. Both images are pinned by digest, and both ports bind to `127.0.0.1` only.

```bash
podman compose up -d                 # or: mise exec -- podman-compose up -d
uv run energy-curves db-migrate      # numbered, checksummed, locked migrations
uv run energy-curves db-import       # import published versions (ordered, idempotent, outbox event each)
uv run energy-curves db-status
uv run energy-curves db-backup backups/$(date +%F)   # Postgres-owned app schema
```

- `market` tables can be rebuilt from the published files at any time: `db-import --rebuild` (one transaction; a failure keeps the previous state).
- `app` tables (outbox; alert rules and history from M3c) exist only in Postgres. Back them up with `db-backup` and restore with `db-restore`. See [ADR-0013](docs/adr/0013-m3-serving-architecture.md).

**Local API.** `uv run energy-curves serve` serves on `http://127.0.0.1:8000`:
- endpoints: `/api/curves`, `/api/history/curves`, `/api/history/prices`, `/api/health`, the CSV exports under `/api/export/`, and live `dataset_updated` events at `/api/events`;
- responses are cached in Valkey by dataset version, and are still served from Postgres if Valkey is down;
- `db-import` notifies open event streams after each commit.

See [ADR-0014](docs/adr/0014-m3b-api-cache-events.md).

**Alerts.** Threshold rules fire when a curve position crosses upward, and re-arm once it is back at or below the threshold. New rules take a baseline instead of firing ([ADR-0013](docs/adr/0013-m3-serving-architecture.md), [ADR-0015](docs/adr/0015-m3c-alerts.md)).
- **Evaluation** runs after each `db-import`, from `energy-curves process-events`, and in the background of `serve`.
- **Endpoints:**
  - `/api/alerts/rules`: rule changes need the token from `/api/csrf` in `X-CSRF-Token`;
  - `/api/alerts`: the fired-alert history and a cursor;
  - `/api/alerts/events`: the alert stream, which resumes exactly after the last alert seen (`Last-Event-ID` or `?after=`).
- **Retention:** `energy-curves alerts-prune --keep-days N` deletes old alerts.

**UI.** Open `http://127.0.0.1:8000/` while `serve` runs. It has four tabs:
- **Curves:** the latest available curves.
- **History:** a table and a chart, with filters and CSV export.
- **Alerts:** rules and live alerts.
- **Health:** published versions, failed attempts and dead events.

The banner always reads "Modelled estimate, not market quotes". A **SYNTHETIC DATA** badge shows for synthetic data, and a notice appears when data is more than 4 days old. w2ui 2.0.0 is vendored, so the page loads nothing from other origins ([ADR-0016](docs/adr/0016-m3d-ui-replay-playwright.md)).

**Replay demo** (synthetic only, so use a separate store and database, not `data/`):
```bash
podman exec energy-curves_postgres_1 createdb -U ecp ecp_demo
DEMO=postgresql://ecp:ecp@127.0.0.1:5432/ecp_demo   # set it per command: mise's .env would win
DATABASE_URL=$DEMO uv run energy-curves db-migrate
DATABASE_URL=$DEMO uv run energy-curves serve &      # open http://127.0.0.1:8000/
DATABASE_URL=$DEMO uv run energy-curves replay --store /tmp/ecp-demo \
  --start 2024-02-01 --end 2024-02-29 --interval 5 --override RWTC=2024-02-14:99.99
```
Replay refuses a database or store that holds real data, so pointing it at the wrong one fails safely.

**Browser tests:** `uv run playwright install chromium` once, then `uv run pytest -m browser`.
