"""Normalisation of domains, URLs, phones, emails and names (used for dedupe keys)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import phonenumbers
import tldextract
from email_validator import EmailNotValidError, validate_email

_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

GENERIC_MAILBOXES = {
    "info", "hello", "contact", "sales", "support", "admin", "office", "enquiry", "enquiries",
    "inquiry", "inquiries", "mail", "team", "help", "hi", "care", "business", "accounts",
    "billing", "marketing", "hr", "careers", "webmaster", "service", "services", "reception",
}
JUNK_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "email.com", "domain.com", "yourdomain.com",
    "sentry.io", "wixpress.com", "sentry.wixpress.com", "w3.org", "schema.org", "wordpress.org",
    "googleapis.com", "gstatic.com", "2x.png", "yoursite.com", "mysite.com", "test.com",
}
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(private limited|pvt\.? ltd\.?|pvt|ltd\.?|limited|llp|inc\.?|llc|plc|gmbh|co\.?|"
    r"corp\.?|corporation|company|technologies|technology|solutions|services|group)\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    host = parts.hostname or ""
    if not host or ("." not in host and host != "localhost"):
        return None
    netloc = host.lower()
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80) or (parts.scheme == "https" and parts.port == 443)
    ):
        netloc = f"{netloc}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def host_key(url_or_host: str | None) -> str | None:
    """Host without a leading www. and without port — the primary dedupe key."""
    if not url_or_host:
        return None
    norm = normalize_url(url_or_host)
    if not norm:
        return None
    host = (urlsplit(norm).hostname or "").lower().rstrip(".")
    host = host.removeprefix("www.")
    return host or None


def registered_domain(url_or_host: str | None) -> str | None:
    host = host_key(url_or_host)
    if not host:
        return None
    ext = _extract(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def normalize_phone(raw: str | None, region: str = "IN") -> str | None:
    if not raw:
        return None
    try:
        num = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def find_phones(text: str, region: str = "IN", limit: int = 5) -> list[str]:
    found: list[str] = []
    try:
        for match in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
            e164 = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)
            if e164 not in found:
                found.append(e164)
            if len(found) >= limit:
                break
    except Exception:  # pragma: no cover - defensive against odd input
        return found
    return found


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip(".,;:()<>[]\"'").lower()
    if not raw or "@" not in raw:
        return None
    try:
        result = validate_email(raw, check_deliverability=False)
    except EmailNotValidError:
        return None
    email = result.normalized.lower()
    domain = email.rsplit("@", 1)[1]
    if domain in JUNK_EMAIL_DOMAINS or re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", domain):
        return None
    return email


def find_emails(text: str, limit: int = 10) -> list[str]:
    found: list[str] = []
    for raw in _EMAIL_RE.findall(text):
        email = normalize_email(raw)
        if email and email not in found:
            found.append(email)
        if len(found) >= limit:
            break
    return found


def is_generic_mailbox(email: str) -> bool:
    local = email.split("@", 1)[0]
    return local in GENERIC_MAILBOXES


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    text = _LEGAL_SUFFIX_RE.sub(" ", name)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return " ".join(text.split())
