"""Google Places API (New) — discovery and linking only (§4.2).

Text Search is called with a minimal field mask. The storage policy keeps `place_id` and
coordinates (30-day expiry); the website URL is handed to the crawler in memory only, and the
display name is used solely to label the job while it is running. Live details for the UI
attribution panel are fetched on demand and never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..compliance.policies import PlacesStoragePolicy
from .base import Candidate, Connector, ConnectorError, register

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.businessStatus,places.websiteUri,"
    "places.types,places.location,nextPageToken"
)
LIVE_FIELD_MASK = (
    "id,displayName,formattedAddress,rating,userRatingCount,businessStatus,googleMapsUri,websiteUri"
)
AGENCY_TYPE_HINTS = {
    "website_designer", "marketing_agency", "advertising_agency", "software_company",
    "graphic_designer", "consultant", "internet_marketing_service",
}


@dataclass
class SearchPage:
    candidates: list[Candidate]
    next_page_token: str | None
    raw_count: int
    without_website: int


@register
class GooglePlacesConnector(Connector):
    name = "google_places"
    terms_url = "https://cloud.google.com/maps-platform/terms/maps-service-terms"
    requires_terms_review = True
    default_daily_cap = 200
    cost_per_call_usd = 0.035  # Text Search (Enterprise SKU) list price per call; verify
    policy = PlacesStoragePolicy()

    def __init__(self, settings=None, client: httpx.Client | None = None) -> None:
        super().__init__(settings)
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=15.0)
        return self._client

    def _headers(self, field_mask: str) -> dict[str, str]:
        key = self.settings.google_maps_api_key
        if not key:
            raise ConnectorError("GOOGLE_MAPS_API_KEY is not configured")
        return {
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": field_mask,
            "Content-Type": "application/json",
        }

    def search_text(
        self,
        session: Session,
        query: str,
        *,
        track: str = "smb",
        region_code: str | None = None,
        page_token: str | None = None,
        page_size: int = 20,
    ) -> SearchPage:
        self.reserve_calls(session, 1)
        body: dict[str, Any] = {
            "textQuery": query,
            "pageSize": min(max(page_size, 1), 20),
            "languageCode": self.settings.places_language_code,
        }
        if region_code or self.settings.places_region_code:
            body["regionCode"] = region_code or self.settings.places_region_code
        if page_token:
            body["pageToken"] = page_token
        try:
            resp = self.client.post(TEXT_SEARCH_URL, json=body, headers=self._headers(SEARCH_FIELD_MASK))
        except httpx.HTTPError as exc:
            self.record_error(session, f"text search transport error: {exc}")
            raise ConnectorError(f"Places text search failed: {exc}") from exc
        if resp.status_code != 200:
            self.record_error(session, f"text search HTTP {resp.status_code}: {resp.text[:300]}")
            raise ConnectorError(f"Places text search HTTP {resp.status_code}")
        data = resp.json()
        candidates: list[Candidate] = []
        without_website = 0
        for raw in data.get("places", []):
            website = raw.get("websiteUri")
            if not website:
                without_website += 1
                continue
            types = set(raw.get("types") or [])
            cand = Candidate(
                provider=self.name,
                external_id=raw["id"],
                track=track,
                persist=self.policy.apply(raw),
                transient={
                    "website": website,
                    "agency_type_hint": bool(types & AGENCY_TYPE_HINTS),
                    "operational": raw.get("businessStatus", "OPERATIONAL") == "OPERATIONAL",
                },
            )
            candidates.append(cand)
        return SearchPage(
            candidates=candidates,
            next_page_token=data.get("nextPageToken"),
            raw_count=len(data.get("places", [])),
            without_website=without_website,
        )

    def live_details(self, session: Session, place_id: str) -> dict[str, Any]:
        """On-demand lookup for the attribution panel. Returned, displayed, never stored."""
        self.reserve_calls(session, 1)
        try:
            resp = self.client.get(
                DETAILS_URL.format(place_id=place_id), headers=self._headers(LIVE_FIELD_MASK)
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Places details failed: {exc}") from exc
        if resp.status_code != 200:
            raise ConnectorError(f"Places details HTTP {resp.status_code}")
        data = resp.json()
        return {
            "place_id": data.get("id"),
            "name": (data.get("displayName") or {}).get("text"),
            "address": data.get("formattedAddress"),
            "rating": data.get("rating"),
            "rating_count": data.get("userRatingCount"),
            "status": data.get("businessStatus"),
            "maps_url": data.get("googleMapsUri"),
            "website": data.get("websiteUri"),
            "attribution": "Powered by Google",
        }
