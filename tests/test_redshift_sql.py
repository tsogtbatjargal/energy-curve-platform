"""M5c-2 (ADR-0023): the Redshift SQL. DDL and the five queries are shared with the local Postgres
and run there on synthetic data; the Redshift-only COPY file is parsed with sqlglot; nothing here
reaches AWS."""

import re
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
import pytest
import sqlglot
from db_support import three_version_store
from sqlglot import exp

from energy_curves.curves.shape_core import POSITIONS
from energy_curves.pipeline.publish import load_published, read_artifact
from energy_curves.storage.artifacts import LocalArtifactStore

SQL = Path(__file__).parents[1] / "sql" / "redshift"
TABLES = (
    "ecp_shape_params", "ecp_observations", "ecp_revisions", "ecp_curve_points", "ecp_load_log",
)  # fmt: skip
ROLE = "arn:aws:iam::123456789012:role/ecp-redshift-copy"


def named(path: Path) -> dict[str, str]:
    """`-- name: x` sections of a SQL file, each one statement."""
    parts = re.split(r"(?m)^-- name: (\w+)\s*$", path.read_text())
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def statements(path: Path) -> list[str]:
    text = re.sub(r"(?m)^--.*$", "", path.read_text())
    return [s.strip() for s in text.split(";") if s.strip()]


def test_the_sql_folder_holds_exactly_these_files() -> None:
    assert sorted(p.name for p in SQL.iterdir()) == ["copy.sql", "ddl.sql", "queries.sql"]


# --- the DDL: ecp_* tables in public, nothing else -----------------------------------------------


@pytest.mark.parametrize("dialect", ["postgres", "redshift"])
def test_the_ddl_parses_in_both_dialects_and_creates_only_ecp_tables_in_public(
    dialect: str,
) -> None:
    created = []
    for statement in statements(SQL / "ddl.sql"):
        tree = sqlglot.parse_one(statement, read=dialect)
        assert isinstance(tree, exp.Create), statement
        table = tree.this.this
        assert table.args.get("db").name == "public" and table.name.startswith("ecp_"), statement
        created.append(table.name)
    assert created == list(TABLES)


def test_the_ddl_is_idempotent_and_has_no_admin_or_grant_statements() -> None:
    text = (SQL / "ddl.sql").read_text().upper()
    assert text.count("CREATE TABLE IF NOT EXISTS") == len(TABLES)
    for forbidden in ("CREATE SCHEMA", "GRANT", "CREATE USER", "ALTER USER", "PASSWORD", "DROP "):
        assert forbidden not in text, forbidden


# --- the five queries: shared, parsed in both dialects, over ecp_* tables only --------------------

QUERIES = ["latest_curve_per_series", "wti_brent_spread_by_tenor", "contango_backwardation_days",
           "revisions_per_month", "rolling_percentile"]  # fmt: skip


def test_there_are_exactly_the_five_adr_queries() -> None:
    assert list(named(SQL / "queries.sql")) == QUERIES


@pytest.mark.parametrize("dialect", ["postgres", "redshift"])
@pytest.mark.parametrize("name", QUERIES)
def test_each_query_parses_in_both_dialects_and_reads_only_ecp_tables(
    name: str, dialect: str
) -> None:
    sql = named(SQL / "queries.sql")[name].rstrip(";")
    tree = sqlglot.parse_one(sql, read=dialect)
    assert isinstance(tree, exp.Select)
    tables = {t.name for t in tree.find_all(exp.Table)}
    assert tables and all(t.startswith("ecp_") for t in tables), tables
    assert not list(tree.find_all(exp.Command))


def test_the_rolling_percentile_is_one_window_function() -> None:
    tree = sqlglot.parse_one(named(SQL / "queries.sql")["rolling_percentile"].rstrip(";"))
    assert len(list(tree.find_all(exp.Window))) == 1


# --- the Redshift-only file: COPY from full keys, parsed with sqlglot -----------------------------

URIS = {
    "shape_uri": "s3://ecp-glue-123456789012-ca-central-1/m5/shape/jr_" + "a" * 64
    + "/shape_params.csv/part-00000",
    "observations_uri": "s3://ecp-data-123456789012-ca-central-1/store/runs/x/y/gold/current.parquet",
    "revisions_uri": "s3://ecp-data-123456789012-ca-central-1/store/runs/x/y/gold/revisions.parquet",
    "curves_uri": "s3://ecp-data-123456789012-ca-central-1/store/runs/x/y/gold/curves.parquet",
}  # fmt: skip
COPIES = {
    "copy_shape_params": ("ecp_shape_params", "shape_uri"),
    "copy_observations": ("ecp_observations", "observations_uri"),
    "copy_revisions": ("ecp_revisions", "revisions_uri"),
    "copy_curve_points": ("ecp_curve_points", "curves_uri"),
}


def test_the_copy_file_has_exactly_the_four_copies() -> None:
    assert list(named(SQL / "copy.sql")) == list(COPIES)


@pytest.mark.parametrize("name", COPIES)
def test_each_copy_parses_as_a_copy_of_one_full_key_with_the_role(name: str) -> None:
    table, uri_key = COPIES[name]
    sql = named(SQL / "copy.sql")[name].format(role_arn=ROLE, **URIS)
    tree = sqlglot.parse_one(sql.rstrip(";"), read="redshift")
    assert isinstance(tree, exp.Copy)
    assert tree.this.name == table and tree.this.args["db"].name == "public"
    (file,) = tree.args["files"]
    assert file.name == URIS[uri_key] and not file.name.endswith("/")
    assert f"IAM_ROLE '{ROLE}'" in sql
    if name == "copy_shape_params":
        assert "CSV" in sql.upper() and "IGNOREHEADER 1" in sql.upper()
    else:
        assert "PARQUET" in sql.upper()


@pytest.mark.parametrize("name", COPIES)
def test_a_copy_names_no_manifest_credentials_prefix_wildcard_or_region(name: str) -> None:
    sql = named(SQL / "copy.sql")[name].upper()
    banned = ("MANIFEST", "ACCESS_KEY", "SECRET_ACCESS_KEY", "CREDENTIALS", "SESSION_TOKEN", "%",
              "*", "REGION", "COMPUPDATE", "STATUPDATE", "TRUNCATECOLUMNS")  # fmt: skip
    for forbidden in banned:
        assert forbidden not in sql, forbidden


def test_the_copy_placeholders_are_exactly_the_role_and_four_uris() -> None:
    found = set(re.findall(r"\{(\w+)\}", (SQL / "copy.sql").read_text()))
    assert found == {"role_arn", *URIS}


# --- the shared SQL on the local Postgres, with synthetic data ------------------------------------

pg = pytest.mark.postgres


@pytest.fixture
def warehouse(pg_dsn: str, tmp_path: Path) -> tuple[psycopg.Connection, dict[str, pl.DataFrame]]:
    """The DDL applied to a fresh database and the three-version synthetic store loaded."""
    root = three_version_store(tmp_path / "store")
    store = LocalArtifactStore(root)
    published = load_published(store)
    assert published.manifest is not None
    frames = {
        "ecp_observations": published.current,
        "ecp_revisions": published.revisions,
        "ecp_curve_points": read_artifact(store, published.manifest, "gold_curves").with_columns(
            pl.col("position").cast(pl.Utf8)
        ),
    }
    conn = psycopg.connect(pg_dsn, autocommit=True)
    conn.execute((SQL / "ddl.sql").read_text())
    for table, frame in frames.items():
        columns = ", ".join(f'"{c}"' for c in frame.columns)
        marks = ", ".join(["%s"] * frame.width)
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO public.{table} ({columns}) VALUES ({marks})",  # noqa: S608
                frame.rows(),
            )
    return conn, frames


def run(conn: psycopg.Connection, name: str) -> list[tuple[Any, ...]]:
    return conn.execute(named(SQL / "queries.sql")[name].rstrip(";")).fetchall()


@pg
def test_the_ddl_applies_twice_and_creates_the_four_tables_plus_the_log(pg_dsn: str) -> None:
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        for _ in range(2):
            conn.execute((SQL / "ddl.sql").read_text())
        found = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' ORDER BY 1"
        ).fetchall()
    assert [r[0] for r in found] == sorted(TABLES)


@pg
def test_the_ddl_column_order_is_the_parquet_and_csv_order(pg_dsn: str, warehouse) -> None:  # type: ignore[no-untyped-def]
    """COPY maps by position, so the columns must follow the files' column order exactly."""
    conn, frames = warehouse
    for table, frame in frames.items():
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        assert [c[0] for c in cols] == frame.columns, table
    shape = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'ecp_shape_params'"
        " ORDER BY ordinal_position"
    ).fetchall()
    assert [c[0] for c in shape] == ["position", "month", "s", "n_obs"]


@pg
def test_latest_curve_per_series(warehouse) -> None:  # type: ignore[no-untyped-def]
    conn, frames = warehouse
    points = frames["ecp_curve_points"].filter(pl.col("status") == "ok")
    latest = points.group_by("curve_id").agg(pl.col("as_of_date").max().alias("d"))
    want = (
        points.join(latest, on="curve_id")
        .filter(pl.col("as_of_date") == pl.col("d"))
        .select("curve_id", "as_of_date", "position", "price")
        .sort("curve_id", "position")
        .rows()
    )
    got = run(conn, "latest_curve_per_series")
    assert got == want and {r[0] for r in got} == {"BRENT", "BRENT_WTI", "WTI"}
    assert {r[2] for r in got} == {"Spot", *POSITIONS}


@pg
def test_wti_brent_spread_by_tenor(warehouse) -> None:  # type: ignore[no-untyped-def]
    conn, frames = warehouse
    pts = frames["ecp_curve_points"].filter(pl.col("status") == "ok")
    wti = pts.filter(pl.col("curve_id") == "WTI").select("as_of_date", "position", "price")
    brent = pts.filter(pl.col("curve_id") == "BRENT").select("as_of_date", "position", "price")
    want = (
        brent.join(wti, on=["as_of_date", "position"], suffix="_w")
        .select("as_of_date", "position", (pl.col("price") - pl.col("price_w")).alias("spread"))
        .sort("as_of_date", "position")
        .rows()
    )
    got = run(conn, "wti_brent_spread_by_tenor")
    assert got == want and len(got) > 0


@pg
def test_contango_and_backwardation_days(warehouse) -> None:  # type: ignore[no-untyped-def]
    conn, frames = warehouse
    pts = frames["ecp_curve_points"].filter(
        (pl.col("status") == "ok") & (pl.col("kind") == "outright")
    )
    spot = pts.filter(pl.col("position") == "Spot").select("curve_id", "as_of_date", "price")
    c4 = pts.filter(pl.col("position") == "C4").select("curve_id", "as_of_date", "price")
    both = spot.join(c4, on=["curve_id", "as_of_date"], suffix="_c4")
    want = (
        both.group_by("curve_id")
        .agg(
            (pl.col("price_c4") > pl.col("price")).sum().alias("contango"),
            (pl.col("price_c4") < pl.col("price")).sum().alias("backwardation"),
        )
        .sort("curve_id")
        .rows()
    )
    got = [(c, int(a), int(b)) for c, a, b in run(conn, "contango_backwardation_days")]
    assert got == want and sum(a + b for _, a, b in got) > 0


@pg
def test_revisions_per_month(warehouse) -> None:  # type: ignore[no-untyped-def]
    conn, frames = warehouse
    revs = frames["ecp_revisions"]
    assert revs.height > 0  # the third version corrects 10 January
    want = (
        revs.with_columns(pl.col("observation_date").dt.strftime("%Y-%m").alias("m"))
        .group_by("m", "series_id")
        .len()
        .sort("m", "series_id")
        .rows()
    )
    got = [(m, s, int(n)) for m, s, n in run(conn, "revisions_per_month")]
    assert got == want


@pg
def test_rolling_percentile_ranks_each_price_within_its_curve_position_and_month(
    warehouse,
) -> None:  # type: ignore[no-untyped-def]
    conn, frames = warehouse
    got = run(conn, "rolling_percentile")
    assert got and all(0 <= float(r[-1]) <= 1 for r in got)
    # the lowest price of each partition ranks 0 and the highest 1
    pts = frames["ecp_curve_points"]
    one = pts.filter(
        (pl.col("curve_id") == "WTI") & (pl.col("position") == "C1") & (pl.col("status") == "ok")
        & (pl.col("as_of_date").dt.month() == 1)
    ).sort("price")  # fmt: skip
    ranks = {r[2]: float(r[-1]) for r in got if r[0] == "WTI" and r[1] == "C1" and r[2].month == 1}
    assert ranks[one["as_of_date"][0]] == 0.0 and ranks[one["as_of_date"][-1]] == 1.0
    assert date(2024, 1, 31) >= one["as_of_date"][0]
