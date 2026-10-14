"""Chrome UX Report API — real-user p75 metrics per origin (field data without a Lighthouse run).

`POST https://chromeuxreport.googleapis.com/v1/records:queryRecord?key=…` with
`{"origin": "https://example.com", "formFactor": "PHONE"}`. A 404 means Chrome has no data for
the origin, which is information, not an error (`available=False`). Snapshots are stored with
`source="crux_api"`; the scorer merges them with PageSpeed lab metrics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .base import Connector, ConnectorError, register

CRUX_URL = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
FORM_FACTORS = {"mobile": "PHONE", "desktop": "DESKTOP"}


@dataclass
class CruxResult:
    strategy: str
    available: bool = False
    lcp_p75: int | None = None
    inp_p75: int | None = None
    cls_p75: float | None = None
    ttfb_p75: int | None = None
    fcp_p75: int | None = None
    collection_period: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def lcp_rating(self) -> str | None:
        if self.lcp_p75 is None:
            return None
        return "good" if self.lcp_p75 <= 2500 else "needs_improvement" if self.lcp_p75 <= 4000 else "poor"


def _p75(metrics: dict[str, Any], name: str) -> float | None:
    value = ((metrics.get(name) or {}).get("percentiles") or {}).get("p75")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_record(data: dict[str, Any], strategy: str) -> CruxResult:
    record = data.get("record") or {}
    metrics = record.get("metrics") or {}
    res = CruxResult(strategy=strategy, available=bool(metrics), raw=data)
    lcp = _p75(metrics, "largest_contentful_paint")
    inp = _p75(metrics, "interaction_to_next_paint")
    cls = _p75(metrics, "cumulative_layout_shift")
    ttfb = _p75(metrics, "experimental_time_to_first_byte")
    fcp = _p75(metrics, "first_contentful_paint")
    res.lcp_p75 = int(lcp) if lcp is not None else None
    res.inp_p75 = int(inp) if inp is not None else None
    res.cls_p75 = round(cls, 3) if cls is not None else None
    res.ttfb_p75 = int(ttfb) if ttfb is not None else None
    res.fcp_p75 = int(fcp) if fcp is not None else None
    period = record.get("collectionPeriod") or {}
    first, last = period.get("firstDate") or {}, period.get("lastDate") or {}
    if first and last:
        res.collection_period = (f"{first.get('year')}-{first.get('month'):02d}-{first.get('day'):02d}"
                                 f"..{last.get('year')}-{last.get('month'):02d}-{last.get('day'):02d}")
    return res


@register
class CruxConnector(Connector):
    name = "crux"
    terms_url = "https://developer.chrome.com/docs/crux/api"
    requires_terms_review = True
    default_daily_cap = 1000

    def __init__(self, settings=None, client: httpx.Client | None = None) -> None:
        super().__init__(settings)
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=20.0)
        return self._client

    def query(self, session, origin: str, strategy: str = "mobile") -> CruxResult:
        key = self.settings.crux_api_key or self.settings.psi_api_key
        if not key:
            raise ConnectorError("CrUX needs CRUX_API_KEY or PSI_API_KEY")
        self.reserve_calls(session, 1)
        body = {"origin": origin.rstrip("/"), "formFactor": FORM_FACTORS.get(strategy, "PHONE")}
        try:
            resp = self.client.post(CRUX_URL, params={"key": key}, json=body)
        except httpx.HTTPError as exc:
            self.record_error(session, f"transport: {exc}")
            raise ConnectorError(f"CrUX request failed for {origin}: {exc}") from exc
        if resp.status_code == 404:
            return CruxResult(strategy=strategy, available=False, raw=None)
        if resp.status_code != 200:
            self.record_error(session, f"HTTP {resp.status_code}: {resp.text[:200]}")
            raise ConnectorError(f"CrUX HTTP {resp.status_code} for {origin}")
        return parse_record(resp.json(), strategy)
