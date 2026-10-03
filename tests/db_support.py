"""Per-test Postgres databases. Tests needing Postgres take the `pg_dsn` fixture (conftest)."""

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ADMIN_DSN = os.environ.get("DATABASE_URL", "")


def fresh_database() -> Iterator[str]:
    if not ADMIN_DSN:
        if os.environ.get("ECP_REQUIRE_POSTGRES"):
            pytest.fail("ECP_REQUIRE_POSTGRES is set but DATABASE_URL is not")
        pytest.skip("DATABASE_URL not set (start it with: podman compose up -d)")
    name = f"ecp_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    except psycopg.OperationalError as exc:
        if os.environ.get("ECP_REQUIRE_POSTGRES"):
            raise
        pytest.skip(f"Postgres unreachable: {exc}")
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield make_conninfo(**{**conninfo_to_dict(ADMIN_DSN), "dbname": name})
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


# --- synthetic published stores for importer tests ---------------------------------------------

from datetime import UTC, date, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from energy_curves.cli import load_shape  # noqa: E402
from energy_curves.ingestion.synthetic import SyntheticSource  # noqa: E402
from energy_curves.pipeline.runner import FetchRequest, run_ingest  # noqa: E402

T1 = datetime(2026, 3, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 2, tzinfo=UTC)
T3 = datetime(2026, 3, 3, tzinfo=UTC)
JAN = [FetchRequest(("RBRTE", "RWTC"), date(2024, 1, 1), date(2024, 1, 31))]
FEB = [FetchRequest(("RBRTE", "RWTC"), date(2024, 2, 1), date(2024, 2, 29))]


def ingest(store: Path, requests, **source_kw):  # type: ignore[no-untyped-def]
    shape = load_shape(store, "synthetic")  # packaged synthetic parameters
    return run_ingest(
        store, SyntheticSource(**source_kw), requests, source_name="synthetic", shape=shape
    )


def three_version_store(store: Path) -> Path:
    """v1 January, v2 February, v3 a later correction to 10 January."""
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, FEB, retrieved_at=T1)
    ingest(store, JAN, retrieved_at=T2, overrides={("RWTC", date(2024, 1, 10)): "99.99"})
    return store


def weekdays(start: date, end: date) -> int:
    """Business days in [start, end]; the synthetic source publishes every weekday."""
    from datetime import timedelta

    return sum(
        1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5
    )


JAN_ROWS = 2 * weekdays(date(2024, 1, 1), date(2024, 1, 31))  # two series
FEB_ROWS = 2 * weekdays(date(2024, 2, 1), date(2024, 2, 29))
