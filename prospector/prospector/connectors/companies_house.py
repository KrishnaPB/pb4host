"""Companies House public data API (UK registry; plan §9 `companies_house`).

Free API key, HTTP basic auth with the key as username. Company facts (legal name, number,
status, registered office, incorporation date, SIC codes) may be stored with provenance under
the Open Government Licence. Officers are shown live on explicit request and are **never
stored** by this connector: a named person only enters Prospector through the manual capture
form with a lawful basis (R2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import Connector, ConnectorError, register

API = "https://api.company-information.service.gov.uk"
PUBLIC = "https://find-and-update.company-information.service.gov.uk/company/"


@dataclass
class CompanyHit:
    company_number: str
    title: str
    status: str | None = None
    address: str | None = None
    created: str | None = None

    @property
    def url(self) -> str:
        return PUBLIC + self.company_number


@dataclass
class CompanyProfile:
    company_number: str
    name: str
    status: str | None = None
    type: str | None = None
    created: str | None = None
    address: str | None = None
    postal_code: str | None = None
    locality: str | None = None
    sic_codes: list[str] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        return PUBLIC + self.company_number


def format_address(addr: dict[str, Any] | None) -> str | None:
    if not addr:
        return None
    parts = [addr.get(k) for k in ("premises", "address_line_1", "address_line_2", "locality", "region", "postal_code", "country")]
    text = ", ".join(str(p).strip() for p in parts if p)
    return text or None


def parse_profile(data: dict[str, Any]) -> CompanyProfile:
    addr = data.get("registered_office_address") or {}
    return CompanyProfile(
        company_number=str(data.get("company_number") or ""),
        name=str(data.get("company_name") or ""),
        status=data.get("company_status"),
        type=data.get("type"),
        created=data.get("date_of_creation"),
        address=format_address(addr),
        postal_code=addr.get("postal_code"),
        locality=addr.get("locality"),
        sic_codes=[str(c) for c in (data.get("sic_codes") or [])],
        raw=data,
    )


@register
class CompaniesHouseConnector(Connector):
    name = "companies_house"
    terms_url = "https://developer.company-information.service.gov.uk/terms-and-conditions"
    requires_terms_review = True
    default_daily_cap = 500

    def __init__(self, settings=None, client: httpx.Client | None = None) -> None:
        super().__init__(settings)
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=20.0)
        return self._client

    def _get(self, session, path: str, params: dict[str, Any] | None = None) -> Any:
        key = self.settings.companies_house_api_key
        if not key:
            raise ConnectorError("Companies House needs COMPANIES_HOUSE_API_KEY")
        self.reserve_calls(session, 1)
        try:
            resp = self.client.get(API + path, params=params, auth=(key, ""))
        except httpx.HTTPError as exc:
            self.record_error(session, f"transport: {exc}")
            raise ConnectorError(f"Companies House request failed: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            self.record_error(session, "rate limited")
            raise ConnectorError("Companies House rate limit reached (600 requests / 5 minutes); try later")
        if resp.status_code != 200:
            self.record_error(session, f"HTTP {resp.status_code}: {resp.text[:200]}")
            raise ConnectorError(f"Companies House HTTP {resp.status_code} for {path}")
        return resp.json()

    def search(self, session, query: str, *, limit: int = 10) -> list[CompanyHit]:
        data = self._get(session, "/search/companies", {"q": query, "items_per_page": limit}) or {}
        hits = []
        for item in data.get("items") or []:
            if not item.get("company_number"):
                continue
            hits.append(CompanyHit(
                company_number=str(item["company_number"]), title=str(item.get("title") or ""),
                status=item.get("company_status"), address=item.get("address_snippet"), created=item.get("date_of_creation"),
            ))
        return hits

    def profile(self, session, company_number: str) -> CompanyProfile | None:
        data = self._get(session, f"/company/{company_number.strip().upper()}")
        return parse_profile(data) if data else None

    def officers_live(self, session, company_number: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Live view for an analyst (audited by the route). Returned, displayed, discarded."""
        data = self._get(session, f"/company/{company_number.strip().upper()}/officers", {"items_per_page": limit}) or {}
        out = []
        for item in data.get("items") or []:
            out.append({"name": item.get("name"), "role": item.get("officer_role"), "appointed_on": item.get("appointed_on"),
                        "resigned_on": item.get("resigned_on")})
        return out
