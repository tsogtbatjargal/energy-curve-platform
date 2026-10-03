"""Logging configuration that cannot leak the EIA API key.

httpx logs every request URL at INFO, and EIA takes the key as a query parameter. Two layers:
httpx/httpcore are held at WARNING, and a filter on every root handler rewrites each record so
`api_key=` values and any registered secret are replaced, including in exception text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

_QUERY_KEY = re.compile(r"(api_key=)[^&\s\"'<>]+", re.IGNORECASE)
REDACTED = "REDACTED"
NOISY_LOGGERS = ("httpx", "httpcore")


def redact_text(text: str, secrets: Iterable[str]) -> str:
    text = _QUERY_KEY.sub(rf"\g<1>{REDACTED}", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), self._secrets)
        record.args = None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact_text(text, self._secrets)
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, self._secrets)
        return True


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    logging.basicConfig(
        level=level.upper(), format="%(levelname)s %(name)s %(message)s", force=True
    )
    redactor = RedactingFilter(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
