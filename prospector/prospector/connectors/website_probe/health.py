"""Health classification of a fetched homepage (visible error states, parked domains, TLS)."""

from __future__ import annotations

import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

ERROR_MARKERS = [
    ("db_error", [r"Error establishing a database connection"]),
    ("php_error", [
        r"There has been a critical error on (?:this|your) website",
        r"<b>Fatal error</b>:", r"\bFatal error: Uncaught", r"\bParse error: syntax error",
        r"\bWarning: .*? on line \d+", r"\bNotice: .*? on line \d+",
    ]),
    ("maintenance", [r"Briefly unavailable for scheduled maintenance", r"Check back in a minute"]),
]
PARKED_MARKERS = [
    r"this domain is parked", r"buy this domain", r"domain (?:is|may be) for sale",
    r"sedoparking", r"parkingcrew", r"parked free, courtesy of", r"domain has expired",
    r"this site can.?t be reached", r"account suspended", r"website is temporarily unavailable",
    r"this website has been suspended", r"hostgator\.com/.*suspended", r"bodis\.com",
]


def classify(status: int | None, html: str, fetch_error: str | None) -> str:
    err = (fetch_error or "").lower()
    if err.startswith("dns"):
        return "dns_fail"
    if err.startswith("tls") or "certificate" in err or "ssl" in err:
        return "tls_invalid"
    if err.startswith("timeout"):
        return "timeout"
    if err.startswith(("connection", "too_many_redirects", "http_error", "transport", "blocked", "refused")):
        return "unreachable"
    text = html or ""
    if status is not None and status >= 500:
        for state, patterns in ERROR_MARKERS:
            if any(re.search(p, text, re.IGNORECASE) for p in patterns):
                return state
        return "server_error"
    for state, patterns in ERROR_MARKERS:
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            return state
    sample = text[:50_000].lower()
    if any(re.search(p, sample, re.IGNORECASE) for p in PARKED_MARKERS):
        return "parked"
    return "none"


@dataclass
class TlsInfo:
    valid: bool | None
    expires_at: datetime | None
    error: str | None = None


def tls_info(url: str, timeout: float = 5.0) -> TlsInfo:
    parts = urlsplit(url)
    if parts.scheme != "https":
        return TlsInfo(valid=None, expires_at=None, error="not_https")
    host = parts.hostname or ""
    port = parts.port or 443
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock, ctx.wrap_socket(sock, server_hostname=host) as ssock:
            cert = ssock.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        return TlsInfo(valid=False, expires_at=None, error=f"verify:{exc.verify_message if hasattr(exc, 'verify_message') else exc}")
    except (TimeoutError, ssl.SSLError, OSError) as exc:
        return TlsInfo(valid=None, expires_at=None, error=type(exc).__name__)
    not_after = cert.get("notAfter") if cert else None
    expires = None
    if not_after:
        try:
            expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
        except ValueError:
            expires = None
    return TlsInfo(valid=True, expires_at=expires)
