import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest
import redis
from db_support import FEB, JAN, T1, T2, ingest, weekdays
from fastapi.testclient import TestClient

from energy_curves.api import app as api_app
from energy_curves.api import queries
from energy_curves.api.cache import VersionCache
from energy_curves.db import importer as imp
from energy_curves.db.migrate import migrate
from energy_curves.pipeline.publish import read_artifact, read_manifest
from energy_curves.storage.artifacts import LocalArtifactStore

pytestmark = pytest.mark.postgres
TODAY = date(2024, 3, 10)
BASE = "http://127.0.0.1:8000"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """v1 January, v2 February; nothing imported yet."""
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, FEB, retrieved_at=T1)
    return store


@pytest.fixture
def db(pg_dsn: str) -> str:
    migrate(pg_dsn)
    return pg_dsn


@pytest.fixture
def cache(redis_url: str) -> Iterator[VersionCache]:
    prefix = f"ecp-test-{uuid.uuid4().hex[:8]}"
    client = redis.Redis.from_url(redis_url)
    yield VersionCache(client, prefix=prefix)
    for key in client.scan_iter(f"{prefix}:*"):
        client.delete(key)


def make_client(db: str, cache: VersionCache) -> TestClient:
    app = api_app.create_app(
        database_url=db, cache=cache, redis_url=None, port=8000, today=lambda: TODAY
    )
    return TestClient(app, base_url=BASE)


def import_first(db: str, store: Path) -> None:
    s = LocalArtifactStore(store)
    with psycopg.connect(db) as conn:
        imp.import_version(conn, s, imp.published_versions(s)[0])


def latest_points(store: Path, version: int) -> dict[tuple[str, str], tuple[str, str]]:
    """Oracle from the version's own artifact: (curve, position) -> (as_of, price)."""
    s = LocalArtifactStore(store)
    record = imp.published_versions(s)[version - 1]
    curves = read_artifact(s, read_manifest(s, record), "gold_curves")
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for r in curves.sort("as_of_date").iter_rows(named=True):
        out[(r["curve_id"], str(r["position"]))] = (r["as_of_date"].isoformat(), str(r["price"]))
    return out


def as_points(body: dict) -> dict[tuple[str, str], tuple[str, str]]:  # type: ignore[type-arg]
    return {
        (c["curve_id"], p["position"]): (p["as_of"], p["price"])
        for c in body["curves"]
        for p in c["points"]
    }


def test_curves_carry_version_as_of_age_and_estimate_type(
    db: str, store: Path, cache: VersionCache
) -> None:
    imp.import_pending(db, store)
    r = make_client(db, cache).get("/api/curves")
    assert r.status_code == 200
    body = r.json()
    assert body["dataset_version"] == 2 and r.headers["X-Dataset-Version"] == "2"
    assert body["label"] == "latest available"
    assert body["source"] == "synthetic" and body["synthetic"] is True
    assert body["disclaimer"] == queries.DISCLAIMER
    assert [c["curve_id"] for c in body["curves"]] == ["BRENT", "BRENT_WTI", "WTI"]
    for curve in body["curves"]:
        assert curve["as_of"] == "2024-02-29"
        assert curve["data_age_days"] == (TODAY - date(2024, 2, 29)).days
        assert [p["position"] for p in curve["points"]] == ["Spot", "C1", "C2", "C3", "C4"]
        for p in curve["points"]:
            assert p["estimate_type"] and p["status"] in {"ok", "gap"}
            assert p["data_age_days"] == (TODAY - date.fromisoformat(p["as_of"])).days
    assert as_points(body) == latest_points(store, 2)


def test_version_label_and_data_come_from_one_snapshot(
    db: str, store: Path, cache: VersionCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import committing between reading the version and reading the data must not put
    version 2 rows under a version 1 label (or a version 1 cache key)."""
    import_first(db, store)
    original = queries.latest_curves

    def import_v2_then_read(conn: psycopg.Connection):  # type: ignore[no-untyped-def]
        imp.import_pending(db, store)  # another connection; commits version 2
        return original(conn)

    monkeypatch.setattr(queries, "latest_curves", import_v2_then_read)
    body = make_client(db, cache).get("/api/curves").json()
    assert body["dataset_version"] == 1
    assert as_points(body) == latest_points(store, 1)


def test_cache_is_keyed_by_dataset_version(db: str, store: Path, cache: VersionCache) -> None:
    import_first(db, store)
    client = make_client(db, cache)
    first, second = client.get("/api/curves"), client.get("/api/curves")
    assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("miss", "hit")
    assert first.json() == second.json()
    imp.import_pending(db, store)  # version 2: new keys, no invalidation needed
    third = client.get("/api/curves")
    assert third.headers["X-Cache"] == "miss" and third.json()["dataset_version"] == 2
    assert as_points(third.json()) == latest_points(store, 2)


def test_data_age_is_computed_per_request_not_cached(
    db: str, store: Path, cache: VersionCache
) -> None:
    import_first(db, store)
    days = iter([TODAY, date(2024, 3, 20)])
    app = api_app.create_app(
        database_url=db, cache=cache, redis_url=None, port=8000, today=lambda: next(days)
    )
    client = TestClient(app, base_url=BASE)
    ages = [client.get("/api/curves").json()["curves"][0]["data_age_days"] for _ in range(2)]
    assert ages[1] - ages[0] == 10


def test_valkey_down_reads_are_served_from_postgres(db: str, store: Path) -> None:
    imp.import_pending(db, store)
    down = VersionCache.from_url("redis://127.0.0.1:1/0")  # nothing listens on port 1
    client = make_client(db, down)
    responses = [client.get("/api/curves") for _ in range(2)]
    assert [r.status_code for r in responses] == [200, 200]
    assert {r.headers["X-Cache"] for r in responses} == {"bypass"}
    assert as_points(responses[0].json()) == latest_points(store, 2)


def test_nothing_imported_is_503(db: str, cache: VersionCache) -> None:
    r = make_client(db, cache).get("/api/curves")
    assert r.status_code == 503


def test_curve_and_price_history(db: str, store: Path, cache: VersionCache) -> None:
    imp.import_pending(db, store)
    client = make_client(db, cache)
    r = client.get(
        "/api/history/curves",
        params={"curve_id": "WTI", "position": "C1", "start": "2024-02-01", "end": "2024-02-29"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dataset_version"] == 2 and body["curve_id"] == "WTI"
    dates = [p["as_of"] for p in body["points"]]
    assert dates == sorted(dates) and dates[0] >= "2024-02-01" and dates[-1] == "2024-02-29"
    prices = client.get("/api/history/prices", params={"series_id": "RWTC"}).json()
    dates = [p["date"] for p in prices["points"]]
    assert (dates[0], dates[-1]) == ("2024-01-01", "2024-02-29")
    assert len(dates) == weekdays(date(2024, 1, 1), date(2024, 2, 29))
    assert all(isinstance(p["price"], str) for p in prices["points"])  # decimals, never floats


def test_history_rejects_bad_input(db: str, store: Path, cache: VersionCache) -> None:
    imp.import_pending(db, store)
    client = make_client(db, cache)
    curves = "/api/history/curves"
    assert client.get(curves, params={"curve_id": "NOPE", "position": "C1"}).status_code == 404
    assert client.get(curves, params={"curve_id": "WTI", "position": "C9"}).status_code == 422
    backwards = {"curve_id": "WTI", "position": "C1", "start": "2024-02-02", "end": "2024-02-01"}
    assert client.get(curves, params=backwards).status_code == 422
    prices = client.get("/api/history/prices", params={"series_id": "NOPE"})
    assert prices.status_code == 404


def test_csv_exports_carry_the_disclaimer_and_synthetic_marker(
    db: str, store: Path, cache: VersionCache
) -> None:
    imp.import_pending(db, store)
    client = make_client(db, cache)
    r = client.get("/api/export/curves.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="curves-v2.csv"' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[:2] == [f"# {queries.DISCLAIMER}", "# SYNTHETIC DATA"]
    assert lines[2].startswith("# dataset_version=2 source=synthetic")
    assert lines[3].startswith("curve_id,kind,position,as_of,price,status")
    assert len(lines) == 4 + 3 * 5  # three curves, five positions
    h = client.get("/api/export/history.csv", params={"curve_id": "WTI", "position": "Spot"})
    assert h.text.splitlines()[:2] == [f"# {queries.DISCLAIMER}", "# SYNTHETIC DATA"]


def test_real_data_exports_have_no_synthetic_marker() -> None:
    from datetime import UTC, datetime

    current = queries.Current(
        7, "eia", datetime(2026, 1, 1, tzinfo=UTC), datetime.now(UTC), "a" * 64
    )
    body = api_app._csv(current, "x.csv", [{"a": 1}], "miss").body.decode()
    assert body.splitlines()[0] == f"# {queries.DISCLAIMER}"
    assert "SYNTHETIC" not in body


def test_health_lists_versions_problem_attempts_and_dead_events(
    db: str, tmp_path: Path, cache: VersionCache
) -> None:
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    ingest(store, JAN, retrieved_at=T2, overrides={("RWTC", date(2024, 1, 3)): "NA"})
    imp.import_pending(db, store)
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE app.outbox_events SET status = 'dead', last_error = 'boom'")
    body = make_client(db, cache).get("/api/health").json()
    assert [v["dataset_version"] for v in body["versions"]] == [1]
    assert body["attempts"]["counts"] == {"published": 1, "quarantined": 1}
    assert [a["status"] for a in body["attempts"]["failed_or_quarantined"]] == ["quarantined"]
    assert [(e["dataset_version"], e["last_error"]) for e in body["outbox"]["dead"]] == [
        (1, "boom")
    ]


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 200),
        ({"origin": "http://127.0.0.1:8000"}, 200),
        ({"origin": "http://localhost:8000"}, 200),
        ({"host": "evil.example"}, 400),  # DNS rebinding: a foreign name resolving to loopback
        ({"host": "evil.example:8000"}, 400),
        ({"origin": "http://evil.example"}, 403),
        ({"origin": "http://127.0.0.1:3000"}, 403),  # another local app is another origin
        ({"origin": "null"}, 403),
    ],
)
def test_local_only_host_and_origin(
    db: str, store: Path, cache: VersionCache, headers: dict[str, str], status: int
) -> None:
    imp.import_pending(db, store)
    r = make_client(db, cache).get("/api/health", headers=headers)
    assert r.status_code == status
    assert "access-control-allow-origin" not in r.headers  # no CORS, ever


def test_cross_origin_preflight_is_refused(db: str, cache: VersionCache) -> None:
    r = make_client(db, cache).options(
        "/api/curves",
        headers={"origin": "http://evil.example", "access-control-request-method": "GET"},
    )
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers


# --- shared cache across dataset histories ------------------------------------------------------


@pytest.fixture
def other_db() -> Iterator[str]:
    from db_support import fresh_database

    for dsn in fresh_database():
        migrate(dsn)
        yield dsn


Points = dict[tuple[str, str], tuple[str, str]]


def curves_json(client: TestClient) -> Points:
    return as_points(client.get("/api/curves").json())


def curves_csv(client: TestClient) -> Points:
    rows = [
        line.split(",")
        for line in client.get("/api/export/curves.csv").text.splitlines()
        if not line.startswith("#")
    ]
    col = {name: i for i, name in enumerate(rows[0])}
    return {
        (r[col["curve_id"]], r[col["position"]]): (r[col["as_of"]], r[col["price"]])
        for r in rows[1:]
    }


def history_json(client: TestClient) -> Points:
    """The latest WTI Spot point, from the history endpoint."""
    params = {"curve_id": "WTI", "position": "Spot", "start": "2024-01-29"}
    last = client.get("/api/history/curves", params=params).json()["points"][-1]
    return {("WTI", "Spot"): (last["as_of"], last["price"])}


@pytest.mark.parametrize("read", [curves_json, curves_csv, history_json], ids=lambda f: f.__name__)
def test_a_shared_cache_never_mixes_dataset_histories(
    db: str, other_db: str, tmp_path: Path, cache: VersionCache, read
) -> None:  # type: ignore[no-untyped-def]
    """Two databases, each at version 1 but with different synthetic histories, share one
    Valkey. Each must get its own prices, not the first database's cached ones under its
    own metadata: the cache identity is the dataset history, not just the version number."""
    store_a, store_b = tmp_path / "a", tmp_path / "b"
    ingest(store_a, JAN, retrieved_at=T1)
    ingest(store_b, JAN, retrieved_at=T2, overrides={("RWTC", date(2024, 1, 31)): "99.99"})
    imp.import_pending(db, store_a)
    imp.import_pending(other_db, store_b)
    for name, dsn, store in (("database a", db, store_a), ("database b", other_db, store_b)):
        expected = latest_points(store, 1)
        got = read(make_client(dsn, cache))  # same Valkey, same prefix
        assert got == {k: expected[k] for k in got}, name
    assert latest_points(store_a, 1)[("WTI", "Spot")] != latest_points(store_b, 1)[("WTI", "Spot")]
