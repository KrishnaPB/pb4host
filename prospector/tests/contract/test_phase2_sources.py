"""Phase 2 sources: CrUX API, GitHub organisations, HTTP Archive feed, Companies House, LinkedIn connections."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from sqlalchemy import select

from prospector.connectors.base import ConnectorError
from prospector.connectors.companies_house import API as CH_API
from prospector.connectors.companies_house import CompaniesHouseConnector
from prospector.connectors.crux import CRUX_URL, CruxConnector, parse_record
from prospector.connectors.github_orgs import API as GH_API
from prospector.connectors.github_orgs import GithubOrgsConnector, count_wordpress_repos
from prospector.connectors.httparchive_feed import HttpArchiveFeedConnector, month_params
from prospector.connectors.linkedin_connections import (
    LinkedInConnectionsConnector,
    link_known_contacts,
    parse_connections_csv,
)
from prospector.jobs import tasks
from prospector.jobs.queue import enqueue, run_pending
from prospector.models import DiscoveryRun, ImportBatch, Job, Organization, PerformanceSnapshot, Person, Website

CRUX_RESPONSE = {
    "record": {
        "key": {"formFactor": "PHONE", "origin": "https://coastalbakes.example"},
        "metrics": {
            "largest_contentful_paint": {"histogram": [], "percentiles": {"p75": 4850}},
            "interaction_to_next_paint": {"percentiles": {"p75": 310}},
            "cumulative_layout_shift": {"percentiles": {"p75": "0.21"}},
            "experimental_time_to_first_byte": {"percentiles": {"p75": 1400}},
        },
        "collectionPeriod": {"firstDate": {"year": 2026, "month": 9, "day": 1}, "lastDate": {"year": 2026, "month": 9, "day": 28}},
    }
}


# --- CrUX ------------------------------------------------------------------------------------------
def test_parse_crux_record():
    res = parse_record(CRUX_RESPONSE, "mobile")
    assert res.available and res.lcp_p75 == 4850 and res.inp_p75 == 310 and res.cls_p75 == 0.21 and res.ttfb_p75 == 1400
    assert res.lcp_rating == "poor" and res.collection_period == "2026-09-01..2026-09-28"
    assert parse_record({"record": {"metrics": {}}}, "mobile").available is False


@respx.mock
def test_crux_task_stores_field_snapshot_and_scorer_merges_with_lab_data(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "crux_api_key", "crux-key")
    route = respx.post(CRUX_URL).mock(return_value=httpx.Response(200, json=CRUX_RESPONSE))
    org = Organization(display_name="Coastal Bakes", domain="coastalbakes.example", website_url="https://coastalbakes.example/", track="smb")
    site = Website(organization=org, url="https://coastalbakes.example/", final_url="https://coastalbakes.example/", host_key="coastalbakes.example", cms="wordpress")
    session.add_all([org, site])
    session.add(PerformanceSnapshot(website=site, strategy="mobile", lh_performance=38, lcp_ms=6100, source="psi", field_available=False))
    session.commit()
    tasks.overrides["crux"] = CruxConnector(settings, client=httpx.Client())
    try:
        tasks.crux_origin(session, {"organization_id": org.id, "website_id": site.id})
    finally:
        tasks.overrides.clear()
    sent = route.calls.last.request
    assert sent.url.params["key"] == "crux-key"
    assert json.loads(sent.content) == {"origin": "https://coastalbakes.example", "formFactor": "PHONE"}
    session.commit()
    snaps = session.scalars(select(PerformanceSnapshot).where(PerformanceSnapshot.website_id == site.id)).all()
    assert [s.source for s in snaps] == ["psi", "crux_api"]
    from prospector.core.scoring import build_context

    ctx = build_context(org, site, settings)
    assert ctx["perf"].mobile.lh_performance == 38  # lab from PSI
    assert ctx["perf"].mobile.field_lcp_p75 == 4850 and ctx["perf"].mobile.field_source == "crux_api"  # field from CrUX
    # scoring picks both "slow mobile" and "CrUX poor" rules
    from prospector.core.scoring import score_organization

    result = score_organization(session, org)
    ids = {b["id"] for b in result.breakdown}
    assert {"slow_mobile_severe", "field_poor"} <= ids, ids


@respx.mock
def test_crux_404_means_no_data_not_an_error(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "psi_api_key", "psi-key")
    respx.post(CRUX_URL).mock(return_value=httpx.Response(404, json={"error": {"code": 404, "message": "chrome ux report data not found"}}))
    res = CruxConnector(settings, client=httpx.Client()).query(session, "https://tiny.example")
    assert res.available is False and res.lcp_p75 is None


# --- GitHub ----------------------------------------------------------------------------------------
GH_SEARCH = {"total_count": 2, "incomplete_results": False, "items": [{"login": "pixelgrove-dev", "type": "Organization"},
                                                                       {"login": "nosite-labs", "type": "Organization"}]}
GH_ORG = {"login": "pixelgrove-dev", "name": "Pixelgrove", "blog": "pixelgrove.example", "location": "Bengaluru, India",
          "html_url": "https://github.com/pixelgrove-dev", "public_repos": 12, "email": "team@pixelgrove.example"}
GH_REPOS = [{"name": "wp-client-theme", "description": "Custom WordPress theme", "topics": ["wordpress", "theme"]},
            {"name": "api-gateway", "description": "Go service", "topics": []},
            {"name": "woo-checkout-fields", "description": None, "topics": ["woocommerce"]}]


def test_count_wordpress_repos():
    assert count_wordpress_repos(GH_REPOS) == 2


@respx.mock
def test_github_run_creates_agency_orgs_with_provenance_and_no_person_data(session, settings):
    respx.get(f"{GH_API}/search/users").mock(return_value=httpx.Response(200, json=GH_SEARCH))
    respx.get(f"{GH_API}/orgs/pixelgrove-dev").mock(return_value=httpx.Response(200, json=GH_ORG))
    respx.get(f"{GH_API}/orgs/pixelgrove-dev/repos").mock(return_value=httpx.Response(200, json=GH_REPOS))
    respx.get(f"{GH_API}/orgs/nosite-labs").mock(return_value=httpx.Response(200, json={**GH_ORG, "login": "nosite-labs", "blog": "", "html_url": "https://github.com/nosite-labs"}))
    run = DiscoveryRun(track="agency", provider="github_orgs", query="", location="Bengaluru", max_pages=1)
    session.add(run)
    session.commit()
    tasks.overrides["github_orgs"] = GithubOrgsConnector(settings, client=httpx.Client())
    try:
        tasks.github_org_search(session, {"run_id": run.id, "page_no": 1})
    finally:
        tasks.overrides.clear()
    session.commit()
    run = session.get(DiscoveryRun, run.id)
    assert run.status == "done" and run.candidates_found == 2 and run.candidates_without_website == 1 and run.organizations_created == 1
    assert run.calls_made == 4  # search + 2 profiles + 1 repo list
    org = session.scalar(select(Organization))
    assert org.domain == "pixelgrove.example" and org.track == "agency" and org.display_name == "Pixelgrove" and org.city == "Bengaluru"
    assert org.social_links == {"github": "https://github.com/pixelgrove-dev"} and org.lead_source == "github"
    assert {"github_org", "github_wordpress_repos"} <= set(org.signals)
    assert all(e.source == "github_api" and e.source_url == "https://github.com/pixelgrove-dev" for e in org.evidence)
    assert org.emails == [] and not org.persons  # the org e-mail from GitHub is never stored
    assert session.scalar(select(Job).where(Job.type == "website_probe")) is not None


@respx.mock
def test_github_rate_limit_fails_the_run_cleanly(session, settings):
    respx.get(f"{GH_API}/search/users").mock(return_value=httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "rate limit"}))
    run = DiscoveryRun(track="agency", provider="github_orgs", query="", location="Mangaluru", max_pages=1)
    session.add(run)
    session.commit()
    tasks.overrides["github_orgs"] = GithubOrgsConnector(settings, client=httpx.Client())
    try:
        with pytest.raises(ConnectorError, match="rate limit"):
            tasks.github_org_search(session, {"run_id": run.id, "page_no": 1})
    finally:
        tasks.overrides.clear()
    assert session.get(DiscoveryRun, run.id).status == "failed"


# --- HTTP Archive feed -------------------------------------------------------------------------------
class FakeBigQuery:
    def __init__(self, rows, estimate):
        self.rows, self.estimate, self.queries = rows, estimate, []

    def dry_run_bytes(self, sql, params):
        self.queries.append(("dry", params))
        return self.estimate

    def run(self, sql, params):
        self.queries.append(("run", params))
        return iter(self.rows)


FEED_ROWS = [{"origin": "https://slowshop.example", "lcp_p75": 5200, "inp_p75": 400, "cls_p75": 0.3, "rank": 100000},
             {"origin": "https://coastalbakes.example", "lcp_p75": 4300, "inp_p75": None, "cls_p75": 0.05, "rank": 1000000},
             {"origin": "not-a-url", "lcp_p75": 1}]


def test_month_params():
    assert month_params("2026-09") == (__import__("datetime").date(2026, 9, 1), 202609)
    with pytest.raises(ConnectorError):
        month_params("September")


def test_httparchive_feed_fails_closed_above_budget_and_runs_below(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "httparchive_max_bytes", 10 * 1024**3)
    expensive = FakeBigQuery(FEED_ROWS, estimate=250 * 1024**3)
    feed = HttpArchiveFeedConnector(settings, backend=expensive)
    with pytest.raises(ConnectorError, match="budget"):
        feed.fetch(session, month="2026-09", country="in")
    assert [q[0] for q in expensive.queries] == ["dry"]  # the real query never ran

    cheap = FakeBigQuery(FEED_ROWS, estimate=6 * 1024**3)
    result = HttpArchiveFeedConnector(settings, backend=cheap).fetch(session, month="2026-09", country="in", limit=50)
    assert [q[0] for q in cheap.queries] == ["dry", "run"]
    assert cheap.queries[1][1] == {"month": __import__("datetime").date(2026, 9, 1), "yyyymm": 202609, "country": "in",
                                   "lcp_threshold_ms": 4000, "limit": 50}
    assert [r.origin for r in result.rows] == ["https://slowshop.example", "https://coastalbakes.example"]
    assert result.bytes_estimated == 6 * 1024**3 and 0 < result.cost_estimate_usd < 0.05
    assert "chrome-ux-report.materialized.country_summary" in feed.sql() and "httparchive.crawl.pages" in feed.sql()


def test_httparchive_feed_task_creates_orgs_with_field_snapshots(session, settings):
    run = DiscoveryRun(track="smb", provider="httparchive_feed", query="in 2026-09", max_pages=1)
    session.add(run)
    session.commit()
    tasks.overrides["httparchive_feed"] = HttpArchiveFeedConnector(settings, backend=FakeBigQuery(FEED_ROWS, estimate=1024))
    try:
        tasks.httparchive_feed_run(session, {"run_id": run.id, "month": "2026-09", "country": "in", "limit": 10})
    finally:
        tasks.overrides.clear()
    session.commit()
    run = session.get(DiscoveryRun, run.id)
    assert run.status == "done" and run.organizations_created == 2 and run.candidates_found == 2
    org = session.scalar(select(Organization).where(Organization.domain == "slowshop.example"))
    assert org.lead_source == "httparchive" and {"httparchive_wordpress", "crux_poor_lcp"} <= set(org.signals)
    snap = org.websites[0].latest_field_performance("mobile")
    assert snap.source == "crux_bq" and snap.field_lcp_p75 == 5200 and snap.raw_json["rank"] == 100000
    assert len(session.scalars(select(Job).where(Job.type == "website_probe")).all()) == 2


# --- Companies House -----------------------------------------------------------------------------------
CH_SEARCH = {"items": [{"company_number": "12345678", "title": "PIXELGROVE DIGITAL LTD", "company_status": "active",
                        "address_snippet": "1 High Street, Reading, RG1 1AA", "date_of_creation": "2018-04-03"}]}
CH_PROFILE = {"company_number": "12345678", "company_name": "PIXELGROVE DIGITAL LTD", "company_status": "active", "type": "ltd",
              "date_of_creation": "2018-04-03", "sic_codes": ["62012", "73110"],
              "registered_office_address": {"address_line_1": "1 High Street", "locality": "Reading", "postal_code": "RG1 1AA", "country": "England"}}


@respx.mock
def test_companies_house_search_profile_and_live_officers(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "companies_house_api_key", "ch-key")
    search = respx.get(f"{CH_API}/search/companies").mock(return_value=httpx.Response(200, json=CH_SEARCH))
    respx.get(f"{CH_API}/company/12345678").mock(return_value=httpx.Response(200, json=CH_PROFILE))
    respx.get(f"{CH_API}/company/12345678/officers").mock(return_value=httpx.Response(200, json={
        "items": [{"name": "SMITH, Jane", "officer_role": "director", "appointed_on": "2018-04-03"}]}))
    ch = CompaniesHouseConnector(settings, client=httpx.Client())
    hits = ch.search(session, "Pixelgrove")
    assert search.calls.last.request.headers["authorization"].startswith("Basic ")
    assert hits[0].company_number == "12345678" and hits[0].url.endswith("/company/12345678")
    profile = ch.profile(session, "12345678")
    assert profile.name == "PIXELGROVE DIGITAL LTD" and profile.address == "1 High Street, Reading, RG1 1AA, England"
    assert profile.sic_codes == ["62012", "73110"] and profile.locality == "Reading"
    officers = ch.officers_live(session, "12345678")
    assert officers[0]["name"] == "SMITH, Jane"
    assert ch.creates_persons is False  # officers are displayed, never stored


# --- LinkedIn connections import ----------------------------------------------------------------------
CONNECTIONS_CSV = b"""Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing. You will only see email addresses for connections who have allowed their connections to see or download their email address using this setting https://www.linkedin.com/psettings/privacy/email. You can learn more here https://www.linkedin.com/help/linkedin/answer/261"

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Priya,Rao,https://www.linkedin.com/in/priya-rao-1,,Coastal Bakes,Founder,12 Mar 2024
Dev,Patel,https://www.linkedin.com/in/dev-patel-9,dev@pixelgrove.example,Pixelgrove,Technical Director,03 Jan 2023
Sam,Lee,https://www.linkedin.com/in/sam-lee-77,,Unknown Ventures Pvt Ltd,CEO,20 Aug 2022
"""


def test_parse_connections_csv_skips_preamble():
    rows = parse_connections_csv(CONNECTIONS_CSV)
    assert [r.full_name for r in rows] == ["Priya Rao", "Dev Patel", "Sam Lee"]
    assert rows[1].email == "dev@pixelgrove.example" and rows[0].connected_on == "12 Mar 2024"
    with pytest.raises(ValueError):
        parse_connections_csv(b"name,company\nA,B\n")


def test_connections_import_links_known_companies_and_relinks_later(session, settings, owner):
    session.add(Organization(display_name="Coastal Bakes", domain="coastalbakes.example", track="smb"))
    session.commit()
    batch = ImportBatch(kind="linkedin_connections", filename="Connections.csv", imported_by=owner.id)
    session.add(batch)
    session.flush()
    summary = LinkedInConnectionsConnector(settings).import_rows(session, batch, parse_connections_csv(CONNECTIONS_CSV), actor=owner)
    session.commit()
    assert (summary.rows, summary.created, summary.linked, summary.unlinked) == (3, 3, 1, 2)
    priya = session.scalar(select(Person).where(Person.full_name == "Priya Rao"))
    assert priya.organization.domain == "coastalbakes.example" and priya.source == "linkedin_connections"
    assert priya.lawful_basis == "legitimate_interest" and priya.lia_reference == settings.linkedin_connections_lia_reference
    assert priya.notice_status == "pending" and priya.meta["connected_on"] == "2024-03-12" and priya.is_known_connection
    dev = session.scalar(select(Person).where(Person.full_name == "Dev Patel"))
    assert dev.organization_id is None and dev.meta["company"] == "Pixelgrove"

    # re-import updates instead of duplicating
    summary2 = LinkedInConnectionsConnector(settings).import_rows(session, batch, parse_connections_csv(CONNECTIONS_CSV), actor=owner)
    assert summary2.created == 0 and summary2.updated == 3 and len(session.scalars(select(Person)).all()) == 3

    # a new organisation with a matching name picks up the unlinked contact
    pix = Organization(display_name="Pixelgrove", domain="pixelgrove.example", track="agency")
    session.add(pix)
    session.flush()
    assert link_known_contacts(session, pix) == 1
    session.commit()
    assert session.scalar(select(Person).where(Person.full_name == "Dev Patel")).organization_id == pix.id


# --- scheduled re-checks -----------------------------------------------------------------------------------
def test_schedule_rechecks_enqueues_light_probes_for_due_tracked_orgs(session, settings):
    from datetime import timedelta

    from prospector.util import utcnow

    due = Organization(display_name="Tracked", domain="tracked.example", website_url="https://tracked.example/", status="customer",
                       tracked=True, next_recheck_at=utcnow() - timedelta(hours=1))
    session.add(Website(organization=due, url="https://tracked.example/", host_key="tracked.example"))
    later = Organization(display_name="Later", domain="later.example", status="qualified", tracked=True, next_recheck_at=utcnow() + timedelta(days=3))
    session.add(Website(organization=later, url="https://later.example/", host_key="later.example"))
    session.add_all([due, later])
    session.commit()
    tasks.schedule_rechecks(session, {})
    session.commit()
    jobs = session.scalars(select(Job).where(Job.type == "website_probe")).all()
    assert len(jobs) == 1 and jobs[0].payload["organization_id"] == due.id
    assert jobs[0].payload["light"] is True and jobs[0].payload["homepage_only"] is True
    assert due.next_recheck_at > utcnow() + timedelta(days=settings.recheck_interval_days - 1)
    # a queued job can be drained by the inline runner without the probe actually running (handler override)
    enqueue(session, "noop", {}, idempotency_key="noop-1")
    session.commit()
    assert run_pending(lambda: session, types=["noop"]) == 1
