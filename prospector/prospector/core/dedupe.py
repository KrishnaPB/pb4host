"""Dedupe v1: deterministic keys first, fuzzy name+city last. Merges keep all evidence."""

from __future__ import annotations

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Organization, PlaceLink
from .normalize import host_key, normalize_name, normalize_phone

FUZZY_THRESHOLD = 92


def find_existing(
    session: Session,
    *,
    domain: str | None = None,
    place_id: str | None = None,
    registry_ids: dict[str, str] | None = None,
    phones: list[str] | None = None,
    name: str | None = None,
    city: str | None = None,
    region: str = "IN",
) -> Organization | None:
    active = Organization.status.notin_(("merged",))
    if domain:
        key = host_key(domain)
        if key:
            org = session.scalar(select(Organization).where(Organization.domain == key, active))
            if org:
                return org
    if place_id:
        link = session.scalar(select(PlaceLink).where(PlaceLink.place_id == place_id))
        if link and link.organization.status != "merged":
            return link.organization
    for key, value in (registry_ids or {}).items():
        if not value:
            continue
        for org in session.scalars(select(Organization).where(active)):
            if org.registry_ids.get(key) == value:
                return org
    phone_keys = {p for p in (normalize_phone(p, region) for p in (phones or [])) if p}
    if phone_keys:
        for org in session.scalars(select(Organization).where(active)):
            if phone_keys.intersection(org.phones or []):
                return org
    if name:
        wanted = normalize_name(name)
        if wanted:
            stmt = select(Organization).where(active)
            if city:
                stmt = stmt.where(func.lower(Organization.city) == city.lower())
            best: tuple[int, Organization | None] = (0, None)
            for org in session.scalars(stmt):
                for candidate in (org.display_name, org.legal_name, org.trade_name):
                    score = fuzz.token_set_ratio(wanted, normalize_name(candidate)) if candidate else 0
                    if score > best[0]:
                        best = (int(score), org)
            threshold = FUZZY_THRESHOLD if city else 97
            if best[1] is not None and best[0] >= threshold:
                return best[1]
    return None


def merge_organizations(session: Session, primary: Organization, duplicate: Organization) -> Organization:
    """Move everything from `duplicate` into `primary`; mark the duplicate merged."""
    if primary.id == duplicate.id:
        return primary
    for site in list(duplicate.websites):
        site.organization = primary
    for link in list(duplicate.place_links):
        link.organization = primary
    for ev in list(duplicate.evidence):
        ev.organization = primary
    for person in list(duplicate.persons):
        person.organization = primary
    primary.phones = list(dict.fromkeys([*primary.phones, *duplicate.phones]))
    primary.emails = list(dict.fromkeys([*primary.emails, *duplicate.emails]))
    primary.registry_ids = {**duplicate.registry_ids, **primary.registry_ids}
    primary.social_links = {**duplicate.social_links, **primary.social_links}
    primary.signals = list(dict.fromkeys([*primary.signals, *duplicate.signals]))
    for attr in ("display_name", "legal_name", "website_url", "domain", "city", "country",
                 "address_text", "region", "postal_code"):
        if not getattr(primary, attr) and getattr(duplicate, attr):
            setattr(primary, attr, getattr(duplicate, attr))
    if primary.track == "unknown" and duplicate.track != "unknown":
        primary.track = duplicate.track
    if duplicate.notes:
        primary.notes = ((primary.notes or "") + "\n" + duplicate.notes).strip()
    duplicate.status = "merged"
    duplicate.merged_into_id = primary.id
    duplicate.domain = None
    session.flush()
    return primary
