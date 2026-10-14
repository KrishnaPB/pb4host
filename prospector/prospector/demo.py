"""Synthetic demo data: obviously fake organisations so the UI can be explored without any API key.

Everything here is invented — `.example` is a reserved TLD that can never resolve, names carry a
`Demo:` prefix, and the evidence source is `demo_seed` so rows are easy to find and delete.
"""

from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy.orm import Session

from .core.facts import Fact, apply_facts
from .core.organizations import ensure_website
from .core.scoring import score_organization
from .models import Organization, PerformanceSnapshot
from .util import utcnow

CITIES = ["Mangaluru", "Udupi", "Bengaluru", "Mysuru", "Hubballi", "Kochi", "Goa"]
SMB_KINDS = ["Bakery", "Dental Clinic", "Boutique", "Interiors", "Tours", "Fitness Studio", "Ayurveda Centre", "Hardware"]
AGENCY_KINDS = ["Digital", "Creative Studio", "Web Works", "Media Labs", "Design Co"]
BUILDERS = [None, "elementor", "wpbakery", "divi", None]
HOSTS = [("Hostinger International", "shared"), ("GoDaddy.com LLC", "shared"), ("Amazon.com Inc.", "cloud"),
         ("Cloudflare Inc.", "cdn_proxied"), ("WP Engine", "managed_wp"), ("DigitalOcean LLC", "cloud")]
ERRORS = ["none"] * 6 + ["db_error", "php_error", "server_error", "tls_invalid"]
SOURCE = "demo_seed"


def seed_demo(session: Session, count: int = 12, seed: int = 42) -> int:
    rng = random.Random(seed)
    now = utcnow()
    created = 0
    for i in range(count):
        is_agency = i % 3 == 2
        kind = rng.choice(AGENCY_KINDS if is_agency else SMB_KINDS)
        city = rng.choice(CITIES)
        name = f"Demo: {city} {kind} {i + 1}"
        slug = f"demo-{city.lower()}-{kind.lower().replace(' ', '-')}-{i + 1}"
        domain = f"{slug}.example"
        url = f"https://{domain}/"
        org = Organization(domain=domain, website_url=url, country="IN", city=city, track="agency" if is_agency else "smb", status="new")
        session.add(org)
        session.flush()
        site = ensure_website(session, org, url)
        site.final_url = url
        site.http_status = 200
        site.last_probed_at = now - timedelta(hours=rng.randint(1, 48))
        site.probe_count = 1
        site.robots_allowed = True
        cms = "wordpress" if rng.random() < 0.75 else rng.choice(["shopify", "wix", None])
        facts = [
            Fact("organization.display_name", name, SOURCE, url, 0.9, now),
            Fact("organization.website_url", url, SOURCE, url, 1.0, now),
            Fact("organization.city", city, SOURCE, url + "contact/", 0.8, now),
            Fact("organization.country", "IN", SOURCE, url, 0.8, now),
            Fact("organization.phone", f"+91 824 {rng.randint(2000000, 2999999)}", SOURCE, url + "contact/", 0.9, now),
            Fact("organization.email", f"info@{domain}", SOURCE, url + "contact/", 0.9, now),
            Fact("organization.track", org.track, SOURCE, url, 0.7, now),
            Fact("website.http_status", "200", SOURCE, url, 1.0, now),
        ]
        signals: list[str] = []
        if cms:
            facts.append(Fact("website.cms", cms, SOURCE, url, 0.95, now))
        if cms == "wordpress":
            signals.append("wordpress")
            version = rng.choice(["6.8.2", "6.7.1", "6.4.3", "5.9.3", None])
            if version:
                facts.append(Fact("website.cms_version", version, SOURCE, url, 0.9, now))
                signals.append("wp_version_exposed")
            if rng.random() < 0.4:
                facts.append(Fact("website.is_woocommerce", "true", SOURCE, url, 0.9, now))
                signals.append("woocommerce")
            builder = rng.choice(BUILDERS)
            if builder:
                facts.append(Fact("website.page_builder", builder, SOURCE, url, 0.8, now))
                signals.append(f"page_builder:{builder}")
            if rng.random() < 0.5:
                facts.append(Fact("website.caching_layer", rng.choice(["wp-rocket", "litespeed-cache", "w3-total-cache"]), SOURCE, url, 0.8, now))
            else:
                signals.append("no_caching_detected")
            php = rng.choice([None, "7.4.33", "8.1.27", "8.2.12"])
            if php:
                facts.append(Fact("website.php_version", php, SOURCE, url, 0.9, now))
                signals.append("php_exposed")
        host, host_class = rng.choice(HOSTS)
        facts += [
            Fact("website.host_provider", host, SOURCE, f"dns://{domain}", 0.9, now),
            Fact("website.host_class", host_class, SOURCE, f"dns://{domain}", 0.7, now),
            Fact("website.host_asn", f"AS{rng.randint(10000, 60000)}", SOURCE, f"dns://{domain}", 1.0, now),
            Fact("website.mail_provider", rng.choice(["google_workspace", "microsoft_365", "zoho", "other"]), SOURCE, f"dns://{domain}", 0.9, now),
            Fact("website.tls_valid", "true", SOURCE, url, 1.0, now),
        ]
        error = rng.choice(ERRORS)
        if error != "none":
            facts.append(Fact("website.error_state", error, SOURCE, url, 1.0, now))
            if error == "tls_invalid":
                facts.append(Fact("website.tls_valid", "false", SOURCE, url, 1.0, now))
        if is_agency:
            signals += ["agency_keywords", "portfolio_page"]
            facts.append(Fact("organization.social", f"linkedin=https://www.linkedin.com/company/{slug}", SOURCE, url, 0.9, now))
        if rng.random() < 0.3:
            facts.append(Fact("organization.registry.gstin", f"29ABCDE{rng.randint(1000, 9999)}F1Z{rng.randint(1, 9)}", SOURCE, url + "about/", 0.8, now))
            signals.append("gstin_published")
        if host_class == "managed_wp":
            signals.append(f"managed_wp_host:{host.lower().replace(' ', '')}")
        apply_facts(session, org, facts, site)
        org.signals = signals
        if error != "none":
            site.error_first_seen_at = now - timedelta(days=2)
            site.error_confirmed_at = now - timedelta(days=1)
        if cms == "wordpress" or is_agency:
            lh = rng.randint(18, 92)
            session.add(PerformanceSnapshot(
                website=site, strategy="mobile", lh_performance=lh,
                lcp_ms=int(rng.uniform(1800, 7000)), tbt_ms=int(rng.uniform(50, 1800)), cls=round(rng.uniform(0, 0.4), 3),
                ttfb_ms=int(rng.uniform(200, 2500)),
                field_lcp_p75=int(rng.uniform(1800, 6500)) if rng.random() < 0.6 else None,
                field_inp_p75=int(rng.uniform(100, 700)) if rng.random() < 0.6 else None,
                field_cls_p75=round(rng.uniform(0, 0.3), 3) if rng.random() < 0.6 else None,
                field_available=rng.random() < 0.6, source="demo_seed", runs=1, raw_json={"demo": True},
            ))
        session.flush()
        score_organization(session, org)
        created += 1
    return created
