"""End-to-end: Places run → probe → DNS → PageSpeed → score, all through the job queue with mocked HTTP."""

import httpx
import respx
from sqlalchemy import select

from prospector.api.routers.runs import create_run
from prospector.connectors.dns_asn import DnsAsnConnector
from prospector.connectors.google_places import TEXT_SEARCH_URL, GooglePlacesConnector
from prospector.connectors.pagespeed import PSI_URL, PageSpeedConnector
from prospector.connectors.website_probe.health import TlsInfo
from prospector.connectors.website_probe.probe import WebsiteProbeConnector
from prospector.db import session_factory
from prospector.jobs import tasks
from prospector.jobs.queue import run_pending
from prospector.models import (
    DiscoveryRun,
    Job,
    Organization,
    Person,
    PlaceLink,
    RawSnapshot,
)
from tests.conftest import FixtureSite, html_fixture, json_fixture


class FakeDns:
    def query(self, name, rdtype):
        table = {
            ("coastalbakes.example", "A"): ["203.0.113.10"],
            ("www.pixelgrove.example", "A"): ["203.0.113.20"],
            ("closedcafe.example", "A"): ["203.0.113.30"],
            ("10.113.0.203.origin.asn.cymru.com", "TXT"): ["47583 | 203.0.113.0/24 | IN | apnic | 2019-01-01"],
            ("20.113.0.203.origin.asn.cymru.com", "TXT"): ["14061 | 203.0.113.0/24 | US | arin | 2012-09-25"],
            ("AS47583.asn.cymru.com", "TXT"): ["47583 | IN | apnic | 2019 | AS-HOSTINGER, Hostinger International Limited, CY"],
            ("AS14061.asn.cymru.com", "TXT"): ["14061 | US | arin | 2012 | DIGITALOCEAN-ASN, US"],
            ("coastalbakes.example", "MX"): ["aspmx.l.google.com"],
            ("pixelgrove.example", "MX"): ["mx.zoho.in"],
        }
        return table.get((name, rdtype), [])


def multi_site_handler(sites: dict[str, FixtureSite]):
    def handler(request: httpx.Request) -> httpx.Response:
        site = sites.get(request.url.host)
        if site is None:
            raise httpx.ConnectError("dns: name or service not known", request=request)
        return site.handler(request)

    return handler


@respx.mock
def test_full_pipeline(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "google_maps_api_key", "test-key")
    respx.post(TEXT_SEARCH_URL).mock(return_value=httpx.Response(200, json={**json_fixture("places_search.json"), "nextPageToken": None}))
    respx.get(PSI_URL).mock(return_value=httpx.Response(200, json=json_fixture("psi_mobile.json")))
    sites = {
        "coastalbakes.example": FixtureSite({
            "/": (200, html_fixture("wp_home.html"), {"server": "LiteSpeed", "x-powered-by": "PHP/7.4.33"}),
            "/contact-us/": (200, html_fixture("wp_contact.html"), None),
        }),
        "www.pixelgrove.example": FixtureSite({"/": (200, html_fixture("agency_home.html"), None)}),
        "closedcafe.example": FixtureSite({"/": (500, html_fixture("db_error.html"), None)}),
    }
    web = httpx.Client(transport=httpx.MockTransport(multi_site_handler(sites)), follow_redirects=True)
    tasks.overrides.clear()
    tasks.overrides["google_places"] = GooglePlacesConnector(settings, client=httpx.Client())
    tasks.overrides["website_probe"] = WebsiteProbeConnector(settings, client=web, tls_lookup=lambda url: TlsInfo(True, None, None))
    tasks.overrides["dns_asn"] = DnsAsnConnector(settings, client=FakeDns())
    tasks.overrides["pagespeed"] = PageSpeedConnector(settings, client=httpx.Client())
    try:
        run = create_run(session, query="bakery", location="Mangaluru", track="smb", max_pages=2, region_code="IN", user=None)
        session.commit()
        executed = run_pending(session_factory())
        assert executed >= 8

        session.expire_all()
        run = session.get(DiscoveryRun, run.id)
        assert run.status == "done" and run.candidates_found == 4 and run.candidates_without_website == 1
        assert run.organizations_created == 3 and run.pages_fetched == 1

        orgs = {o.domain: o for o in session.scalars(select(Organization)).all()}
        bakery = orgs["coastalbakes.example"]
        assert bakery.display_name == "Coastal Bakes"
        assert "+918242456789" in bakery.phones
        assert bakery.registry_ids.get("gstin") == "29AAACC1234F1ZP"
        assert bakery.city == "Mangaluru"
        site = bakery.primary_website
        assert site.cms == "wordpress" and site.cms_version == "6.4.3" and site.is_woocommerce
        assert site.host_provider and "Hostinger" in site.host_provider and site.host_class == "shared"
        assert site.mail_provider == "google_workspace"
        perf = site.latest_performance("mobile")
        assert perf is not None and perf.lh_performance == 31 and perf.field_lcp_p75 == 4850
        assert bakery.score_total >= 70 and bakery.score_band == "hot"
        rule_ids = {r["id"] for r in bakery.score_breakdown["rules"]}
        assert {"wp_detected", "woocommerce", "slow_mobile_severe", "field_poor", "heavy_builder_no_cache", "php_eol", "shared_hosting"} <= rule_ids

        # Only place_id + coords were stored from Google; name/phone came from the website with evidence
        link = session.scalar(select(PlaceLink).where(PlaceLink.organization_id == bakery.id))
        assert link.place_id == "ChIJdemo0000000000000001" and link.lat == 12.8746 and link.coords_expires_at is not None
        sources = {e.source for e in bakery.evidence}
        assert sources <= {"own_website", "dns", "derived"}
        assert all(e.source_url for e in bakery.evidence)

        agency = orgs["pixelgrove.example"]
        assert agency.track == "agency"  # Places type hint + agency keywords
        assert "agency_keywords" in agency.signals
        assert agency.score_total >= 40

        cafe = orgs["closedcafe.example"]
        cafe_site = cafe.primary_website
        assert cafe_site.error_state == "db_error" and cafe_site.error_confirmed_at is None
        rechecks = [j for j in session.scalars(select(Job).where(Job.type == "confirm_error_state")) if j.payload["website_id"] == cafe_site.id]
        assert len(rechecks) == 2 and all(j.status == "queued" for j in rechecks)
        # unconfirmed error does not score yet
        assert "broken_confirmed" not in {r["id"] for r in cafe.score_breakdown["rules"]}

        # R2: nothing in the pipeline created a person
        assert session.scalar(select(Person)) is None
        # raw snapshots were written under data_dir with an expiry
        snaps = session.scalars(select(RawSnapshot)).all()
        assert snaps and all(s.expires_at is not None and s.path.startswith(str(settings.data_dir)) for s in snaps)
        # transient Places website hints were scrubbed from finished job payloads
        for job in session.scalars(select(Job).where(Job.type == "website_probe", Job.status == "done")):
            assert "url_hint" not in job.payload and job.payload.get("scrubbed") is True

        # Re-check confirms the error → scores
        for j in rechecks:
            j.run_after = run.created_at
        session.commit()
        run_pending(session_factory())
        session.expire_all()
        cafe = session.get(Organization, cafe.id)
        assert cafe.primary_website.error_confirmed_at is not None
        assert "broken_confirmed" in {r["id"] for r in cafe.score_breakdown["rules"]}
    finally:
        tasks.overrides.clear()
