"""CRM sync (EspoCRM adapter): push only notice-ready, non-suppressed records; pull status back."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from prospector.compliance.suppression import add_suppression
from prospector.crm.base import person_is_notice_ready
from prospector.crm.espocrm import EspoCrmAdapter
from prospector.crm.sync import eligibility, pull_status, push_organization
from prospector.jobs import tasks
from prospector.models import ConsentRecord, CrmLink, Evidence, Organization, Person, Website

BASE = "https://crm.pb4host.example"


class FakeEspo:
    """Minimal in-memory EspoCRM: Account/Contact list+create+update+get."""

    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.contacts: dict[str, dict] = {}
        self.calls: list[str] = []
        self.auth_seen: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        self.auth_seen.add(request.headers.get("x-api-key", ""))
        path = request.url.path.removeprefix("/api/v1/")
        entity, _, rid = path.partition("/")
        store = self.accounts if entity == "Account" else self.contacts
        if request.method == "GET" and not rid:
            attr, value = request.url.params.get("where[0][attribute]"), request.url.params.get("where[0][value]")
            found = [r for r in store.values() if r.get(attr) == value]
            return httpx.Response(200, json={"total": len(found), "list": found})
        if request.method == "GET":
            return httpx.Response(200, json=store[rid]) if rid in store else httpx.Response(404)
        body = json.loads(request.content)
        if request.method == "POST":
            new_id = f"{entity[:1].lower()}{len(store) + 1:03d}"
            store[new_id] = {"id": new_id, **body}
            return httpx.Response(200, json=store[new_id])
        if request.method == "PUT":
            if rid not in store:
                return httpx.Response(404)
            store[rid].update(body)
            return httpx.Response(200, json=store[rid])
        return httpx.Response(405)


@pytest.fixture()
def espo(settings, monkeypatch):
    monkeypatch.setattr(settings, "espocrm_base_url", BASE)
    monkeypatch.setattr(settings, "espocrm_api_key", "espo-key")
    monkeypatch.setattr(settings, "base_url", "https://prospector.pb4host.example")
    fake = FakeEspo()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler), headers={"X-Api-Key": "espo-key"})
    adapter = EspoCrmAdapter(BASE, "espo-key", client=client)
    return fake, adapter


def _org(session, **kw):
    org = Organization(display_name="Coastal Bakes", domain="coastalbakes.example", website_url="https://coastalbakes.example/",
                       city="Mangaluru", country="IN", track="smb", status="qualified", score_total=72, score_band="hot",
                       emails=["hello@coastalbakes.example"], phones=["+918242456789"], signals=["wordpress", "woocommerce"])
    for k, v in kw.items():
        setattr(org, k, v)
    session.add(org)
    session.add(Website(organization=org, url="https://coastalbakes.example/", host_key="coastalbakes.example", cms="wordpress", cms_version="6.4.3",
                        is_woocommerce=True, host_provider="Hostinger"))
    session.add(Evidence(organization=org, entity_type="organization", field="organization.display_name", value="Coastal Bakes",
                         source="own_website", source_url="https://coastalbakes.example/"))
    session.flush()
    return org


def test_person_notice_readiness_rules(session):
    org = _org(session)
    consent = ConsentRecord(purpose="x", text_version="v1", captured_via="webhook")
    session.add(consent)
    session.flush()
    ready = Person(organization=org, full_name="Priya Rao", source="inbound_form", lawful_basis="consent", consent_id=consent.id, notice_status="sent")
    pending = Person(organization=org, full_name="Dev Patel", source="linkedin_connections", lawful_basis="legitimate_interest", notice_status="pending")
    sent_li = Person(organization=org, full_name="Sam Lee", source="manual_research", lawful_basis="manual_research", notice_status="sent")
    objected = Person(organization=org, full_name="No Thanks", source="manual_research", lawful_basis="manual_research", notice_status="objected")
    dnc = Person(organization=org, full_name="Stop", source="manual_research", lawful_basis="manual_research", notice_status="sent", do_not_contact=True)
    session.add_all([ready, pending, sent_li, objected, dnc])
    assert [person_is_notice_ready(p) for p in (ready, pending, sent_li, objected, dnc)] == [True, False, True, False, False]


def test_push_creates_account_and_ready_contacts_then_skips_unchanged(session, settings, espo, owner):
    fake, adapter = espo
    org = _org(session)
    consent = ConsentRecord(purpose="speed_evaluation_contact", text_version="v1", captured_via="webhook")
    session.add(consent)
    session.flush()
    session.add(Person(organization=org, full_name="Priya Rao", email="priya@coastalbakes.example", role_title="Founder", source="inbound_form",
                       lawful_basis="consent", consent_id=consent.id, notice_status="sent"))
    session.add(Person(organization=org, full_name="Dev Patel", source="linkedin_connections", lawful_basis="legitimate_interest", notice_status="pending"))
    session.commit()

    outcome = push_organization(session, org, adapter, notice_text="NOTICE TEXT v1", settings=settings, actor=owner)
    session.commit()
    assert outcome.pushed and len(outcome.contacts) == 1 and outcome.skipped_contacts == 1
    account = next(iter(fake.accounts.values()))
    assert account["name"] == "Coastal Bakes" and account["website"] == "https://coastalbakes.example/"
    assert account["emailAddress"] == "hello@coastalbakes.example" and account["billingAddressCity"] == "Mangaluru"
    assert "NOTICE TEXT v1" in account["description"] and "own_website" in account["description"]
    assert "https://prospector.pb4host.example/organizations/" in account["description"]
    contact = next(iter(fake.contacts.values()))
    assert contact["firstName"] == "Priya" and contact["lastName"] == "Rao" and contact["accountId"] == account["id"]
    assert "Lawful basis: consent" in contact["description"]
    assert fake.auth_seen == {"espo-key"}
    links = session.scalars(select(CrmLink)).all()
    assert {(link.entity_type, link.remote_id) for link in links} == {("organization", account["id"]), ("person", contact["id"])}
    assert org.status == "exported"  # qualified → exported once it reached the CRM

    # second push with nothing changed: no writes
    before = len(fake.calls)
    outcome2 = push_organization(session, org, adapter, notice_text="NOTICE TEXT v1", settings=settings)
    assert not outcome2.pushed and outcome2.reason == "unchanged since last push" and len(fake.calls) == before

    # a change is pushed as an update to the same remote record
    org.score_total = 85
    outcome3 = push_organization(session, org, adapter, notice_text="NOTICE TEXT v1", settings=settings)
    assert outcome3.pushed and len(fake.accounts) == 1 and "score 85" in account["description"]


def test_push_refuses_suppressed_and_unqualified_orgs(session, settings, espo):
    fake, adapter = espo
    cold = _org(session, status="new", domain="cold.example", website_url="https://cold.example/")
    ok, reason = eligibility(session, cold)
    assert not ok and "not qualified" in reason
    assert not push_organization(session, cold, adapter, notice_text="n", settings=settings).pushed
    inbound = _org(session, status="reviewing", lead_source="inbound", domain="inbound.example", website_url="https://inbound.example/")
    assert eligibility(session, inbound)[0] is True
    suppressed = _org(session, domain="dnc.example", website_url="https://dnc.example/")
    add_suppression(session, "domain", "dnc.example", reason="asked")
    ok, reason = eligibility(session, suppressed)
    assert not ok and "suppression" in reason
    assert fake.calls == []


def test_push_reuses_existing_crm_account_found_by_website(session, settings, espo):
    fake, adapter = espo
    fake.accounts["a999"] = {"id": "a999", "name": "Coastal Bakes (old)", "website": "https://coastalbakes.example", "type": "Customer"}
    org = _org(session)
    session.commit()
    outcome = push_organization(session, org, adapter, notice_text="n", settings=settings)
    assert outcome.pushed and outcome.account.remote_id == "a999" and len(fake.accounts) == 1
    assert fake.accounts["a999"]["name"] == "Coastal Bakes"  # updated in place


def test_pull_status_advances_funnel_and_disqualifies(session, settings, espo):
    fake, adapter = espo
    org = _org(session)
    session.commit()
    push_organization(session, org, adapter, notice_text="n", settings=settings)
    link = session.scalar(select(CrmLink).where(CrmLink.entity_type == "organization"))
    fake.accounts[link.remote_id]["type"] = "Customer"
    assert pull_status(session, org, adapter, settings=settings) == "customer" and org.status == "customer"
    # the CRM cannot move a customer backwards
    fake.accounts[link.remote_id]["type"] = "Prospect"
    pull_status(session, org, adapter, settings=settings)
    assert org.status == "customer"
    other = _org(session, domain="other.example", website_url="https://other.example/")
    session.commit()
    push_organization(session, other, adapter, notice_text="n", settings=settings)
    link2 = session.scalar(select(CrmLink).where(CrmLink.entity_id == other.id, CrmLink.entity_type == "organization"))
    fake.accounts[link2.remote_id]["type"] = "Dead"
    assert pull_status(session, other, adapter, settings=settings) == "disqualified"
    assert other.status == "disqualified" and other.disqualify_reason == "crm:Dead"


def test_crm_tasks_use_the_adapter_and_skip_when_unconfigured(session, settings, espo, monkeypatch):
    fake, adapter = espo
    org = _org(session)
    session.commit()
    tasks.overrides["crm_adapter"] = adapter
    try:
        tasks.crm_sync(session, {"organization_id": org.id, "reason": "test"})
        session.commit()
        assert len(fake.accounts) == 1
        tasks.crm_pull_statuses(session, {})
        assert session.scalar(select(CrmLink)).last_pulled_at is not None
    finally:
        tasks.overrides.clear()
    monkeypatch.setattr(settings, "espocrm_api_key", None)
    other = _org(session, domain="x.example", website_url="https://x.example/")
    session.commit()
    tasks.crm_sync(session, {"organization_id": other.id})  # not configured → logged, no error
    assert len(fake.accounts) == 1
