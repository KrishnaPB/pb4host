"""Shared organisation/website creation helpers used by discovery, imports and manual capture."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Organization, Website
from .dedupe import find_existing
from .normalize import host_key, normalize_url


@dataclass
class Resolved:
    organization: Organization
    created: bool


def resolve_organization(
    session: Session,
    *,
    website: str | None = None,
    name: str | None = None,
    city: str | None = None,
    country: str | None = None,
    place_id: str | None = None,
    phones: list[str] | None = None,
    registry_ids: dict[str, str] | None = None,
    track: str = "unknown",
) -> Resolved:
    domain = host_key(website) if website else None
    existing = find_existing(
        session,
        domain=domain,
        place_id=place_id,
        registry_ids=registry_ids,
        phones=phones,
        name=name,
        city=city,
    )
    if existing is not None:
        if existing.track == "unknown" and track != "unknown":
            existing.track = track
        return Resolved(existing, False)
    org = Organization(
        domain=domain,
        website_url=normalize_url(website) if website else None,
        country=(country or None),
        city=city or None,
        track=track if track in {"smb", "agency", "unknown"} else "unknown",
        status="new",
    )
    session.add(org)
    session.flush()
    return Resolved(org, True)


def ensure_website(session: Session, org: Organization, url: str) -> Website:
    norm = normalize_url(url) or url
    key = host_key(norm)
    for site in org.websites:
        if site.host_key == key or site.url == norm:
            return site
    site = Website(organization=org, url=norm, host_key=key)
    session.add(site)
    session.flush()
    return site
