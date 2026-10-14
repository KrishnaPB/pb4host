from prospector.connectors.website_probe.fingerprint import fingerprint
from tests.conftest import html_fixture


def test_wordpress_woocommerce_elementor_detected():
    fp = fingerprint(html_fixture("wp_home.html"), {"server": "LiteSpeed", "x-powered-by": "PHP/7.4.33"})
    assert fp.cms == "wordpress"
    assert fp.cms_version == "6.4.3"
    assert fp.is_woocommerce is True
    assert fp.page_builder == "elementor"
    assert fp.theme == "astra"
    assert fp.php_version == "7.4.33"
    assert fp.server_header and "litespeed" in fp.server_header.lower()
    assert fp.cms_confidence >= 0.9


def test_wpbakery_and_cache_detected_for_agency_page():
    fp = fingerprint(html_fixture("agency_home.html"), {})
    assert fp.cms == "wordpress"
    assert fp.page_builder == "wpbakery"
    assert fp.caching_layer == "wp_rocket"


def test_shopify_detected_and_wordpress_absent():
    fp = fingerprint(html_fixture("shopify_home.html"), {})
    assert fp.cms == "shopify"
    assert fp.is_woocommerce is False


def test_plain_html_has_no_cms():
    fp = fingerprint("<html><body><h1>hello</h1></body></html>", {"server": "nginx"})
    assert fp.cms is None
