"""Logging with redaction of e-mail addresses and phone-like sequences (R8: no PII in logs)."""

from __future__ import annotations

import logging
import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w:-])\+?\d[\d\s().-]{8,}\d(?![\w:-])")


def redact(text: str) -> str:
    text = _EMAIL.sub("<email>", text)
    return _PHONE.sub("<phone>", text)


class RedactingFormatter(logging.Formatter):
    """Redacts the log message and exception text, leaving the timestamp prefix intact."""

    def format(self, record: logging.LogRecord) -> str:
        original_msg, original_args = record.msg, record.args
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            text = super().format(record)
        finally:
            record.msg, record.args = original_msg, original_args
        if record.exc_info or record.exc_text:
            # the traceback is appended after the message; redact everything past the prefix
            head, sep, tail = text.partition("\n")
            text = head + sep + redact(tail)
        return text


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "alembic.runtime.plugins", "tldextract"):
        logging.getLogger(noisy).setLevel("WARNING")
