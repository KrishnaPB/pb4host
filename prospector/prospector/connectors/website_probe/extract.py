"""Extraction of business facts from HTML: name, contact details, registry IDs, social links,
address from JSON-LD, agency signals and internal links worth visiting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ...core.normalize import (
    find_emails,
    find_phones,
    normalize_email,
    normalize_phone,
    registered_domain,
)

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
CIN_RE = re.compile(r"\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b")
UK_COMPANY_RE = re.compile(r"\b(?:company|registered|reg\.?)\s*(?:no\.?|number)[:\s]*([0-9]{8}|[A-Z]{2}[0-9]{6})\b", re.IGNORECASE)
GENERIC_TITLE_WORDS = {
    "home", "homepage", "welcome", "index", "official site", "official website", "main page",
    "default", "untitled", "just another wordpress site", "coming soon", "under construction",
}
AGENCY_KEYWORDS = [
    "web design", "website design", "web development", "website development", "digital agency",
    "design agency", "creative agency", "digital marketing agency", "we build websites",
    "wordpress development", "wordpress agency", "seo agency", "branding agency", "our clients",
    "case studies", "web solutions", "ecommerce development", "app development company",
]
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin", "www.linkedin.com": "linkedin", "in.linkedin.com": "linkedin",
    "facebook.com": "facebook", "www.facebook.com": "facebook", "m.facebook.com": "facebook",
    "instagram.com": "instagram", "www.instagram.com": "instagram",
    "twitter.com": "x", "www.twitter.com": "x", "x.com": "x", "www.x.com": "x",
    "youtube.com": "youtube", "www.youtube.com": "youtube",
    "github.com": "github", "www.github.com": "github",
}
_CONTACT_LINK_RE = re.compile(r"(contact|get-in-touch|reach-us)", re.IGNORECASE)
_ABOUT_LINK_RE = re.compile(r"(about|who-we-are|our-story|company)", re.IGNORECASE)
_PORTFOLIO_LINK_RE = re.compile(r"(portfolio|our-work|/work\b|case-stud|projects)", re.IGNORECASE)


@dataclass
class PageFacts:
    url: str
    title: str | None = None
    site_name: str | None = None
    site_name_source: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    gstin: str | None = None
    cin: str | None = None
    company_number: str | None = None
    social: dict[str, str] = field(default_factory=dict)
    address: dict[str, str] = field(default_factory=dict)
    agency_hits: list[str] = field(default_factory=list)
    contact_links: list[str] = field(default_factory=list)
    about_links: list[str] = field(default_factory=list)
    has_portfolio_link: bool = False
    text: str = ""


def _clean_title(title: str) -> str | None:
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:»·]\s+|\s*::\s*", title) if p.strip()]
    parts = [p for p in parts if p.lower() not in GENERIC_TITLE_WORDS]
    if not parts:
        return None
    # Site names are usually the shortest brand-like segment; prefer the last if it looks short
    parts_sorted = sorted(parts, key=len)
    candidate = parts_sorted[0] if len(parts_sorted[0]) >= 3 else parts[-1]
    return candidate[:120]


def _walk_jsonld(node, out: list[dict]) -> None:
    if isinstance(node, dict):
        out.append(node)
        for key in ("@graph", "publisher", "itemListElement", "mainEntity"):
            if key in node:
                _walk_jsonld(node[key], out)
    elif isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out)


ORG_TYPES = {
    "organization", "localbusiness", "corporation", "professionalservice", "store", "restaurant",
    "medicalbusiness", "legalservice", "financialservice", "homeandconstructionbusiness",
    "educationalorganization", "ngo", "website", "dentist", "physician", "hotel", "lodgingbusiness",
    "automotivebusiness", "foodestablishment", "healthandbeautybusiness", "sportsactivitylocation",
}


def extract(html: str, url: str, phone_region: str = "IN") -> PageFacts:
    facts = PageFacts(url=url)
    tree = LexborHTMLParser(html or "")
    title_node = tree.css_first("title")
    if title_node is not None:
        facts.title = " ".join(title_node.text().split())[:200] or None

    # JSON-LD
    jsonld_nodes: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        try:
            _walk_jsonld(json.loads(raw), jsonld_nodes)
        except (json.JSONDecodeError, TypeError):
            continue
    for item in jsonld_nodes:
        types = item.get("@type") or []
        if isinstance(types, str):
            types = [types]
        types_l = {str(t).lower() for t in types}
        if types_l & ORG_TYPES and isinstance(item.get("name"), str) and not facts.site_name:
            if "website" in types_l and len(types_l) == 1 and len(jsonld_nodes) > 1:
                pass  # prefer the Organization node when both exist
            facts.site_name = item["name"].strip()[:120]
            facts.site_name_source = "jsonld"
        addr = item.get("address")
        if isinstance(addr, dict) and not facts.address:
            facts.address = {
                k: str(addr[k]).strip()
                for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
                if addr.get(k)
            }
        tel = item.get("telephone")
        if isinstance(tel, str):
            e164 = normalize_phone(tel, phone_region)
            if e164 and e164 not in facts.phones:
                facts.phones.append(e164)
        mail = item.get("email")
        if isinstance(mail, str):
            em = normalize_email(mail.replace("mailto:", ""))
            if em and em not in facts.emails:
                facts.emails.append(em)
        same_as = item.get("sameAs")
        if isinstance(same_as, str):
            same_as = [same_as]
        for link in same_as or []:
            if isinstance(link, str):
                _register_social(facts, link)

    if not facts.site_name:
        for selector, attr in (
            ('meta[property="og:site_name"]', "content"),
            ('meta[name="application-name"]', "content"),
        ):
            node = tree.css_first(selector)
            if node is not None and node.attributes.get(attr):
                facts.site_name = node.attributes[attr].strip()[:120]
                facts.site_name_source = selector
                break
    if not facts.site_name and facts.title:
        cleaned = _clean_title(facts.title)
        if cleaned:
            facts.site_name = cleaned
            facts.site_name_source = "title"

    # Links
    base_host = urlsplit(url).netloc.lower()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if href.lower().startswith("mailto:"):
            em = normalize_email(href[7:].split("?")[0])
            if em and em not in facts.emails:
                facts.emails.append(em)
            continue
        if href.lower().startswith("tel:"):
            e164 = normalize_phone(href[4:].split("?")[0], phone_region)
            if e164 and e164 not in facts.phones:
                facts.phones.append(e164)
            continue
        absolute = urljoin(url, href)
        parts = urlsplit(absolute)
        if parts.netloc.lower() in SOCIAL_HOSTS:
            _register_social(facts, absolute)
            continue
        if parts.netloc.lower().removeprefix("www.") != base_host.removeprefix("www."):
            continue
        path = parts.path or "/"
        text = (a.text() or "").strip().lower()
        if _CONTACT_LINK_RE.search(path) or text in {"contact", "contact us", "get in touch"}:
            if absolute not in facts.contact_links:
                facts.contact_links.append(absolute)
        elif (_ABOUT_LINK_RE.search(path) or text in {"about", "about us"}) and absolute not in facts.about_links:
            facts.about_links.append(absolute)
        if _PORTFOLIO_LINK_RE.search(path) or text in {"portfolio", "our work", "case studies"}:
            facts.has_portfolio_link = True

    # Visible text
    for bad in tree.css("script, style, noscript, svg"):
        bad.decompose()
    body = tree.body
    text = body.text(separator=" ", strip=True) if body is not None else ""
    facts.text = text[:200_000]

    own = registered_domain(url)
    for email in find_emails(text):
        if email not in facts.emails:
            facts.emails.append(email)
    # Prefer same-domain mailboxes first
    facts.emails.sort(key=lambda e: 0 if own and e.endswith("@" + own) else 1)
    for phone in find_phones(text, phone_region):
        if phone not in facts.phones:
            facts.phones.append(phone)

    gst = GSTIN_RE.search(text)
    if gst:
        facts.gstin = gst.group(0)
    cin = CIN_RE.search(text)
    if cin:
        facts.cin = cin.group(0)
    ukm = UK_COMPANY_RE.search(text)
    if ukm:
        facts.company_number = ukm.group(1).upper()

    lowered = text.lower()
    facts.agency_hits = [kw for kw in AGENCY_KEYWORDS if kw in lowered]
    return facts


def _register_social(facts: PageFacts, link: str) -> None:
    parts = urlsplit(link)
    network = SOCIAL_HOSTS.get(parts.netloc.lower())
    if not network or network in facts.social:
        return
    path = parts.path.rstrip("/")
    if not path or path in {"/share", "/sharer.php", "/intent/tweet", "/sharer"}:
        return
    if "share" in path or "intent" in path:
        return
    facts.social[network] = f"{parts.scheme}://{parts.netloc}{path}"
