# ruff: noqa: S101 (smoke assertions)
"""Probe the installed package and the running API inside the CI image.

The API uses real Postgres and Valkey on Docker's --internal network. The database is migrated
but empty; no prices or external services are needed, and no dependency check is skipped.
"""

import json
import os
import time
from importlib import metadata, resources
from urllib.error import URLError
from urllib.request import urlopen

import redis

REQUIRED = {
    "energy_curves/api/app.py",
    "energy_curves/api/static/index.html",
    "energy_curves/api/static/app.js",
    "energy_curves/api/static/app.css",
    "energy_curves/api/static/chart.js",
    "energy_curves/api/static/vendor/w2ui-2.0.0/w2ui-2.0.es6.min.js",
    "energy_curves/api/static/vendor/w2ui-2.0.0/w2ui-2.0.min.css",
    "energy_curves/api/static/vendor/w2ui-2.0.0/LICENSE",
}


def check_package():
    dist = metadata.distribution("energy-curves")
    shipped = {str(path) for path in dist.files or []}
    assert shipped >= REQUIRED, f"installed distribution missing: {sorted(REQUIRED - shipped)}"
    package = resources.files("energy_curves")
    for name in REQUIRED:
        assert package.joinpath(name.removeprefix("energy_curves/")).is_file(), name
    from energy_curves.api.app import create_app

    assert callable(create_app)


def check_http(base="http://127.0.0.1:8000"):
    assert redis.Redis.from_url(os.environ["REDIS_URL"]).ping()
    deadline = time.monotonic() + 30
    while True:
        try:
            with urlopen(base + "/api/health", timeout=2) as response:  # noqa: S310 - fixed loopback
                assert response.status == 200
                health = json.load(response)
                assert health["dataset_version"] is None
            break
        except (URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    with urlopen(base + "/static/app.js", timeout=2) as response:  # noqa: S310 - fixed loopback
        assert response.status == 200 and response.read()
    print(json.dumps({"api_smoke": "ok", "health": 200, "static": 200, "valkey": "pong"}))


if __name__ == "__main__":
    check_package()
    check_http()
