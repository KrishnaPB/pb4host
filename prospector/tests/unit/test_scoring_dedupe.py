
from prospector.core.dedupe import find_existing, merge_organizations
from prospector.core.facts import Fact, apply_facts
from prospector.core.organizations import ensure_website
from prospector.core.scoring import load_rules, score_organization
from prospector.models import Organization, PerformanceSnapshot
from prospector.util import utcnow


def make_org(session, domain, name=None, city=None, track="smb"):
    org = Organization(domain=domain, website_url=f"https://{domain}/", track=track, status="new", country="IN", city=city)
    session.add(org)
    session.flush()
    site = ensure_website(session, org, f"https://{domain}/")
    if name:
        apply_facts(session, org, [Fact("organization.display_name", name, "own_website", f"https://{domain}/", 0.9)], site)
    return org, site


def test_scoring_wordpress_broken_site_is_hot(session):
    org, site = make_org(session, "broken.example", "Broken Co")
    now = utcnow()
    apply_facts(session, org, [
        Fact("website.cms", "wordpress", "own_website", org.website_url, 0.95, now),
        Fact("website.cms_version", "5.9.3", "own_website", org.website_url, 0.9, now),
        Fact("website.is_woocommerce", "true", "own_website", org.website_url, 0.9, now),
        Fact("website.error_state", "db_error", "own_website", org.website_url, 1.0, now),
        Fact("website.host_class", "shared", "dns", "dns://broken.example", 0.7, now),
        Fact("website.php_version", "7.4.33", "own_website", org.website_url, 0.9, now),
    ], site)
    site.error_confirmed_at = now
    session.add(PerformanceSnapshot(website=site, strategy="mobile", lh_performance=28, field_lcp_p75=5200, field_available=True, source="psi"))
    session.flush()
    result = score_organization(session, org)
    ids = {r["id"] for r in result.breakdown}
    assert {"wp_detected", "woocommerce", "broken_confirmed", "slow_mobile_severe", "field_poor", "wp_outdated", "php_eol", "shared_hosting"} <= ids
    assert result.total == 100  # capped
    assert result.band == "hot"
    assert org.score_total == 100 and org.score_band == "hot"


def test_unconfirmed_error_does_not_score(session):
    org, site = make_org(session, "flaky.example")
    apply_facts(session, org, [Fact("website.cms", "wordpress", "own_website", org.website_url, 0.95),
                               Fact("website.error_state", "server_error", "own_website", org.website_url, 1.0)], site)
    result = score_organization(session, org)
    assert "broken_confirmed" not in {r["id"] for r in result.breakdown}
    assert result.total == 30


def test_agency_track_uses_agency_rules_and_managed_host_penalty(session):
    org, site = make_org(session, "agency.example", "Pixelgrove", track="agency")
    apply_facts(session, org, [Fact("website.cms", "wordpress", "own_website", org.website_url, 0.95),
                               Fact("website.managed_host", "wpengine", "own_website", org.website_url, 0.9)], site)
    org.signals = ["agency_keywords", "wordpress"]
    result = score_organization(session, org)
    ids = {r["id"]: r["points"] for r in result.breakdown}
    assert ids["wp_detected"] == 15
    assert ids["agency_signals"] == 25
    assert ids["managed_wp_host"] == -5
    assert result.track_used == "agency"


def test_rules_file_loads_with_checksum():
    rules = load_rules()
    assert rules.checksum and rules.version_label == "1"
    assert rules.thresholds["hot"] == 70


def test_dedupe_domain_place_phone_and_fuzzy_name(session):
    org, site = make_org(session, "coastalbakes.example", "Coastal Bakes", city="Mangaluru")
    apply_facts(session, org, [Fact("organization.phone", "+918242456789", "own_website", org.website_url, 0.9)], site)
    session.flush()
    assert find_existing(session, domain="coastalbakes.example") is org
    assert find_existing(session, phones=["+918242456789"]) is org
    assert find_existing(session, name="Coastal Bakes Pvt Ltd", city="Mangaluru") is org
    assert find_existing(session, name="Coastal Bakes", city="Udupi") is None
    assert find_existing(session, name="Totally Different", city="Mangaluru") is None


def test_merge_keeps_evidence_and_marks_duplicate(session):
    a, _sa = make_org(session, "a.example", "Alpha Studio", city="Udupi")
    b, sb = make_org(session, "b.example", "Alpha Studio Pvt Ltd", city="Udupi")
    apply_facts(session, b, [Fact("organization.phone", "+918202520000", "own_website", b.website_url, 0.9)], sb)
    merged = merge_organizations(session, a, b)
    session.flush()
    assert merged is a
    assert b.status == "merged" and b.merged_into_id == a.id
    assert "+918202520000" in a.phones
    assert len(a.websites) == 2
    assert any(e.field == "organization.phone" for e in a.evidence)
