"""The image serves real health/static responses on an isolated dependency network."""

import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/image_api_smoke.py"


def test_ci_starts_the_installed_cli_with_an_entrypoint_override():
    steps = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["image"][
        "steps"
    ]
    run = next(
        (s.get("run", "") for s in steps if s.get("name") == "smoke test the installed API and UI"),
        "",
    )
    assert "docker network create --internal ecp-api-smoke" in run
    assert '--entrypoint python ecp-batch:ci -c "$cli" serve --port 8000' in run
    assert '--entrypoint python ecp-batch:ci -c "$cli" db-migrate' in run
    assert "docker exec -i ecp-api python - < scripts/image_api_smoke.py" in run
    assert "--network ecp-api-smoke" in run


def test_smoke_checks_installed_package_health_and_static_asset():
    assert SCRIPT.exists()
    text = SCRIPT.read_text()
    assert 'distribution("energy-curves")' in text
    assert '"energy_curves/api/app.py"' in text
    assert '"energy_curves/api/static/index.html"' in text
    assert '"/api/health"' in text and '"/static/app.js"' in text
    assert "ping()" in text


@pytest.mark.postgres
def test_smoke_passes_against_the_current_app(pg_dsn, redis_url, monkeypatch):
    import threading

    import uvicorn

    from energy_curves.api.app import create_app
    from energy_curves.api.cache import VersionCache
    from energy_curves.db.migrate import migrate

    migrate(pg_dsn)
    monkeypatch.setenv("REDIS_URL", redis_url)
    app = create_app(
        database_url=pg_dsn, cache=VersionCache.from_url(redis_url), redis_url=redis_url, port=8768
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8768, log_level="error"))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        monkeypatch.setenv("IMAGE_API_SMOKE_URL", "http://127.0.0.1:8768")
        runpy.run_path(str(SCRIPT))["check_http"]("http://127.0.0.1:8768")
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()
