"""The UI's static side, without a browser (ADR-0016): vendored code is pinned, the page and
every response carry the security headers, and the catalog lists what rules may name."""

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from energy_curves.api.app import STATIC, create_app
from energy_curves.api.cache import VersionCache

# Unmodified from the npm tarball w2ui-2.0.0.tgz, integrity
# sha512-oMvLitt2jMholqCpX0mDZ0b70ITjlLpfV56gswk7/cD2oPmlibV+DqcizESSs3+t5VJHiG0Xlcb23e4AoCN1Qw==
VENDORED = {
    "w2ui-2.0.es6.min.js": "b5c48efdc1999bb70ca9b8d7ce0b78a4f629156c029fcf7245b6e0993fdb640c",
    "w2ui-2.0.min.css": "b6ea6e8f2a27d069f16e85e8349919e74654d8f72d5d0b5de90dbe8517abdfad",
}
W2UI = STATIC / "vendor" / "w2ui-2.0.0"


def client() -> TestClient:
    app = create_app(database_url="postgresql://unused", cache=VersionCache(None),
                     redis_url=None, port=8000)  # fmt: skip
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_vendored_w2ui_is_the_pinned_release() -> None:
    assert {p.name for p in W2UI.iterdir()} == {*VENDORED, "LICENSE"}
    for name, digest in VENDORED.items():
        assert hashlib.sha256((W2UI / name).read_bytes()).hexdigest() == digest, name
    assert "MIT License" in (W2UI / "LICENSE").read_text()


def test_the_page_and_its_files_carry_the_security_headers() -> None:
    c = client()
    for path in ("/", "/static/app.js", "/static/vendor/w2ui-2.0.0/w2ui-2.0.min.css",
                 "/api/catalog"):  # fmt: skip
        r = c.get(path)
        assert r.status_code == 200, path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self';" in csp and "default-src 'none'" in csp, path
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
    assert "<title>Energy curves (local)</title>" in c.get("/").text


def test_refusals_carry_the_security_headers_too() -> None:
    r = client().get("/", headers={"host": "evil.example"})
    assert r.status_code == 400 and "content-security-policy" in r.headers


def test_the_page_loads_nothing_from_other_origins() -> None:
    html = (STATIC / "index.html").read_text()
    js = "".join(p.read_text() for p in Path(STATIC).glob("*.js"))
    assert "http://" not in html.replace("http://www.w3.org", "") and "https://" not in html
    assert "<script>" not in html  # inline scripts would be blocked by the CSP anyway
    assert "innerHTML" not in js  # data is written with textContent or escaped for w2ui


def test_catalog_lists_curves_and_positions() -> None:
    assert client().get("/api/catalog").json() == {
        "curves": ["BRENT", "BRENT_WTI", "WTI"],
        "positions": ["Spot", "C1", "C2", "C3", "C4"],
    }
