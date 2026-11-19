"""Logging configuration: rotating file log, secret redaction, crash logging."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys

from .paths import LOG_FILE, ensure_runtime_dir

# Telegram puts the bot token in the request path (".../bot<id>:<secret>/...").
_SECRET_PATTERNS = [
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{10,}"), "gsk_<redacted>"),
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?bearer\s+)[^\s'\"]+"), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Scrub tokens and API keys from every record before it is written."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def configure() -> None:
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    redactor = RedactSecretsFilter()

    ensure_runtime_dir()
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    # sys.stdout is None when started windowless (pythonw / start_silent.vbs).
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redactor)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)

    # httpx logs every request URL at INFO, which includes the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    def _log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("crash").critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _log_uncaught
