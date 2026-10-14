"""Facts with provenance (R1).

Connectors, imports and manual capture never set organisation/website fields directly; they
produce `Fact` items and call `apply_facts`, which writes the field *and* the matching
`Evidence` row in one transaction. `evidence_coverage` reports fields that lack provenance.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Evidence, Organization, Website
from ..util import utcnow
from .normalize import host_key, normalize_email, normalize_phone


class FactError(ValueError):
    pass


@dataclass
class Fact:
    field: str  # e.g. "organization.display_name", "website.cms"
    value: object
    source: str  # connector / origin name, e.g. "own_website", "import:customers"
    source_url: str  # public URL (or import://, manual:// reference) an analyst can open
    confidence: float = 1.0
    fetched_at: datetime | None = None
    expires_at: datetime | None = None
    entity_type: str = dc_field(default="", init=False)

    def __post_init__(self) -> None:
        if "." not in self.field:
            raise FactError(f"field must be '<entity>.<name>', got {self.field!r}")
        self.entity_type = self.field.split(".", 1)[0]
        if not self.source or not self.source_url:
            raise FactError(f"fact {self.field} needs source and source_url (R1)")


def _set_simple(attr: str) -> Callable[[Organization, Website | None, object], None]:
    def setter(org: Organization, website: Website | None, value: object) -> None:
        setattr(org, attr, None if value is None else str(value).strip() or None)

    return setter


def _set_site(attr: str, cast: Callable[[object], object] = lambda v: v):
    def setter(org: Organization, website: Website | None, value: object) -> None:
        if website is None:
            raise FactError(f"website fact requires a website row (field {attr})")
        setattr(website, attr, cast(value))

    return setter


def _append_phone(org: Organization, website: Website | None, value: object) -> None:
    e164 = normalize_phone(str(value), org.country or "IN") or (
        str(value) if str(value).startswith("+") else None
    )
    if e164 and e164 not in org.phones:
        org.phones = [*org.phones, e164]


def _append_email(org: Organization, website: Website | None, value: object) -> None:
    email = normalize_email(str(value))
    if email and email not in org.emails:
        org.emails = [*org.emails, email]


def _set_registry(key: str):
    def setter(org: Organization, website: Website | None, value: object) -> None:
        ids = dict(org.registry_ids)
        ids[key] = str(value).strip()
        org.registry_ids = ids

    return setter


def _set_social(org: Organization, website: Website | None, value: object) -> None:
    network, _, url = str(value).partition("=")
    if network and url:
        links = dict(org.social_links)
        links[network] = url
        org.social_links = links


def _set_website_url(org: Organization, website: Website | None, value: object) -> None:
    url = str(value)
    org.website_url = url
    org.domain = host_key(url)
    if website is not None:
        website.final_url = url
        website.host_key = org.domain


def _opt_str(v: object) -> str | None:
    return None if v is None else (str(v).strip() or None)


def _opt_bool(v: object) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).lower() in {"1", "true", "yes"}


def _opt_int(v: object) -> int | None:
    return None if v is None else int(v)


def _opt_dt(v: object) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v  # type: ignore[return-value]
    return datetime.fromisoformat(str(v))


SETTERS: dict[str, Callable[[Organization, Website | None, object], None]] = {
    "organization.display_name": _set_simple("display_name"),
    "organization.legal_name": _set_simple("legal_name"),
    "organization.trade_name": _set_simple("trade_name"),
    "organization.website_url": _set_website_url,
    "organization.phone": _append_phone,
    "organization.email": _append_email,
    "organization.address_text": _set_simple("address_text"),
    "organization.city": _set_simple("city"),
    "organization.region": _set_simple("region"),
    "organization.postal_code": _set_simple("postal_code"),
    "organization.country": _set_simple("country"),
    "organization.track": _set_simple("track"),
    "organization.registry.gstin": _set_registry("gstin"),
    "organization.registry.cin": _set_registry("cin"),
    "organization.registry.company_number": _set_registry("company_number"),
    "organization.social": _set_social,
    "website.cms": _set_site("cms", _opt_str),
    "website.cms_version": _set_site("cms_version", _opt_str),
    "website.is_woocommerce": _set_site("is_woocommerce", _opt_bool),
    "website.theme": _set_site("theme", _opt_str),
    "website.page_builder": _set_site("page_builder", _opt_str),
    "website.caching_layer": _set_site("caching_layer", _opt_str),
    "website.cdn": _set_site("cdn", _opt_str),
    "website.managed_host": _set_site("managed_host", _opt_str),
    "website.server_header": _set_site("server_header", _opt_str),
    "website.php_version": _set_site("php_version", _opt_str),
    "website.http_status": _set_site("http_status", _opt_int),
    "website.error_state": _set_site("error_state", _opt_str),
    "website.tls_valid": _set_site("tls_valid", _opt_bool),
    "website.tls_expires_at": _set_site("tls_expires_at", _opt_dt),
    "website.host_ip": _set_site("host_ip", _opt_str),
    "website.host_asn": _set_site("host_asn", _opt_str),
    "website.host_provider": _set_site("host_provider", _opt_str),
    "website.host_class": _set_site("host_class", _opt_str),
    "website.mail_provider": _set_site("mail_provider", _opt_str),
}

# Fields whose non-null value must be backed by evidence (used by evidence_coverage / R1 test)
ORG_COVERED_ATTRS = {
    "display_name": "organization.display_name",
    "legal_name": "organization.legal_name",
    "website_url": "organization.website_url",
    "address_text": "organization.address_text",
    "city": "organization.city",
    "country": "organization.country",
}
SITE_COVERED_ATTRS = {
    "cms": "website.cms",
    "cms_version": "website.cms_version",
    "theme": "website.theme",
    "page_builder": "website.page_builder",
    "caching_layer": "website.caching_layer",
    "cdn": "website.cdn",
    "php_version": "website.php_version",
    "host_asn": "website.host_asn",
    "host_provider": "website.host_provider",
    "mail_provider": "website.mail_provider",
}


def apply_facts(
    session: Session,
    org: Organization,
    facts: list[Fact],
    website: Website | None = None,
) -> list[Evidence]:
    """Set fields and write evidence rows. Unknown fields raise (no silent provenance gaps)."""
    now = utcnow()
    rows: list[Evidence] = []
    for fact in facts:
        setter = SETTERS.get(fact.field)
        if setter is None:
            raise FactError(f"unknown fact field {fact.field!r}")
        if fact.value is None or (isinstance(fact.value, str) and not fact.value.strip()):
            continue
        setter(org, website, fact.value)
        entity_id = None
        if fact.entity_type == "website" and website is not None:
            entity_id = website.id
        elif fact.entity_type == "organization":
            entity_id = org.id
        row = Evidence(
            organization=org,
            entity_type=fact.entity_type,
            entity_id=entity_id,
            field=fact.field,
            value=str(fact.value),
            source=fact.source,
            source_url=fact.source_url,
            confidence=fact.confidence,
            fetched_at=fact.fetched_at or now,
            expires_at=fact.expires_at,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def evidence_coverage(session: Session, org: Organization) -> list[str]:
    """Return populated fields that have no evidence row — should always be empty (R1)."""
    present = {e.field for e in org.evidence}
    missing: list[str] = []
    for attr, field in ORG_COVERED_ATTRS.items():
        if getattr(org, attr) and field not in present:
            missing.append(field)
    if org.phones and "organization.phone" not in present:
        missing.append("organization.phone")
    if org.emails and "organization.email" not in present:
        missing.append("organization.email")
    for site in org.websites:
        for attr, field in SITE_COVERED_ATTRS.items():
            if getattr(site, attr) and field not in present:
                missing.append(field)
    return missing
