"""Phase 2 UI: rights workflow (one-click erasure), notice tracking, people, runs providers, imports, org-detail panels."""

from __future__ import annotations

import re

import httpx
import pytest
from sqlalchemy import select

from prospector.compliance.suppression import is_suppressed
from prospector.core.facts import Fact, apply_facts
from prospector.core.organizations import ensure_website
from prospector.jobs import tasks
from prospector.models import (
    ActivityLog,
    ConsentRecord,
    CrmLink,
    DiscoveryRun,
    Evidence,
    InboundSubmission,
    Job,
    NoticeEvent,
    Organization,
    Person,
    RightsRequest,
    Suppression,
)
from tests.contract.test_phase2_sources import CONNECTIONS_CSV


def seed_org(session, domain="seed.example", name="Seed Co", status="qualified", score=80, **kw):
    fields = dict(domain=domain, website_url=f"https://{domain}/", track="smb", status=status, country="IN", city="Udupi")
    fields.update(kw)
    org = Organization(**fields)
    session.add(org)
    session.flush()
    site = ensure_website(session, org, f"https://{domain}/")
    apply_facts(session, org, [
        Fact("organization.display_name", name, "own_website", f"https://{domain}/", 0.9),
        Fact("organization.email", f"info@{domain}", "own_website", f"https://{domain}/contact/", 0.9),
        Fact("organization.phone", "+91 824 2456789", "own_website", f"https://{domain}/contact/", 0.9),
        Fact("website.cms", "wordpress", "own_website", f"https://{domain}/", 0.95),
    ], site)
    org.score_total, org.score_band = score, "hot"
    session.commit()
    return org


def add_person(session, org, name="Priya Rao", email=None, **kw):
    defaults = dict(source="inbound_form", lawful_basis="consent", notice_status="sent")
    defaults.update(kw)
    person = Person(organization=org, full_name=name, email=email or f"{name.split()[0].lower()}@{org.domain}", **defaults)
    session.add(person)
    session.commit()
    return person


# --- rights workflow ----------------------------------------------------------------------------------
def test_rights_request_open_update_and_dashboard_counts(logged_in, session):
    org = seed_org(session)
    r = logged_in.post("/rights/requests", data={"kind": "access", "subject": "info@seed.example", "channel": "email", "details": "what do you hold"},
                       follow_redirects=True)
    assert r.status_code == 200 and "Matched 1 organisation" in r.text and "Seed Co" in r.text
    req = session.scalar(select(RightsRequest))
    assert req.kind == "access" and req.organization_id == org.id and req.status == "open" and req.due_at > req.received_at
    r = logged_in.get("/")
    assert "Rights requests" in r.text and "<strong>1</strong> open" in r.text
    r = logged_in.post(f"/rights/requests/{req.id}", data={"status": "completed", "resolution": ""}, follow_redirects=True)
    assert "Record what was done" in r.text  # cannot close without a note
    r = logged_in.post(f"/rights/requests/{req.id}", data={"status": "completed", "resolution": "export sent by e-mail"}, follow_redirects=True)
    assert r.status_code == 200
    session.rollback()
    req = session.get(RightsRequest, req.id)
    assert req.status == "completed" and req.completed_at and "export sent" in req.resolution
    r = logged_in.get("/rights/requests?status=completed")
    assert "info@seed.example" in r.text
    r = logged_in.post("/rights/requests", data={"kind": "bogus", "subject": "x"})
    assert r.status_code == 400


def test_one_click_erasure_removes_person_and_anonymises_history(logged_in, session, settings, monkeypatch):
    """Plan §11 acceptance: erasure removes the person and anonymises organisation history in one workflow."""
    org = seed_org(session)
    consent = ConsentRecord(purpose="speed_evaluation_contact", text_version="v1", captured_via="webhook")
    session.add(consent)
    session.flush()
    priya = add_person(session, org, consent_id=consent.id)
    consent.person_id = priya.id
    session.add(InboundSubmission(form_key="eval", idempotency_key="i1", captured_via="webhook", organization_id=org.id, person_id=priya.id,
                                  consent_id=consent.id, payload_hash="h", ip_hash="iph", message="please call me"))
    session.add(CrmLink(target="espocrm", entity_type="organization", entity_id=org.id, organization_id=org.id, remote_id="a1"))
    session.add(CrmLink(target="espocrm", entity_type="person", entity_id=priya.id, organization_id=org.id, remote_id="c1"))
    org.tracked, org.notes = True, "met at the expo"
    session.commit()
    org_id, person_id = org.id, priya.id

    r = logged_in.post("/rights/requests", data={"kind": "erasure", "subject": "priya@seed.example", "channel": "email"}, follow_redirects=True)
    req_id = int(re.search(r"/rights/requests/(\d+)", str(r.url)).group(1))
    r = logged_in.post(f"/rights/requests/{req_id}/fulfil", data={"action": "erase", "include_org": "1"}, follow_redirects=True)
    assert r.status_code == 200 and "1 contact(s) erased" in r.text and "1 organisation(s) anonymised" in r.text

    session.rollback()
    session.expire_all()
    assert session.get(Person, person_id) is None
    org = session.get(Organization, org_id)
    assert org.status == "anonymised" and org.domain is None and org.emails == [] and org.notes is None and org.tracked is False
    assert session.scalars(select(Evidence).where(Evidence.organization_id == org_id)).all() == []
    sub = session.scalar(select(InboundSubmission))
    assert sub.message is None and sub.ip_hash is None and sub.status == "erased" and sub.person_id is None
    assert session.get(ConsentRecord, consent.id).withdrawn_at is not None
    assert session.scalars(select(CrmLink)).all() == []
    deletes = session.scalars(select(Job).where(Job.type == "crm_delete")).all()
    assert {(j.payload["remote_entity"], j.payload["remote_id"]) for j in deletes} == {("Account", "a1"), ("Contact", "c1")}
    assert is_suppressed(session, "email", "priya@seed.example") and is_suppressed(session, "domain", "seed.example")
    assert is_suppressed(session, "email", "info@seed.example") and is_suppressed(session, "phone", "+918242456789")
    req = session.get(RightsRequest, req_id)
    assert req.status == "completed" and req.subject.startswith("sha256:") and "erasure" in req.resolution
    actions = {a.action for a in session.scalars(select(ActivityLog))}
    assert {"rights.request_open", "rights.erasure", "organization.anonymise", "person.erase", "suppression.add"} <= actions
    # a second attempt is refused, and the hashed subject no longer leaks the e-mail anywhere on the page
    r = logged_in.post(f"/rights/requests/{req_id}/fulfil", data={"action": "erase"}, follow_redirects=True)
    assert "already closed" in r.text and "priya@seed.example" not in r.text

    # the CRM delete job calls the adapter
    calls = []

    class Adapter:
        target = "espocrm"

        def delete_record(self, entity, remote_id):
            calls.append((entity, remote_id))
            return True

    monkeypatch.setattr(settings, "espocrm_base_url", "https://crm.example")
    monkeypatch.setattr(settings, "espocrm_api_key", "k")
    tasks.overrides["crm_adapter"] = Adapter()
    try:
        for job in deletes:
            tasks.crm_delete(session, job.payload)
    finally:
        tasks.overrides.clear()
    assert sorted(calls) == [("Account", "a1"), ("Contact", "c1")]


def test_person_only_erasure_keeps_business_facts_but_scrubs_notes(logged_in, session):
    org = seed_org(session)
    priya = add_person(session, org)
    org.notes = "Spoke to Priya Rao (priya@seed.example) on Monday"
    session.commit()
    org_id, priya_id = org.id, priya.id
    r = logged_in.post("/rights/requests", data={"kind": "erasure", "subject": "priya@seed.example"}, follow_redirects=True)
    req_id = int(re.search(r"/rights/requests/(\d+)", str(r.url)).group(1))
    r = logged_in.post(f"/rights/requests/{req_id}/fulfil", data={"action": "erase", "include_org": "0"}, follow_redirects=True)
    assert "1 contact(s) erased" in r.text and "anonymised" not in r.text
    session.rollback()
    session.expire_all()
    assert session.get(Person, priya_id) is None
    org = session.get(Organization, org_id)
    assert org.status == "qualified" and org.domain == "seed.example" and org.notes == "Spoke to [erased] ([erased]) on Monday"
    assert is_suppressed(session, "email", "priya@seed.example") and not is_suppressed(session, "domain", "seed.example")


def test_stop_contact_objection_keeps_records_but_blocks_everything(logged_in, session):
    org = seed_org(session)
    priya = add_person(session, org)
    r = logged_in.post("/rights/requests", data={"kind": "objection", "subject": "seed.example"}, follow_redirects=True)
    req_id = int(re.search(r"/rights/requests/(\d+)", str(r.url)).group(1))
    r = logged_in.post(f"/rights/requests/{req_id}/fulfil", data={"action": "stop_contact"}, follow_redirects=True)
    assert "1 organisation(s) suppressed" in r.text and "1 contact(s) marked do-not-contact" in r.text
    session.rollback()
    session.expire_all()
    assert session.get(Organization, org.id).status == "suppressed"
    p = session.get(Person, priya.id)
    assert p is not None and p.do_not_contact and p.notice_status == "objected"
    assert is_suppressed(session, "domain", "seed.example")
    # exports now skip the person and the organisation
    r = logged_in.get("/exports/persons.csv")
    assert "Priya" not in r.text
    r = logged_in.get("/exports/organizations.csv")
    assert "seed.example" not in r.text


def test_consent_withdrawal_erases_consent_only_persons(logged_in, session):
    org = seed_org(session)
    consent_person = add_person(session, org, "Priya Rao")
    contract_person = add_person(session, org, "Dev Patel", lawful_basis="contract", source="csv_import")
    org_id, consent_id, contract_id = org.id, consent_person.id, contract_person.id
    r = logged_in.post("/rights/requests", data={"kind": "withdraw_consent", "subject": "priya@seed.example"}, follow_redirects=True)
    assert r.status_code == 200, r.text
    req_id = int(re.search(r"/rights/requests/(\d+)", str(r.url)).group(1))
    logged_in.post(f"/rights/requests/{req_id}/fulfil", data={"action": "stop_contact"}, follow_redirects=True)
    session.rollback()
    session.expire_all()
    assert session.get(Person, consent_id) is None
    assert session.get(Person, contract_id) is not None
    assert session.get(Organization, org_id).status == "qualified"  # only the person asked; the organisation is untouched
    assert is_suppressed(session, "email", "priya@seed.example")


# --- notice tracking ----------------------------------------------------------------------------------
def test_notice_recording_marks_contacts_and_shows_preview(logged_in, session):
    org = seed_org(session)
    pending = add_person(session, org, "Priya Rao", notice_status="pending")
    r = logged_in.get(f"/organizations/{org.id}/notice-text")
    assert r.status_code == 200 and "seed.example" in r.text
    r = logged_in.post(f"/organizations/{org.id}/notice", data={"channel": "email", "person_id": 0, "notes": "first mail"}, follow_redirects=True)
    assert r.status_code == 200 and "Notice recorded" in r.text and "first mail" in r.text
    session.rollback()
    session.expire_all()
    event = session.scalar(select(NoticeEvent))
    assert event.channel == "email" and event.text_hash and event.organization_id == org.id
    assert session.get(Organization, org.id).notice_sent_at is not None
    assert session.get(Person, pending.id).notice_status == "sent"
    assert logged_in.post(f"/organizations/{org.id}/notice", data={"channel": "pigeon"}).status_code == 400


# --- status → tracking + CRM --------------------------------------------------------------------------
def test_status_change_enables_tracking_and_queues_crm_when_configured(logged_in, session, settings, monkeypatch):
    org = seed_org(session, status="reviewing")
    monkeypatch.setattr(settings, "espocrm_base_url", "https://crm.example")
    monkeypatch.setattr(settings, "espocrm_api_key", "k")
    r = logged_in.post(f"/organizations/{org.id}/status", data={"status": "qualified", "note": ""}, follow_redirects=True)
    assert r.status_code == 200 and "tracked" in r.text
    session.rollback()
    session.expire_all()
    org = session.get(Organization, org.id)
    assert org.tracked and org.next_recheck_at is not None
    assert session.scalar(select(Job).where(Job.type == "crm_sync")) is not None
    # manual push and tracking toggle
    r = logged_in.post(f"/organizations/{org.id}/crm/push", follow_redirects=True)
    assert "Queued for the CRM" in r.text
    r = logged_in.post(f"/organizations/{org.id}/tracking", data={"tracked": "0"}, follow_redirects=True)
    assert "disabled" in r.text
    session.rollback()
    session.expire_all()
    assert session.get(Organization, org.id).tracked is False
    r = logged_in.post(f"/organizations/{org.id}/status", data={"status": "disqualified", "note": "closed"}, follow_redirects=True)
    session.rollback()
    session.expire_all()
    assert session.get(Organization, org.id).tracked is False


def test_crm_push_refused_when_unconfigured_or_ineligible(logged_in, session, settings, monkeypatch):
    org = seed_org(session, status="new")
    r = logged_in.post(f"/organizations/{org.id}/crm/push", follow_redirects=True)
    assert "not configured" in r.text
    monkeypatch.setattr(settings, "espocrm_base_url", "https://crm.example")
    monkeypatch.setattr(settings, "espocrm_api_key", "k")
    r = logged_in.post(f"/organizations/{org.id}/crm/push", follow_redirects=True)
    assert "Not pushed" in r.text
    assert session.scalar(select(Job).where(Job.type == "crm_sync")) is None


# --- people / connections ------------------------------------------------------------------------------
def test_owner_connections_upload_people_page_and_known_filter(logged_in, session):
    org = seed_org(session, domain="coastalbakes.example", name="Coastal Bakes")
    r = logged_in.post("/imports/linkedin", files={"file": ("Connections.csv", CONNECTIONS_CSV, "text/csv")}, follow_redirects=True)
    assert r.status_code == 200 and "3 rows, 3 new" in r.text and "1 linked" in r.text
    assert "Priya Rao" in r.text and "Coastal Bakes" in r.text and "unlinked" in r.text
    r = logged_in.get("/organizations?known=yes")
    assert "Coastal Bakes" in r.text and "known" in r.text
    r = logged_in.get(f"/organizations/{org.id}")
    assert "you know 1 person here" in r.text
    # bulk persons export never includes connections
    r = logged_in.get("/exports/persons.csv")
    assert "Priya" not in r.text and "Dev Patel" not in r.text
    # owner links an unlinked connection by hand
    dev = session.scalar(select(Person).where(Person.full_name == "Dev Patel"))
    r = logged_in.post(f"/people/{dev.id}/link", data={"organization_id": org.id}, follow_redirects=True)
    assert "Linked Dev Patel" in r.text
    r = logged_in.post("/imports/linkedin", files={"file": ("bad.csv", b"a,b\n1,2\n", "text/csv")}, follow_redirects=True)
    assert "Connections.csv" in r.text  # error flash mentions the expected file
    session.rollback()
    batch_kinds = {b for (b,) in session.execute(select(__import__("prospector.models", fromlist=["ImportBatch"]).ImportBatch.kind))}
    assert batch_kinds == {"linkedin_connections"}


# --- runs: GitHub and HTTP Archive providers ------------------------------------------------------------
def test_runs_page_offers_new_providers_and_queues_jobs(logged_in, session):
    r = logged_in.get("/runs")
    assert "GitHub agencies" in r.text and "HTTP Archive" in r.text
    r = logged_in.post("/runs/github", data={"location": "Bengaluru", "query": "wordpress", "max_pages": 2}, follow_redirects=True)
    assert r.status_code == 200 and "GitHub run" in r.text
    r = logged_in.post("/runs/httparchive", data={"month": "2026-09", "country": "in", "limit": 100}, follow_redirects=True)
    assert r.status_code == 200 and "HTTP Archive feed run" in r.text
    session.rollback()
    runs = session.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id)).all()
    assert [(x.provider, x.track) for x in runs] == [("github_orgs", "agency"), ("httparchive_feed", "smb")]
    assert runs[0].location == "Bengaluru" and runs[0].max_pages == 2
    jobs = {j.type: j.payload for j in session.scalars(select(Job))}
    assert jobs["github_org_search"]["run_id"] == runs[0].id
    assert jobs["httparchive_feed_run"] == {"run_id": runs[1].id, "month": "2026-09", "country": "in", "limit": 100}
    assert logged_in.post("/runs/github", data={"location": " "}, follow_redirects=True).text.count("Location is required") == 1


# --- organisation detail: Companies House panel ----------------------------------------------------------
@pytest.fixture()
def companies_house(settings, monkeypatch):
    from prospector.connectors.companies_house import CompaniesHouseConnector

    monkeypatch.setattr(settings, "companies_house_api_key", "ch-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Basic ")
        if request.url.path == "/search/companies":
            return httpx.Response(200, json={"items": [{"title": "SEED CO LIMITED", "company_number": "01234567", "company_status": "active",
                                                        "address_snippet": "1 High St, London, EC1A 1AA"}]})
        if request.url.path == "/company/01234567":
            return httpx.Response(200, json={"company_name": "SEED CO LIMITED", "company_number": "01234567", "company_status": "active",
                                             "date_of_creation": "2015-02-03", "sic_codes": ["62012"],
                                             "registered_office_address": {"address_line_1": "1 High St", "locality": "London", "postal_code": "EC1A 1AA"}})
        if request.url.path == "/company/01234567/officers":
            return httpx.Response(200, json={"items": [{"name": "DOE, Jane", "officer_role": "director", "appointed_on": "2015-02-03"}]})
        return httpx.Response(404, json={})

    connector = CompaniesHouseConnector(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))
    tasks.overrides["companies_house"] = connector
    yield connector
    tasks.overrides.clear()


def test_companies_house_panel_search_apply_and_live_officers(logged_in, session, companies_house):
    org = seed_org(session, domain="seedco.co.uk", name="Seed Co", country="GB")
    r = logged_in.get(f"/organizations/{org.id}")
    assert "Companies House" in r.text
    r = logged_in.get(f"/organizations/{org.id}/registry/search?q=Seed+Co")
    assert r.status_code == 200 and "SEED CO LIMITED" in r.text and "01234567" in r.text
    r = logged_in.post(f"/organizations/{org.id}/registry/apply", data={"company_number": "01234567"}, follow_redirects=True)
    assert r.status_code == 200 and "Applied Companies House facts" in r.text
    session.rollback()
    session.expire_all()
    org = session.get(Organization, org.id)
    assert org.legal_name == "SEED CO LIMITED" and org.registry_ids["company_number"] == "01234567" and org.postal_code == "EC1A 1AA"
    sources = {e.source for e in org.evidence if e.field.startswith("organization.registry") or e.field == "organization.legal_name"}
    assert sources == {"companies_house"}
    assert "SIC 62012" in org.notes
    r = logged_in.get(f"/organizations/{org.id}/registry/officers/01234567")
    assert "DOE, Jane" in r.text and "viewing is audited" in r.text
    session.rollback()
    assert session.scalars(select(Person)).all() == []  # officers are displayed, never stored
    actions = {a.action for a in session.scalars(select(ActivityLog))}
    assert {"registry.search", "registry.apply", "registry.officers_view"} <= actions


def test_detail_page_shows_inbound_badge_crm_card_and_field_data(logged_in, session):
    org = seed_org(session, lead_source="inbound")
    org.inbound_at = org.created_at
    session.add(CrmLink(target="espocrm", entity_type="organization", entity_id=org.id, organization_id=org.id, remote_id="a1",
                        remote_url="https://crm.example/#Account/view/a1"))
    session.commit()
    r = logged_in.get(f"/organizations/{org.id}")
    assert "inbound" in r.text and "in CRM" in r.text and "crm.example/#Account/view/a1" in r.text and "Record notice" in r.text
    r = logged_in.get("/organizations?lead_source=inbound&sort=inbound")
    assert "Seed Co" in r.text
    r = logged_in.get("/people")
    assert r.status_code == 200 and "No connections imported yet" in r.text
    r = logged_in.get("/inbound")
    assert r.status_code == 200


def test_suppression_table_gets_hashes_not_values(session):
    seed_org(session)
    from prospector.compliance.suppression import add_suppression

    add_suppression(session, "email", "someone@seed.example", reason="t")
    session.commit()
    row = session.scalar(select(Suppression))
    assert "someone" not in (row.value_hash or "") and len(row.value_hash) == 64
