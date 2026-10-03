"""Browser tests for the local UI (ADR-0016). Synthetic data only, so traces and screenshots
never contain real prices (ADR-0011). Each test fails on any console error, CSP violations
included, while the server is up."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from http_support import free_port, serve
from playwright.sync_api import Page, expect

from energy_curves.api.queries import DISCLAIMER
from energy_curves.db import alerts
from energy_curves.db.migrate import migrate
from energy_curves.replay import replay

pytestmark = [pytest.mark.browser, pytest.mark.postgres]
FEB_1, FEB_7, FEB_8 = date(2024, 2, 1), date(2024, 2, 7), date(2024, 2, 8)
SPIKE = {("RWTC", FEB_8): "99.99"}  # WTI spot jumps on the next replayed day


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture
def db(pg_dsn: str, store: Path) -> str:
    """Five synthetic versions, 2024-02-01 to 2024-02-07, with 20 days of warm-up history."""
    migrate(pg_dsn)
    replay(store, pg_dsn, FEB_1, FEB_7)
    return pg_dsn


@contextmanager
def ui(page: Page, db: str, **serve_kw: Any) -> Iterator[str]:
    """Serve the app, open the page and wait for its first load. On exit, leave the page
    before the server stops (so open streams close cleanly) and fail on any console error."""
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    with serve(db, None, **{"poll_s": 0.3, **serve_kw}) as base:
        page.goto(f"{base}/")
        expect(page.locator("body")).to_have_attribute("data-ready", "true")
        yield base
        page.goto("about:blank")
        assert errors == []


def tab(page: Page, name: str) -> None:
    page.locator("#tabs").get_by_text(name, exact=True).click()


def total(page: Page, grid: str) -> Any:
    return expect(page.locator(f"#grid-{grid} .w2ui-footer-right"))


def add_rule(page: Page, curve: str, position: str, threshold: str) -> None:
    form = page.get_by_test_id("rule-form")
    form.locator("select[name=curve_id]").select_option(curve)
    form.locator("select[name=position]").select_option(position)
    form.locator("input[name=threshold]").fill(threshold)
    form.get_by_role("button", name="Add rule").click()


def test_curves_tab_banner_badge_values_and_security_headers(page: Page, db: str) -> None:
    with ui(page, db) as base:
        csp = httpx.get(f"{base}/").headers["content-security-policy"]
        assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
        expect(page.get_by_test_id("disclaimer")).to_have_text(
            "Modelled estimate, not market quotes"
        )
        expect(page.get_by_test_id("synthetic-badge")).to_be_visible()
        expect(page.get_by_test_id("dataset-version")).to_have_text("dataset v5")
        expect(page.get_by_test_id("live-status")).to_have_text("live")
        expect(page.get_by_test_id("curves-label")).to_contain_text("latest available")
        total(page, "curves").to_contain_text("of 15")  # 3 curves x 5 positions
        api = httpx.get(f"{base}/api/curves").json()
        for curve in api["curves"]:
            expect(page.locator("#grid-curves")).to_contain_text(curve["points"][0]["price"])


@pytest.mark.parametrize(("today", "shown"), [(FEB_8, False), (date(2024, 2, 19), True)])
def test_stale_banner(page: Page, db: str, today: date, shown: bool) -> None:
    with ui(page, db, today=lambda: today):
        banner = page.get_by_test_id("stale-banner")
        if shown:
            expect(banner).to_have_text("Data is 12 days old (latest as of 2024-02-07).")
        else:
            expect(banner).to_be_hidden()


def test_history_filters_chart_and_export(page: Page, db: str) -> None:
    with ui(page, db) as base:
        tab(page, "History")
        form = page.get_by_test_id("history-filters")
        form.locator("select[name=curve_id]").select_option("WTI")
        form.locator("select[name=position]").select_option("C1")
        form.locator("input[name=start]").fill("2024-02-01")
        form.locator("input[name=end]").fill("2024-02-07")
        form.get_by_role("button", name="Show").click()
        expect(page.get_by_test_id("history-title")).to_have_text("WTI C1")
        expect(page.get_by_test_id("history-line")).to_be_attached()
        total(page, "history").to_contain_text("of 5")
        params = {"curve_id": "WTI", "position": "C1", "start": "2024-02-01", "end": "2024-02-07"}
        latest = httpx.get(f"{base}/api/history/curves", params=params).json()["points"][-1]
        expect(page.locator("#grid-history")).to_contain_text(latest["price"])
        page.get_by_test_id("history-chart").focus()  # keyboard readout starts at the latest
        expect(page.locator(".chart-tooltip strong")).to_have_text(latest["price"])
        with page.expect_download() as download:
            page.get_by_test_id("export-history").click()
        lines = Path(download.value.path()).read_text().splitlines()
        assert lines[:2] == [f"# {DISCLAIMER}", "# SYNTHETIC DATA"] and len(lines) == 4 + 5
        assert download.value.suggested_filename == "history-WTI-C1-v5.csv"


def test_curves_export(page: Page, db: str) -> None:
    with ui(page, db):
        with page.expect_download() as download:
            page.get_by_test_id("export-curves").click()
        lines = Path(download.value.path()).read_text().splitlines()
        assert lines[:2] == [f"# {DISCLAIMER}", "# SYNTHETIC DATA"] and len(lines) == 4 + 15


def test_an_alert_fires_live_during_replay(page: Page, db: str, store: Path) -> None:
    with ui(page, db):
        tab(page, "Alerts")
        add_rule(page, "WTI", "Spot", "95")
        total(page, "rules").to_contain_text("of 1")
        page.evaluate("window.notReloaded = true")
        replay(store, db, FEB_8, FEB_8, overrides=SPIKE)
        expect(page.get_by_test_id("toast")).to_contain_text("WTI Spot at 99.9900 crossed 95.0000")
        expect(page.locator("#grid-alerts")).to_contain_text("99.9900")
        expect(page.get_by_test_id("dataset-version")).to_have_text("dataset v6")
        assert page.evaluate("window.notReloaded") is True


def test_an_invalid_rule_shows_the_error(page: Page, db: str) -> None:
    errors: list[str] = []
    with serve(db, None, poll_s=0.3) as base:
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(f"{base}/")
        expect(page.locator("body")).to_have_attribute("data-ready", "true")
        tab(page, "Alerts")
        add_rule(page, "WTI", "Spot", "1.23456")
        expect(page.get_by_test_id("rule-error")).to_contain_text("more than 4 decimal places")
        page.goto("about:blank")
    assert [e for e in errors if "422" not in e] == []  # only the refused request itself


def test_reconnect_after_a_missed_alert(page: Page, db: str, store: Path) -> None:
    """The server goes away, an alert fires meanwhile, and the server comes back on the same
    port: the alert stream's own reconnect (Last-Event-ID) brings the missed alert, without a
    reload. The page stays on the Curves tab, so the dataset update does not re-fetch alerts:
    the toast can only come from the resumed alert stream."""
    with psycopg.connect(db) as conn:
        alerts.create_rule(conn, "WTI", "Spot", Decimal("95"))
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    port = free_port()
    with serve(db, None, poll_s=0.3, port=port) as base:
        page.goto(f"{base}/")
        expect(page.locator("body")).to_have_attribute("data-ready", "true")
        expect(page.get_by_test_id("live-status")).to_have_text("live")
        page.evaluate("window.notReloaded = true")
    expect(page.get_by_test_id("live-status")).to_have_text("reconnecting")
    replay(store, db, FEB_8, FEB_8, overrides=SPIKE)  # fired while the page is cut off
    with serve(db, None, poll_s=0.3, port=port):
        expect(page.get_by_test_id("toast")).to_contain_text(
            "WTI Spot at 99.9900 crossed 95.0000", timeout=15_000
        )
        expect(page.get_by_test_id("live-status")).to_have_text("live")
        tab(page, "Alerts")
        expect(page.locator("#grid-alerts")).to_contain_text("99.9900")
        assert page.evaluate("window.notReloaded") is True
        page.goto("about:blank")
    network = ("ERR_CONNECTION_REFUSED", "ERR_INCOMPLETE_CHUNKED_ENCODING")
    assert [e for e in errors if not any(n in e for n in network)] == []  # only the outage


def test_health_tab(page: Page, db: str) -> None:
    with ui(page, db):
        tab(page, "Health")
        expect(page.get_by_test_id("outbox-counts")).to_have_text("Outbox: 5 done")
        total(page, "versions").to_contain_text("of 5")


def test_mobile_smoke(browser: Any, db: str) -> None:
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
    )
    page = context.new_page()
    with ui(page, db):
        for name in ("Curves", "History", "Alerts", "Health"):
            page.locator("#tabs").get_by_text(name, exact=True).tap()
            expect(page.get_by_test_id(f"panel-{name.lower()}")).to_be_visible()
            # clientWidth, not innerWidth: on mobile the layout viewport grows with the content
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 0, f"{name}: the page scrolls sideways by {overflow}px"
            expect(page.get_by_test_id("disclaimer")).to_be_in_viewport()
    context.close()


# --- review findings (PR #8) ----------------------------------------------------------------------


def processed(page: Page, name: str) -> int:
    return int(page.locator("body").get_attribute(f"data-{name}") or 0)


def wait_processed(page: Page, name: str, count: int) -> None:
    expect(page.locator("body")).to_have_attribute(f"data-{name}", str(count))


def test_p2_1_a_delayed_alert_snapshot_never_erases_streamed_alerts(
    page: Page, db: str, store: Path
) -> None:
    """Finding P2-1: an alert snapshot taken before an alert fired, but answered after the
    stream delivered that alert, must not erase it from the page."""
    with psycopg.connect(db) as conn:
        alerts.create_rule(conn, "WTI", "Spot", Decimal("95"))
    held: list[Any] = []

    def hold_first(route: Any) -> None:
        if held:
            route.continue_()
        else:
            held.append((route, route.fetch()))  # the snapshot is taken now, before the alert

    with ui(page, db):
        before = processed(page, "alert-snapshots")
        page.route("**/api/alerts", hold_first)
        tab(page, "Alerts")  # requests a snapshot, which is held
        while not held:
            page.wait_for_timeout(20)
        replay(store, db, FEB_8, FEB_8, overrides=SPIKE)
        expect(page.get_by_test_id("toast")).to_contain_text("WTI Spot at 99.9900")
        expect(page.locator("#grid-alerts")).to_contain_text("99.9900")
        wait_processed(page, "alert-snapshots", before + 1)  # the refresh after the new version
        route, stale = held[0]
        route.fulfill(response=stale)  # the old snapshot arrives last
        wait_processed(page, "alert-snapshots", before + 2)
        expect(page.locator("#grid-alerts")).to_contain_text("99.9900")
        total(page, "alerts").to_contain_text("of 1")


def test_p2_2_a_late_history_response_never_replaces_the_current_selection(
    page: Page, db: str
) -> None:
    """Finding P2-2: the answer for an earlier selection arriving after the current one must
    not be drawn under the current heading or behind its export link."""
    held: list[Any] = []

    def hold_first(route: Any) -> None:
        if held:
            route.continue_()
        else:
            held.append(route)

    def choose(curve: str, position: str) -> None:
        form = page.get_by_test_id("history-filters")
        form.locator("select[name=curve_id]").select_option(curve)
        form.locator("select[name=position]").select_option(position)
        form.get_by_role("button", name="Show").click()

    with ui(page, db) as base:
        tab(page, "History")
        wait_processed(page, "history-loads", 1)  # the default selection
        page.route("**/api/history/curves?*", hold_first)
        choose("WTI", "Spot")  # held
        while not held:
            page.wait_for_timeout(20)
        choose("BRENT", "C1")
        wait_processed(page, "history-loads", 2)
        held[0].continue_()  # the WTI Spot answer arrives late
        wait_processed(page, "history-loads", 3)
        prices = {
            key: httpx.get(f"{base}/api/history/curves",
                           params={"curve_id": key[0], "position": key[1]}).json()["points"][-1]
            for key in (("WTI", "Spot"), ("BRENT", "C1"))
        }  # fmt: skip
        assert prices["WTI", "Spot"]["price"] != prices["BRENT", "C1"]["price"]
        expect(page.get_by_test_id("history-title")).to_have_text("BRENT C1")
        expect(page.locator("#grid-history")).to_contain_text(prices["BRENT", "C1"]["price"])
        expect(page.locator("#grid-history")).not_to_contain_text(prices["WTI", "Spot"]["price"])
        href = page.get_by_test_id("export-history").get_attribute("href") or ""
        assert "curve_id=BRENT" in href and "position=C1" in href
