"""Per-source storage policies (R3). Applied before anything from a connector is persisted."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol

from ..util import utcnow


class StoragePolicy(Protocol):
    name: str

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Return only the fields the source's terms allow us to persist."""


class PlacesStoragePolicy:
    """Google Maps Platform terms: place_id may be stored indefinitely, coordinates for up to 30
    consecutive days, nothing else. Names, phones, websites, ratings, hours are *not* returned
    here and therefore can never reach the database from this connector."""

    name = "google_places"
    ALLOWED_KEYS = ("place_id", "lat", "lng", "coords_expires_at")

    def __init__(self, coords_days: int = 30) -> None:
        self.coords_days = coords_days

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        loc = raw.get("location") or {}
        return {
            "place_id": raw["id"],
            "lat": loc.get("latitude"),
            "lng": loc.get("longitude"),
            "coords_expires_at": utcnow() + timedelta(days=self.coords_days),
        }


class PassthroughPolicy:
    """Sources whose extracted facts may be stored (business's own website, imports, manual)."""

    name = "passthrough"

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)


class DropAllPolicy:
    """Connectors that must persist nothing themselves (e.g. live attribution lookups)."""

    name = "drop_all"

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {}


class GithubOrgPolicy:
    """GitHub Acceptable Use §7: organisation-level public metadata only — never members, never e-mails,
    never used for unsolicited e-mail (the data is routed to the agency track for 1:1 outreach only)."""

    name = "github_orgs"
    ALLOWED_KEYS = ("login", "name", "html_url", "blog", "location", "public_repos", "wordpress_repos", "description")

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        out = {k: raw.get(k) for k in self.ALLOWED_KEYS if raw.get(k) not in (None, "")}
        out.pop("email", None)
        return out


class CruxPolicy:
    """CrUX API: aggregate p75 metrics per origin — no user-level data exists; keep only the numbers and the period."""

    name = "crux"
    ALLOWED_KEYS = ("lcp_p75", "inp_p75", "cls_p75", "ttfb_p75", "origin_fallback", "collection_period")

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {k: raw.get(k) for k in self.ALLOWED_KEYS if k in raw}


class CompaniesHousePolicy:
    """Companies House public register (OGL): company facts may be stored with provenance; officer (person) rows
    are displayed live and audited but never persisted (R2)."""

    name = "companies_house"
    ALLOWED_KEYS = ("company_number", "name", "status", "type", "created", "address", "postal_code", "locality", "sic_codes")

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        out = {k: raw.get(k) for k in self.ALLOWED_KEYS if raw.get(k) not in (None, "", [])}
        out.pop("officers", None)
        return out


class LinkedInOidcPolicy:
    """Sign In with LinkedIn (OIDC): keep the verified identity fields the person consented to share; the access
    token is used once for /v2/userinfo and discarded, never stored."""

    name = "linkedin_oidc"
    ALLOWED_KEYS = ("sub", "name", "given_name", "family_name", "email", "email_verified", "picture")

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        out = {k: raw.get(k) for k in self.ALLOWED_KEYS if raw.get(k) not in (None, "")}
        for forbidden in ("access_token", "id_token", "refresh_token"):
            out.pop(forbidden, None)
        return out


class ConnectionsCsvPolicy:
    """Owner's own LinkedIn Connections.csv: name, profile URL, company, position, connected-on and (if the
    connection shared it) e-mail. Legitimate interest with an LIA; one-to-one contact only; excluded from bulk exports."""

    name = "linkedin_connections"
    ALLOWED_KEYS = ("first_name", "last_name", "url", "email", "company", "position", "connected_on")

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {k: raw.get(k) for k in self.ALLOWED_KEYS if raw.get(k) not in (None, "")}


POLICIES: dict[str, StoragePolicy] = {
    "google_places": PlacesStoragePolicy(),
    "google_places_details": DropAllPolicy(),
    "own_website": PassthroughPolicy(),
    "csv_import": PassthroughPolicy(),
    "manual": PassthroughPolicy(),
    "pagespeed": PassthroughPolicy(),
    "dns_asn": PassthroughPolicy(),
    "inbound_webhook": PassthroughPolicy(),
    "linkedin_oidc": LinkedInOidcPolicy(),
    "linkedin_connections": ConnectionsCsvPolicy(),
    "crux": CruxPolicy(),
    "github_orgs": GithubOrgPolicy(),
    "httparchive_feed": PassthroughPolicy(),  # origins + aggregate CWV only; nothing personal exists in the dataset
    "companies_house": CompaniesHousePolicy(),
}
