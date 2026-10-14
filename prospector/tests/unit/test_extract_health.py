from prospector.connectors.website_probe.extract import extract
from prospector.connectors.website_probe.health import classify
from tests.conftest import html_fixture


def test_contact_page_extraction():
    facts = extract(html_fixture("wp_contact.html"), "https://coastalbakes.example/contact-us/", "IN")
    assert "+918242456789" in facts.phones
    assert "orders@coastalbakes.example" in facts.emails
    assert facts.gstin == "29AAACC1234F1ZP"
    assert facts.cin == "U15400KA2015PTC081234"
    assert facts.social.get("linkedin") == "https://www.linkedin.com/company/coastal-bakes"
    assert facts.address["addressLocality"] == "Mangaluru"
    assert facts.address["postalCode"] == "575001"
    assert facts.site_name == "Coastal Bakes" and facts.site_name_source == "jsonld"
    assert "Lighthouse Hill" in facts.address["streetAddress"]


def test_agency_keywords_detected():
    facts = extract(html_fixture("agency_home.html"), "https://www.pixelgrove.example/", "IN")
    assert facts.agency_hits
    assert facts.has_portfolio_link


def test_error_classification():
    assert classify(200, html_fixture("db_error.html"), None) == "db_error"
    assert classify(503, "<html>Briefly unavailable for scheduled maintenance. Check back in a minute.</html>", None) == "maintenance"
    assert classify(500, "<html>Internal Server Error</html>", None) == "server_error"
    assert classify(200, "<html>There has been a critical error on this website.</html>", None) == "php_error"
    assert classify(200, html_fixture("parked.html"), None) == "parked"
    assert classify(None, "", "dns: name or service not known") == "dns_fail"
    assert classify(None, "", "tls: certificate verify failed") == "tls_invalid"
    assert classify(None, "", "timeout") == "timeout"
    assert classify(200, html_fixture("wp_home.html"), None) == "none"
