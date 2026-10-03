import logging
from collections.abc import Iterator

import pytest
from db_support import fresh_database, valkey_url


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """The CLI reconfigures root logging (force=True); undo it so later output, including
    library shutdown messages, never goes to a stream pytest has closed."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, lvl in levels.items():
        logging.getLogger(name).setLevel(lvl)


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """A fresh, empty Postgres database for one test, dropped afterwards."""
    yield from fresh_database()


@pytest.fixture
def redis_url() -> Iterator[str]:
    """A reachable Valkey (local compose or the CI service container)."""
    yield from valkey_url()
