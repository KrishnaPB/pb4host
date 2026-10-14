"""Phase 2 acceptance: an inbound test submission is enriched and visible in the CRM, all through the job queue."""

from __future__ import annotations

import json
import time

import httpx
import respx
from sqlalchemy import select

from prospector.connectors.crux import CRUX_URL, CruxConnector
from prospector.connectors.dns_asn import DnsAsnConnector
from prospector.connectors.inbound_webhook import sign
from prospector.connectors.pagespeed import PSI_URL, PageSpeedConnector
from prospector.connectors.website_probe.health import TlsInfo
from prospector.connectors.website_probe.probe import WebsiteProbeConnector
from prospector.crm.espocrm import EspoCrmAdapter
from prospector.db import session_factory
from prospector.jobs import tasks
from prospector.jobs.queue import run_pending
from prospector.models import ConsentRecord, CrmLink, InboundSubmission, Job, Organization, Person
from tests.conftest import FixtureSite, html_fixture, json_fixture
from tests.contract.test_crm import FakeEspo
from tests.test_pipeline import FakeDns, multi_site_handler

SECRET = "webhook-secret"
CRUX_RESPONSE = {
    "record": {
        "key": {"origin": "https://coastalbakes.example", "formFactor": "PHONE"},
        "metrics": {
            "largest_contentful_paint": {"percentiles": {"p75": 5200}},
            "interaction_to_next_paint": {"percentiles": {"p75": 610}},
            "cumulative_layout_shift": {"percentiles": {"p75": "0.21"}},
        },
        "collectionPeriod": {"firstDate": {"year": 2026, "month": 9, "day": 10}, "lastDate": {"year": 2026, "month": 10, "day": 7}},
    }
}


@respx.mock
def test_inbound_submission_reaches_crm_enriched(client, session, settings, monkeypatch):
    monkeypatch.setattr(settings, "inbound_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "psi_api_key", "psi-key")
    monkeypatch.setattr(settings, "espocrm_base_url", "https://crm.pb4host.example")
    monkeypatch.setattr(settings, "espocrm_api_key", "espo-key")
    respx.get(PSI_URL).mock(return_value=httpx.Response(200, json=json_fixture("psi_mobile.json")))
    respx.post(CRUX_URL).mock(return_value=httpx.Response(200, json=CRUX_RESPONSE))
    sites = {"coastalbakes.example": FixtureSite({
        "/": (200, html_fixture("wp_home.html"), {"server": "LiteSpeed", "x-powered-by": "PHP/7.4.33"}),
        "/contact-us/": (200, html_fixture("wp_contact.html"), None),
    })}
    web = httpx.Client(transport=httpx.MockTransport(multi_site_handler(sites)), follow_redirects=True)
    fake = FakeEspo()
    tasks.overrides.clear()
    tasks.overrides["website_probe"] = WebsiteProbeConnector(settings, client=web, tls_lookup=lambda url: TlsInfo(True, None, None))
    tasks.overrides["dns_asn"] = DnsAsnConnector(settings, client=FakeDns())
    tasks.overrides["pagespeed"] = PageSpeedConnector(settings, client=httpx.Client())
    tasks.overrides["crux"] = CruxConnector(settings, client=httpx.Client())
    tasks.overrides["crm_adapter"] = EspoCrmAdapter("https://crm.pb4host.example", "espo-key",
                                                    client=httpx.Client(transport=httpx.MockTransport(fake.handler), headers={"X-Api-Key": "espo-key"}))
    try:
        payload = {
            "name": "Priya Rao", "email": "priya@coastalbakes.example", "company": "Coastal Bakes", "website": "coastalbakes.example",
            "phone": "+91 98450 12345", "city": "Mangaluru", "message": "Our checkout is slow on phones.",
            "consent": {"granted": True, "text_version": "2026-10-v1"}, "source_url": "https://www.pb4host.com/speed-evaluation/",
            "submitted_at": "2026-10-14T09:30:00Z", "current_host": "shared cPanel",
        }
        body = json.dumps(payload).encode()
        ts = int(time.time())
        started = time.monotonic()
        r = client.post("/inbound/forms/speed_evaluation", content=body,
                        headers={"content-type": "application/json", "X-Prospector-Timestamp": str(ts), "X-Prospector-Signature": sign(SECRET, ts, body)})
        assert r.status_code == 201, r.text

        executed = run_pending(session_factory())
        assert executed >= 5  # probe, dns, psi, crux, score (+ link_known_contacts)
        # the CRM push is deliberately scheduled three minutes after the submission so enrichment lands first;
        # bring it forward instead of sleeping
        session.rollback()
        crm_job = session.scalar(select(Job).where(Job.type == "crm_sync"))
        assert crm_job is not None and crm_job.status == "queued" and (crm_job.run_after - crm_job.created_at).total_seconds() <= 180
        crm_job.run_after = crm_job.created_at
        session.commit()
        executed += run_pending(session_factory())
        elapsed = time.monotonic() - started
        assert elapsed < 600 - 180  # the acceptance budget is ten minutes including the three-minute delay; offline this is seconds

        session.rollback()
        session.expire_all()
        org = session.scalar(select(Organization).where(Organization.domain == "coastalbakes.example"))
        assert org.lead_source == "inbound" and org.inbound_at is not None and org.status in {"reviewing", "exported"}
        site = org.primary_website
        assert site.cms == "wordpress" and site.is_woocommerce and site.host_provider
        assert site.latest_performance("mobile").lh_performance == 31
        field = site.latest_field_performance("mobile")
        assert field is not None and field.field_lcp_p75 == 5200 and field.field_inp_p75 == 610
        assert org.score_total >= 70 and {"slow_mobile_severe", "field_poor"} <= {r["id"] for r in org.score_breakdown["rules"]}

        person = session.scalar(select(Person).where(Person.email == "priya@coastalbakes.example"))
        assert person.lawful_basis == "consent" and person.source == "inbound_form"
        consent = session.get(ConsentRecord, person.consent_id)
        assert consent.text_version == "2026-10-v1" and consent.captured_via == "webhook"
        sub = session.scalar(select(InboundSubmission))
        assert sub.status == "processed" and sub.organization_id == org.id and sub.person_id == person.id

        # visible in the CRM: account + contact, with enrichment and the notice text in the description
        assert len(fake.accounts) == 1 and len(fake.contacts) == 1
        account = next(iter(fake.accounts.values()))
        assert account["name"] == "Coastal Bakes" and "wordpress" in account["description"].lower()
        assert "inbound 2026-" in account["description"] and "Notice text" in account["description"]
        assert f"/organizations/{org.id}" in account["description"]
        contact = next(iter(fake.contacts.values()))
        assert contact["emailAddress"] == "priya@coastalbakes.example" and contact["accountId"] == account["id"]
        assert "consent" in contact["description"]
        links = {(link.entity_type, link.remote_id) for link in session.scalars(select(CrmLink))}
        assert links == {("organization", account["id"]), ("person", contact["id"])}
        assert not session.scalars(select(Job).where(Job.status == "failed")).all()
    finally:
        tasks.overrides.clear()
