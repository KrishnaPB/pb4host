"""Suppression list and rights handling (R6/R7).

Suppressed values are stored as salted SHA-256 hashes so the do-not-contact list survives
erasure of the underlying records without itself holding personal data in clear text.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.normalize import host_key, normalize_email, normalize_phone
from ..models import Organization, Person, Suppression, User
from ..util import utcnow
from . import audit


def normalize_for_kind(kind: str, value: str, region: str = "IN") -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if kind == "domain":
        return host_key(value)
    if kind == "email":
        return normalize_email(value)
    if kind == "phone":
        return normalize_phone(value, region)
    if kind == "place_id":
        return value
    raise ValueError(f"unknown suppression kind {kind}")


def hash_value(kind: str, normalized: str, salt: str | None = None) -> str:
    salt = salt if salt is not None else get_settings().salt
    return hmac.new(salt.encode("utf-8"), f"{kind}:{normalized}".encode(), hashlib.sha256).hexdigest()


def is_suppressed(session: Session, kind: str, value: str | None) -> bool:
    if not value:
        return False
    normalized = normalize_for_kind(kind, value)
    if not normalized:
        return False
    digest = hash_value(kind, normalized)
    return (
        session.scalar(
            select(Suppression.id).where(Suppression.kind == kind, Suppression.value_hash == digest)
        )
        is not None
    )


def add_suppression(
    session: Session,
    kind: str,
    value: str,
    *,
    reason: str | None = None,
    requested_by: str | None = None,
    actor: User | None = None,
) -> Suppression | None:
    normalized = normalize_for_kind(kind, value)
    if not normalized:
        raise ValueError(f"cannot normalise {kind} value")
    digest = hash_value(kind, normalized)
    existing = session.scalar(
        select(Suppression).where(Suppression.kind == kind, Suppression.value_hash == digest)
    )
    if existing:
        return existing
    row = Suppression(kind=kind, value_hash=digest, reason=reason, requested_by=requested_by)
    session.add(row)
    session.flush()
    audit.log(session, "suppression.add", actor=actor, entity_type="suppression", entity_id=row.id,
              kind=kind, reason=reason)
    return row


def organization_is_suppressed(session: Session, org: Organization) -> bool:
    if org.status == "suppressed":
        return True
    if org.domain and is_suppressed(session, "domain", org.domain):
        return True
    for email in org.emails or []:
        if is_suppressed(session, "email", email):
            return True
    for phone in org.phones or []:
        if is_suppressed(session, "phone", phone):
            return True
    return any(is_suppressed(session, "place_id", link.place_id) for link in org.place_links)


def suppress_organization(
    session: Session, org: Organization, *, reason: str, actor: User | None = None
) -> None:
    """Add every identifier of the organisation to the list and mark it suppressed."""
    if org.domain:
        add_suppression(session, "domain", org.domain, reason=reason, actor=actor)
    for email in org.emails or []:
        add_suppression(session, "email", email, reason=reason, actor=actor)
    for phone in org.phones or []:
        add_suppression(session, "phone", phone, reason=reason, actor=actor)
    for link in org.place_links:
        add_suppression(session, "place_id", link.place_id, reason=reason, actor=actor)
    org.status = "suppressed"
    audit.log(session, "organization.suppress", actor=actor, entity_type="organization",
              entity_id=org.id, reason=reason)


@dataclass
class RightsMatches:
    organizations: list[Organization] = field(default_factory=list)
    persons: list[Person] = field(default_factory=list)


def find_subjects(session: Session, query: str) -> RightsMatches:
    """Locate records by email, phone or domain for access/erasure requests."""
    query = (query or "").strip()
    out = RightsMatches()
    if not query:
        return out
    email = normalize_email(query) if "@" in query else None
    phone = normalize_phone(query) if any(ch.isdigit() for ch in query) and "@" not in query else None
    domain = host_key(query) if "." in query and "@" not in query else None
    for org in session.scalars(select(Organization).where(Organization.status != "merged")):
        if (email and email in (org.emails or [])) or (phone and phone in (org.phones or [])) or (domain and org.domain == domain):
            out.organizations.append(org)
    if email:
        out.persons.extend(session.scalars(select(Person).where(Person.email == email)))
    if phone:
        out.persons.extend(session.scalars(select(Person).where(Person.phone_e164 == phone)))
    return out


def anonymise_organization(
    session: Session, org: Organization, *, reason: str, actor: User | None = None,
    suppress: bool = True,
) -> None:
    """Remove contact data and persons; keep non-identifying technical history."""
    if suppress:
        if org.domain:
            add_suppression(session, "domain", org.domain, reason=reason, actor=actor)
        for email in org.emails or []:
            add_suppression(session, "email", email, reason=reason, actor=actor)
        for phone in org.phones or []:
            add_suppression(session, "phone", phone, reason=reason, actor=actor)
    for person in list(org.persons):
        session.delete(person)
    for ev in list(org.evidence):
        session.delete(ev)
    org.display_name = None
    org.legal_name = None
    org.trade_name = None
    org.phones = []
    org.emails = []
    org.social_links = {}
    org.registry_ids = {}
    org.address_text = None
    org.postal_code = None
    org.notes = None
    org.website_url = None
    org.domain = None
    for site in org.websites:
        site.url = "anonymised"
        site.final_url = None
        site.host_key = None
        site.host_ip = None
        for snap in list(site.snapshots):
            session.delete(snap)
    org.status = "anonymised"
    org.updated_at = utcnow()
    audit.log(session, "organization.anonymise", actor=actor, entity_type="organization",
              entity_id=org.id, reason=reason)


def erase_person(session: Session, person: Person, *, reason: str, actor: User | None = None) -> None:
    if person.email:
        add_suppression(session, "email", person.email, reason=reason, actor=actor)
    if person.phone_e164:
        add_suppression(session, "phone", person.phone_e164, reason=reason, actor=actor)
    audit.log(session, "person.erase", actor=actor, entity_type="person", entity_id=person.id,
              organization_id=person.organization_id, reason=reason)
    if person.organization is not None and person in person.organization.persons:
        person.organization.persons.remove(person)  # keep the parent collection consistent for callers that continue
    session.delete(person)
    session.flush()
