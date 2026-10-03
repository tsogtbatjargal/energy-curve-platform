"""Finding 1: the API key must not reach logs. Runs the real CLI (its own logging setup) in a
subprocess against a local fake EIA server, with a fake key."""

import http.server
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

FAKE_KEY = "FAKEKEY" + "x" * 33
FIXTURE = (Path(__file__).parent / "fixtures/eia_spot_page.json").read_bytes()


class FakeEia(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = FIXTURE if "/petroleum/pri/spt/data/" in self.path else b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_eia() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEia)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v2"
    server.shutdown()


def run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    code = "from energy_curves.cli import main; raise SystemExit(main())"
    return subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code, *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_cli_logs_never_contain_the_api_key(tmp_path: Path, fake_eia: str) -> None:
    proc = run_cli(
        ["ingest", "--source", "eia", "--start", "2026-09-25", "--end", "2026-09-28"],
        {
            "EIA_API_KEY": FAKE_KEY,
            "EIA_BASE_URL": fake_eia,
            "DATA_DIR": str(tmp_path),
            "LOG_LEVEL": "DEBUG",
        },
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "run finished" in output  # logging was active, so the check is meaningful
    assert FAKE_KEY not in output
    assert "api_key=" not in output or "api_key=REDACTED" in output


def test_stored_artifacts_never_contain_the_api_key(tmp_path: Path, fake_eia: str) -> None:
    run_cli(
        ["ingest", "--source", "eia", "--start", "2026-09-25", "--end", "2026-09-28"],
        {"EIA_API_KEY": FAKE_KEY, "EIA_BASE_URL": fake_eia, "DATA_DIR": str(tmp_path)},
    )
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert FAKE_KEY.encode() not in f.read_bytes(), f


def test_redacting_filter_scrubs_key_values_and_query_parameters() -> None:
    import logging

    from energy_curves.logging_setup import RedactingFilter

    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "GET %s",
        (f"https://x/v2/d/?a=1&api_key={FAKE_KEY}&b=2",),
        None,
    )
    assert RedactingFilter([FAKE_KEY]).filter(record)
    message = record.getMessage()
    assert FAKE_KEY not in message and "api_key=REDACTED&b=2" in message
    _ = json


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    import logging

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    httpx_level = logging.getLogger("httpx").level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(httpx_level)


def test_filter_redacts_even_if_httpx_logging_is_re_enabled(
    restore_root_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    from energy_curves.logging_setup import configure_logging

    configure_logging("DEBUG", secrets=[FAKE_KEY])
    logging.getLogger("httpx").setLevel(logging.INFO)  # simulate a later misconfiguration
    logging.getLogger("httpx").info('HTTP Request: GET %s "200"', f"https://x/?api_key={FAKE_KEY}")
    try:
        raise RuntimeError(f"failed for https://x/?api_key={FAKE_KEY}")
    except RuntimeError:
        logging.getLogger("energy_curves").exception("fetch failed")
    err = capsys.readouterr().err
    assert "HTTP Request" in err and "fetch failed" in err
    assert FAKE_KEY not in err
