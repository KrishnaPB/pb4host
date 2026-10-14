from prospector.connectors.website_probe.health import TlsInfo
from prospector.connectors.website_probe.probe import WebsiteProbeConnector
from tests.conftest import FixtureSite, html_fixture


def tls_ok(url):
    return TlsInfo(valid=True, expires_at=None, error=None)


def facts_by_field(result):
    out = {}
    for f in result.facts:
        out.setdefault(f.field, []).append(f.value)
    return out


def test_probe_wordpress_site_collects_facts_with_provenance(session, settings, wp_site):
    probe = WebsiteProbeConnector(settings, client=wp_site.client(), tls_lookup=tls_ok)
    result = probe.probe(session, "coastalbakes.example", country_hint="IN")
    assert result.final_url == "https://coastalbakes.example/" and result.status == 200
    assert result.error_state == "none" and not result.robots_blocked
    facts = facts_by_field(result)
    assert facts["website.cms"] == ["wordpress"]
    assert facts["website.cms_version"] == ["6.4.3"]
    assert facts["website.is_woocommerce"] == [True]
    assert facts["website.page_builder"] == ["elementor"]
    assert facts["website.php_version"] == ["7.4.33"]
    assert "+918242456789" in facts["organization.phone"]
    assert "orders@coastalbakes.example" in facts["organization.email"] or "hello@coastalbakes.example" in facts["organization.email"]
    # a person's mailbox on the contact page is never stored automatically
    assert "priya.nair@coastalbakes.example" not in facts.get("organization.email", [])
    assert facts["organization.registry.gstin"] == ["29AAACC1234F1ZP"]
    assert facts["organization.city"] == ["Mangaluru"]
    assert any(v.startswith("linkedin=") for v in facts["organization.social"])
    for f in result.facts:
        assert f.source == "own_website" and f.source_url.startswith("https://coastalbakes.example/")
    assert {"wordpress", "woocommerce", "page_builder:elementor", "php_exposed", "gstin_published"} <= set(result.signals)
    assert result.pages_fetched >= 2  # homepage + contact page
    assert wp_site.requests[0].endswith("/robots.txt")
    assert any(u.endswith("/contact-us/") for u in wp_site.requests)
    assert len(result.snapshots) == result.pages_fetched


def test_probe_respects_robots_disallow(session, settings):
    site = FixtureSite({"/": (200, html_fixture("wp_home.html"), None)}, robots="User-agent: *\nDisallow: /\n")
    probe = WebsiteProbeConnector(settings, client=site.client(), tls_lookup=tls_ok)
    result = probe.probe(session, "https://blocked.example/")
    assert result.robots_blocked
    assert result.pages_fetched == 0
    assert not any(u.rstrip("/").endswith("blocked.example") and not u.endswith("robots.txt") for u in site.requests)
    assert facts_by_field(result).get("website.cms") is None


def test_probe_classifies_database_error(session, settings):
    site = FixtureSite({"/": (500, html_fixture("db_error.html"), None)})
    probe = WebsiteProbeConnector(settings, client=site.client(), tls_lookup=tls_ok)
    result = probe.probe(session, "https://broken.example/")
    assert result.error_state == "db_error"
    facts = facts_by_field(result)
    assert facts["website.error_state"] == ["db_error"]
    assert facts["website.http_status"] == [500]


def test_probe_agency_site_emits_agency_signal(session, settings):
    site = FixtureSite({"/": (200, html_fixture("agency_home.html"), None)})
    probe = WebsiteProbeConnector(settings, client=site.client(), tls_lookup=tls_ok)
    result = probe.probe(session, "https://www.pixelgrove.example/")
    assert "agency_keywords" in result.signals and "portfolio_page" in result.signals
    assert facts_by_field(result)["website.caching_layer"] == ["wp_rocket"]


def test_probe_never_requests_active_paths_by_default(session, settings, wp_site):
    probe = WebsiteProbeConnector(settings, client=wp_site.client(), tls_lookup=tls_ok)
    probe.probe(session, "https://coastalbakes.example/")
    assert not any("/wp-json" in u or "readme.html" in u or "xmlrpc" in u for u in wp_site.requests)
