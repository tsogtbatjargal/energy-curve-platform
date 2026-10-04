import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from db_support import JAN, T1, ingest

from energy_curves import replay as rp
from energy_curves.db import alerts
from energy_curves.db.migrate import migrate
from energy_curves.db.monitored import monitored_values

pytestmark = pytest.mark.postgres
FEB_1, FEB_7 = date(2024, 2, 1), date(2024, 2, 7)


@pytest.fixture
def db(pg_dsn: str) -> str:
    migrate(pg_dsn)
    return pg_dsn


def test_business_days() -> None:
    assert rp.business_days(date(2024, 2, 2), date(2024, 2, 6)) == [
        date(2024, 2, 2), date(2024, 2, 5), date(2024, 2, 6),
    ]  # fmt: skip


def test_each_business_day_becomes_one_imported_version(db: str, tmp_path: Path) -> None:
    committed: list[int] = []
    steps = rp.replay(tmp_path / "store", db, FEB_1, FEB_7, on_committed=committed.append)
    assert [s.day for s in steps] == rp.business_days(FEB_1, FEB_7)
    assert [s.imported for s in steps] == [[1], [2], [3], [4], [5]]
    assert committed == [1, 2, 3, 4, 5]  # dataset_updated after each commit
    assert all(s.events_done == 1 for s in steps)  # each version evaluated straight away
    with psycopg.connect(db) as conn:
        latest = {v: monitored_values(conn, v)[("WTI", "Spot")].as_of_date for v in (1, 5)}
    assert latest == {1: FEB_1, 5: FEB_7}


def test_an_override_makes_a_rule_cross_on_its_day(db: str, tmp_path: Path) -> None:
    store = tmp_path / "store"
    rp.replay(store, db, FEB_1, FEB_1)
    with psycopg.connect(db) as conn:
        spot = monitored_values(conn, 1)[("WTI", "Spot")].price
        assert spot is not None and spot < Decimal("95")
        alerts.create_rule(conn, "WTI", "Spot", Decimal("95"))
    woken: list[int] = []
    rp.replay(
        store, db, date(2024, 2, 2), date(2024, 2, 6),
        overrides={("RWTC", date(2024, 2, 5)): "99.99"},
        on_events_done=lambda: woken.append(1),
    )  # fmt: skip
    with psycopg.connect(db) as conn:
        fired = conn.execute(
            "SELECT dataset_version, as_of, price FROM app.fired_alerts ORDER BY seq"
        ).fetchall()
    assert fired == [(3, date(2024, 2, 5), Decimal("99.9900"))]
    assert woken  # the alert stream was woken after a pass completed events


def test_interval_sleeps_between_days_only(db: str, tmp_path: Path) -> None:
    sleeps: list[float] = []
    rp.replay(tmp_path / "s", db, FEB_1, date(2024, 2, 5), interval_s=2.5, sleep=sleeps.append)
    assert sleeps == [2.5, 2.5]  # three days, two gaps


def test_a_store_with_real_data_is_refused(db: str, tmp_path: Path) -> None:
    store = tmp_path / "store"
    ingest(store, JAN, retrieved_at=T1)
    manifest = next((store / "runs").glob("*/*/manifest.json"))
    record = next((store / "published" / "versions").glob("*.json"))
    doc = json.loads(manifest.read_text())
    doc["identity"]["source"] = "eia"  # as if real; re-seal the record so the hash matches
    manifest.write_text(json.dumps(doc))
    from energy_curves.storage.artifacts import sha256

    rec = json.loads(record.read_text())
    sealed = json.dumps({**rec, "manifest_sha256": sha256(manifest.read_bytes())})
    record.write_text(sealed)
    (store / "published" / "current.json").write_text(sealed)  # the pointer too (ADR-0019)
    with pytest.raises(rp.ReplayRefused, match="'eia'"):
        rp.replay(store, db, FEB_1, FEB_1)


def test_a_database_with_real_data_is_refused(db: str, tmp_path: Path) -> None:
    with psycopg.connect(db) as conn:
        conn.execute(
            "INSERT INTO market.dataset_versions VALUES (1, 'x', 'y', 'eia', now(), now(),"
            " 0, 0, 0, 0)"
        )
    with pytest.raises(rp.ReplayRefused, match="eia"):
        rp.replay(tmp_path / "store", db, FEB_1, FEB_1)
    assert not (tmp_path / "store").exists()  # refused before writing anything


def test_parse_override() -> None:
    assert rp.parse_override("RWTC=2024-02-29:99.99") == (("RWTC", date(2024, 2, 29)), "99.99")
    with pytest.raises(ValueError, match="SERIES"):
        rp.parse_override("RWTC:99")


def test_cli(
    db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from energy_curves.cli import main

    monkeypatch.setenv("DATABASE_URL", db)
    args = ["replay", "--store", str(tmp_path / "s"), "--start", "2024-02-01"]
    assert main([*args, "--end", "2024-02-02", "--interval", "0"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(s["day"], s["imported"]) for s in lines] == [("2024-02-01", [1]), ("2024-02-02", [2])]
