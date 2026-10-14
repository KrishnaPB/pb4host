"""Adapter interface and payload builders shared by all CRM targets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import Settings
from ..models import Organization, Person


@dataclass
class RemoteRecord:
    remote_id: str
    url: str | None = None
    status: str | None = None
    raw: dict[str, Any] | None = None


class CrmAdapter(Protocol):
    target: str

    def find_account(self, *, domain: str | None, name: str | None) -> RemoteRecord | None: ...

    def upsert_account(self, payload: dict[str, Any], remote_id: str | None) -> RemoteRecord: ...

    def upsert_contact(self, payload: dict[str, Any], account_id: str, remote_id: str | None) -> RemoteRecord: ...

    def get_account(self, remote_id: str) -> RemoteRecord | None: ...

    def delete_record(self, entity: str, remote_id: str) -> bool: ...


def source_summary(org: Organization) -> str:
    return ", ".join(sorted({e.source for e in org.evidence})) or "no stored facts"


def account_payload(org: Organization, settings: Settings, notice: str) -> dict[str, Any]:
    site = org.primary_website
    lines = [
        f"Prospector organisation #{org.id} — score {org.score_total} ({org.score_band or 'unscored'}), track {org.track}.",
        f"Signals: {', '.join(org.signals) if org.signals else 'none'}.",
        f"Stack: {site.cms or 'unknown CMS'}{' ' + site.cms_version if site and site.cms_version else ''}"
        f"{', WooCommerce' if site and site.is_woocommerce else ''}{', host ' + site.host_provider if site and site.host_provider else ''}." if site else "Stack: no website probed.",
        f"Sources: {source_summary(org)}.",
        f"Evidence: {settings.base_url.rstrip('/')}/organizations/{org.id}",
        f"Lead source: {org.lead_source or 'discovery'}" + (f" (inbound {org.inbound_at:%Y-%m-%d})" if org.inbound_at else ""),
        "",
        "Notice text to use on first contact:",
        notice,
    ]
    payload: dict[str, Any] = {
        "name": org.label[:249],
        "website": org.website_url or None,
        "description": "\n".join(lines),
        "billingAddressCity": org.city or None,
        "billingAddressCountry": org.country or None,
        "billingAddressPostalCode": org.postal_code or None,
    }
    if org.emails:
        payload["emailAddress"] = org.emails[0]
    if org.phones:
        payload["phoneNumber"] = org.phones[0]
    return {k: v for k, v in payload.items() if v is not None}


def contact_payload(person: Person, settings: Settings) -> dict[str, Any]:
    first, _, last = person.full_name.strip().partition(" ")
    payload: dict[str, Any] = {
        "firstName": first[:100] or None,
        "lastName": (last or first)[:100],
        "title": (person.role_title or None),
        "emailAddress": person.email or None,
        "phoneNumber": person.phone_e164 or None,
        "description": (f"Prospector person #{person.id}. Lawful basis: {person.lawful_basis}"
                        f"{' (' + person.lia_reference + ')' if person.lia_reference else ''}; notice {person.notice_status}"
                        f"{' on ' + person.notice_sent_at.strftime('%Y-%m-%d') if person.notice_sent_at else ''}; source {person.source}."),
    }
    return {k: v for k, v in payload.items() if v is not None}


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def person_is_notice_ready(person: Person) -> bool:
    """Only contacts a human may lawfully reach go to the CRM."""
    if person.do_not_contact or person.notice_status == "objected":
        return False
    if person.lawful_basis == "consent":
        return person.consent_id is not None
    return person.notice_status in {"sent", "not_required"}
