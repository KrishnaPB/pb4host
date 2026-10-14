"""Small shared helpers (time, hashing, text)."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC timestamp. All persisted datetimes are naive UTC for SQLite/Postgres parity."""
    return datetime.now(UTC).replace(tzinfo=None)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text[:max_len] or "x"


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """'6.5.2' -> (6, 5, 2). Returns None when the text is not a dotted version."""
    if not text:
        return None
    m = re.match(r"^\s*v?(\d+(?:\.\d+){0,3})", text)
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def wp_releases_behind(found: str | None, latest: str) -> int | None:
    """WordPress 'major' releases are the X.Y numbers (6.5, 6.6, ...).

    Returns how many X.Y releases `found` is behind `latest`, or None when unknown.
    A different leading number is treated as at least two releases behind.
    """
    f = parse_version(found)
    lt = parse_version(latest)
    if not f or not lt or len(f) < 2 or len(lt) < 2:
        return None
    if f[0] != lt[0]:
        return 2 if f[0] < lt[0] else 0
    return max(0, lt[1] - f[1])


def php_is_eol(found: str | None, min_supported: str) -> bool | None:
    f = parse_version(found)
    m = parse_version(min_supported)
    if not f or not m:
        return None
    return f[:2] < m[:2]
