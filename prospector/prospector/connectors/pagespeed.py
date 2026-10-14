"""Google PageSpeed Insights API v5 — Lighthouse lab metrics plus CrUX field data per URL."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

import httpx

from .base import Connector, ConnectorError, register

PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


@dataclass
class PsiResult:
    strategy: str
    lh_performance: int | None = None
    lcp_ms: int | None = None
    tbt_ms: int | None = None
    cls: float | None = None
    ttfb_ms: int | None = None
    field_lcp_p75: int | None = None
    field_inp_p75: int | None = None
    field_cls_p75: float | None = None
    field_available: bool = False
    field_origin_fallback: bool = False
    runs: int = 0
    raw: dict[str, Any] | None = None
    lighthouse_version: str | None = None
    final_url: str | None = None
    errors: list[str] = field(default_factory=list)


def _audit_num(lhr: dict[str, Any], audit_id: str) -> float | None:
    audit = (lhr.get("audits") or {}).get(audit_id) or {}
    value = audit.get("numericValue")
    return float(value) if isinstance(value, (int, float)) else None


def parse_response(data: dict[str, Any], strategy: str) -> PsiResult:
    res = PsiResult(strategy=strategy, runs=1)
    lhr = data.get("lighthouseResult") or {}
    cats = lhr.get("categories") or {}
    perf = (cats.get("performance") or {}).get("score")
    if isinstance(perf, (int, float)):
        res.lh_performance = round(perf * 100)
    res.lcp_ms = _int(_audit_num(lhr, "largest-contentful-paint"))
    res.tbt_ms = _int(_audit_num(lhr, "total-blocking-time"))
    cls = _audit_num(lhr, "cumulative-layout-shift")
    res.cls = round(cls, 3) if cls is not None else None
    res.ttfb_ms = _int(_audit_num(lhr, "server-response-time"))
    res.lighthouse_version = lhr.get("lighthouseVersion")
    res.final_url = lhr.get("finalDisplayedUrl") or lhr.get("finalUrl")

    exp = data.get("loadingExperience") or {}
    metrics = exp.get("metrics") or {}
    if metrics:
        res.field_available = True
        res.field_origin_fallback = bool(exp.get("origin_fallback"))
        lcp = (metrics.get("LARGEST_CONTENTFUL_PAINT_MS") or {}).get("percentile")
        inp = (metrics.get("INTERACTION_TO_NEXT_PAINT") or {}).get("percentile")
        cls_p = (metrics.get("CUMULATIVE_LAYOUT_SHIFT_SCORE") or {}).get("percentile")
        res.field_lcp_p75 = _int(lcp)
        res.field_inp_p75 = _int(inp)
        res.field_cls_p75 = round(cls_p / 100, 3) if isinstance(cls_p, (int, float)) else None
    res.raw = {
        "lighthouseVersion": res.lighthouse_version,
        "finalUrl": res.final_url,
        "performance": res.lh_performance,
        "audits": {k: (lhr.get("audits") or {}).get(k, {}).get("numericValue") for k in (
            "largest-contentful-paint", "total-blocking-time", "cumulative-layout-shift",
            "server-response-time", "speed-index", "interactive")},
        "loadingExperience": {"metrics": metrics, "origin_fallback": exp.get("origin_fallback")},
    }
    return res


def _int(value: float | None) -> int | None:
    return round(value) if isinstance(value, (int, float)) else None


def median_of(results: list[PsiResult]) -> PsiResult:
    if len(results) == 1:
        return results[0]
    base = results[-1]
    out = PsiResult(strategy=base.strategy, runs=len(results), raw=base.raw,
                    lighthouse_version=base.lighthouse_version, final_url=base.final_url,
                    field_available=base.field_available, field_origin_fallback=base.field_origin_fallback,
                    field_lcp_p75=base.field_lcp_p75, field_inp_p75=base.field_inp_p75,
                    field_cls_p75=base.field_cls_p75)
    for attr in ("lh_performance", "lcp_ms", "tbt_ms", "ttfb_ms"):
        values = [getattr(r, attr) for r in results if getattr(r, attr) is not None]
        setattr(out, attr, round(statistics.median(values)) if values else None)
    cls_values = [r.cls for r in results if r.cls is not None]
    out.cls = round(statistics.median(cls_values), 3) if cls_values else None
    return out


@register
class PageSpeedConnector(Connector):
    name = "pagespeed"
    terms_url = "https://developers.google.com/speed/docs/insights/v5/get-started"
    requires_terms_review = True
    default_daily_cap = 2000
    cost_per_call_usd = 0.0

    def __init__(self, settings=None, client: httpx.Client | None = None) -> None:
        super().__init__(settings)
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=90.0)
        return self._client

    def run(self, session, url: str, strategy: str = "mobile", runs: int | None = None) -> PsiResult:
        runs = runs or self.settings.psi_runs
        self.reserve_calls(session, runs)
        params: dict[str, Any] = {"url": url, "strategy": strategy, "category": "performance"}
        if self.settings.psi_api_key:
            params["key"] = self.settings.psi_api_key
        results: list[PsiResult] = []
        errors: list[str] = []
        for _ in range(runs):
            try:
                resp = self.client.get(PSI_URL, params=params)
            except httpx.HTTPError as exc:
                errors.append(f"transport: {exc}")
                continue
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            try:
                results.append(parse_response(resp.json(), strategy))
            except (ValueError, KeyError) as exc:
                errors.append(f"parse: {exc}")
        if not results:
            self.record_error(session, "; ".join(errors)[:500])
            raise ConnectorError(f"PageSpeed failed for {url}: {'; '.join(errors)[:300]}")
        out = median_of(results)
        out.errors = errors
        return out
