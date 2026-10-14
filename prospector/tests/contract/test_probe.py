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
    assert "organization.display_name" not in facts  # "Database Error" is not a business name


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


def test_probe_refuses_private_and_loopback_hosts(session, settings):
    from prospector.connectors.website_probe.fetcher import host_is_public

    assert host_is_public("127.0.0.1") is False
    assert host_is_public("10.0.0.5") is False
    assert host_is_public("169.254.169.254") is False
    assert host_is_public("[::1]") is False
    assert host_is_public("localhost") is False
    assert host_is_public("8.8.8.8") is True  # 203.0.113.0/24 is TEST-NET, i.e. reserved, so it is correctly blocked
    assert host_is_public("203.0.113.9") is False
    assert host_is_public("does-not-resolve.example") is None
    site = FixtureSite({"/": (200, html_fixture("wp_home.html"), None)})
    probe = WebsiteProbeConnector(settings, client=site.client(), tls_lookup=tls_ok)
    for url in ("http://127.0.0.1:8000/", "http://169.254.169.254/", "http://localhost/", "http://10.1.2.3/contact/"):
        result = probe.probe(session, url)
        assert result.error_state == "unreachable" and result.fetch_error == "blocked_private_address", url
    assert site.requests == []  # nothing was fetched, not even robots.txt
    # a hint with a non-passive path is reduced to the site root, which the host guard then blocks
    result = probe.probe(session, "http://169.254.169.254/latest/meta-data/")
    assert result.fetch_error == "blocked_private_address" and site.requests == []


def test_probe_starts_at_site_root_and_keeps_passive_hinted_page(session, settings, wp_site):
    probe = WebsiteProbeConnector(settings, client=wp_site.client(), tls_lookup=tls_ok)
    result = probe.probe(session, "https://coastalbakes.example/contact-us/?utm_source=maps")
    assert result.final_url == "https://coastalbakes.example/"
    visited = [u for u in wp_site.requests if not u.endswith("robots.txt")]
    assert visited[0] == "https://coastalbakes.example/"
    assert sum(1 for u in visited if u.startswith("https://coastalbakes.example/contact-us/")) == 1
    assert facts_by_field(result)["organization.registry.gstin"] == ["29AAACC1234F1ZP"]
    # a non-passive hinted path is simply ignored
    wp_site.requests.clear()
    result = probe.probe(session, "https://coastalbakes.example/shop/product/sourdough-123/")
    assert result.final_url == "https://coastalbakes.example/"
    assert not any("/shop/" in u for u in wp_site.requests)
