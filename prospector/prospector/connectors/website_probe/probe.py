"""Website probe connector: homepage + contact/about pages → facts with provenance."""

from __future__ import annotations

import gzip
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ...core.facts import Fact
from ...core.normalize import host_key, is_generic_mailbox, normalize_url, registered_domain
from ...util import slugify, utcnow
from ..base import Connector, register
from .extract import PageFacts, extract
from .fetcher import Fetcher, PathNotAllowed
from .fingerprint import Fingerprint, fingerprint
from .health import TlsInfo, classify, tls_info


@dataclass
class ProbeResult:
    requested_url: str
    final_url: str | None
    host_key: str | None
    status: int | None
    fetch_error: str | None
    robots_blocked: bool
    error_state: str
    fingerprint: Fingerprint
    tls: TlsInfo
    facts: list[Fact] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    pages: list[PageFacts] = field(default_factory=list)
    pages_fetched: int = 0
    snapshots: list[tuple[str, str]] = field(default_factory=list)  # (source_url, html)

    @property
    def reachable(self) -> bool:
        return self.final_url is not None and self.status is not None


@register
class WebsiteProbeConnector(Connector):
    name = "website_probe"
    terms_url = "https://www.pb4host.com/prospector"  # our own published crawler policy
    requires_terms_review = False
    default_daily_cap = 5000
    cost_per_call_usd = 0.0

    def __init__(
        self,
        settings=None,
        client: httpx.Client | None = None,
        fetcher: Fetcher | None = None,
        tls_lookup: Callable[[str], TlsInfo] | None = None,
    ) -> None:
        super().__init__(settings)
        self.fetcher = fetcher or Fetcher(self.settings, client=client)
        self.tls_lookup = tls_lookup or tls_info

    def probe(self, session, url_hint: str, *, country_hint: str | None = None, homepage_only: bool = False) -> ProbeResult:
        url = normalize_url(url_hint)
        if url is None:
            raise ValueError(f"invalid URL hint {url_hint!r}")
        # count one "call" per site, not per page, against the crawler cap (R11 still applies)
        self.reserve_calls(session, 1)
        now = utcnow()
        region = (country_hint or self.settings.default_phone_region or "IN").upper()
        home = self.fetcher.get(url)
        pages_fetched = 1 if not home.robots_blocked else 0

        fp = fingerprint(home.text, home.headers) if home.ok else Fingerprint()
        error_state = classify(home.status, home.text, home.error) if not home.robots_blocked else "none"
        final_url = home.final_url
        tls = TlsInfo(valid=None, expires_at=None, error="skipped")
        if final_url and final_url.startswith("https://"):
            tls = self.tls_lookup(final_url)
        elif home.error == "tls_invalid":
            tls = TlsInfo(valid=False, expires_at=None, error="handshake")

        result = ProbeResult(
            requested_url=url,
            final_url=final_url,
            host_key=host_key(final_url or url),
            status=home.status,
            fetch_error=home.error,
            robots_blocked=home.robots_blocked,
            error_state=error_state,
            fingerprint=fp,
            tls=tls,
            pages_fetched=pages_fetched,
        )
        if not home.ok:
            self._facts_for_unreachable(result, now)
            return result

        page_url = final_url or url
        home_facts = extract(home.text, page_url, region)
        result.pages.append(home_facts)
        result.snapshots.append((page_url, home.text))

        if not homepage_only:
            extra: list[str] = []
            for link in home_facts.contact_links[:2] + home_facts.about_links[:1]:
                if link not in extra and link.rstrip("/") != page_url.rstrip("/"):
                    extra.append(link)
            for link in extra:
                if pages_fetched >= self.settings.crawler_max_pages:
                    break
                try:
                    res = self.fetcher.get(link)
                except PathNotAllowed:
                    continue
                if res.robots_blocked or not res.ok or not res.text:
                    continue
                pages_fetched += 1
                result.pages.append(extract(res.text, res.final_url or link, region))
                result.snapshots.append((res.final_url or link, res.text))
        result.pages_fetched = pages_fetched

        self._build_facts(result, now)
        return result

    # --- facts -----------------------------------------------------------------------------
    def _facts_for_unreachable(self, result: ProbeResult, now) -> None:
        src = result.requested_url
        facts = [
            Fact("website.error_state", result.error_state, "own_website", src, 0.7, now),
        ]
        if result.status is not None:
            facts.append(Fact("website.http_status", result.status, "own_website", src, 1.0, now))
        if result.tls.valid is not None:
            facts.append(Fact("website.tls_valid", result.tls.valid, "own_website", src, 1.0, now))
        result.facts = self.check_facts(facts)

    def _build_facts(self, result: ProbeResult, now) -> None:
        home = result.pages[0]
        src = home.url
        fp = result.fingerprint
        facts: list[Fact] = [
            Fact("organization.website_url", src, "own_website", src, 1.0, now),
            Fact("website.http_status", result.status, "own_website", src, 1.0, now),
            Fact("website.error_state", result.error_state, "own_website", src, 0.9, now),
        ]
        if home.site_name:
            conf = {"jsonld": 0.95, "title": 0.6}.get(home.site_name_source or "", 0.8)
            facts.append(Fact("organization.display_name", home.site_name, "own_website", src, conf, now))
        if fp.cms:
            facts.append(Fact("website.cms", fp.cms, "own_website", src, fp.cms_confidence, now))
        if fp.cms_version:
            facts.append(Fact("website.cms_version", fp.cms_version, "own_website", src, 0.9, now))
        if fp.cms == "wordpress":
            facts.append(Fact("website.is_woocommerce", fp.is_woocommerce, "own_website", src, 0.9, now))
        for attr in ("theme", "page_builder", "caching_layer", "cdn", "managed_host", "php_version", "server_header"):
            value = getattr(fp, attr)
            if value:
                facts.append(Fact(f"website.{attr}", value, "own_website", src, 0.85, now))
        if result.tls.valid is not None:
            facts.append(Fact("website.tls_valid", result.tls.valid, "own_website", src, 1.0, now))
        if result.tls.expires_at:
            facts.append(Fact("website.tls_expires_at", result.tls.expires_at.isoformat(), "own_website", src, 1.0, now))

        own = registered_domain(src)
        seen_emails: set[str] = set()
        seen_phones: set[str] = set()
        for page in result.pages:
            for email in page.emails:
                if email in seen_emails:
                    continue
                same_domain = bool(own and email.endswith("@" + own))
                # Organisation-first: only generic business mailboxes on the business's own
                # domain are stored automatically; anything that may name a person is skipped.
                if same_domain and is_generic_mailbox(email):
                    seen_emails.add(email)
                    facts.append(Fact("organization.email", email, "own_website", page.url, 0.9, now))
            for phone in page.phones[:3]:
                if phone not in seen_phones:
                    seen_phones.add(phone)
                    facts.append(Fact("organization.phone", phone, "own_website", page.url, 0.8, now))
            if page.gstin:
                facts.append(Fact("organization.registry.gstin", page.gstin, "own_website", page.url, 0.95, now))
            if page.cin:
                facts.append(Fact("organization.registry.cin", page.cin, "own_website", page.url, 0.95, now))
            if page.company_number:
                facts.append(Fact("organization.registry.company_number", page.company_number, "own_website", page.url, 0.8, now))
            for network, link in page.social.items():
                facts.append(Fact("organization.social", f"{network}={link}", "own_website", page.url, 0.9, now))
            if page.address and not any(f.field == "organization.address_text" for f in facts):
                parts = [page.address.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")]
                facts.append(Fact("organization.address_text", ", ".join(p for p in parts if p), "own_website", page.url, 0.9, now))
                if page.address.get("addressLocality"):
                    facts.append(Fact("organization.city", page.address["addressLocality"], "own_website", page.url, 0.9, now))
                if page.address.get("addressRegion"):
                    facts.append(Fact("organization.region", page.address["addressRegion"], "own_website", page.url, 0.9, now))
                if page.address.get("postalCode"):
                    facts.append(Fact("organization.postal_code", page.address["postalCode"], "own_website", page.url, 0.9, now))
                country = page.address.get("addressCountry")
                if country and len(country) == 2:
                    facts.append(Fact("organization.country", country.upper(), "own_website", page.url, 0.9, now))

        signals = list(fp.signals)
        agency_hits = {kw for page in result.pages for kw in page.agency_hits}
        if len(agency_hits) >= 2:
            signals.append("agency_keywords")
        if any(page.has_portfolio_link for page in result.pages):
            signals.append("portfolio_page")
        if any(page.gstin for page in result.pages):
            signals.append("gstin_published")
        result.signals = signals
        result.facts = self.check_facts(facts)


def write_snapshot(data_dir: Path, website_id: int, source_url: str, html: str, ttl_days: int) -> tuple[str, datetime]:
    """Persist a gzipped copy of a fetched page for the raw-retention window (R5)."""

    folder = data_dir / "snapshots" / str(website_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
    name = f"{stamp}-{slugify(urlsplit(source_url).path or 'home')}.html.gz"
    path = folder / name
    with gzip.open(path, "wb") as fh:
        fh.write(html.encode("utf-8", errors="replace"))
    expires: datetime = utcnow() + timedelta(days=ttl_days)
    return str(path), expires
