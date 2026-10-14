"""Phase 2 — inbound consented leads: webhook, hosted forms, Sign In with LinkedIn."""

from __future__ import annotations

import json
import re
import time

import httpx
import pytest
import respx
from sqlalchemy import select

from prospector.connectors.inbound_webhook import SignatureError, sign, verify_signature
from prospector.connectors.linkedin_oidc import TOKEN_URL, USERINFO_URL, parse_userinfo
from prospector.inbound.forms import SubmissionError, parse_payload
from prospector.models import ConsentRecord, InboundSubmission, Job, Organization, Person

SECRET = "test-webhook-secret"


@pytest.fixture()
def inbound_settings(settings, monkeypatch):
    monkeypatch.setattr(settings, "inbound_webhook_secret", SECRET)
    return settings


def _signed(payload: dict, *, secret: str = SECRET, ts: int | None = None, key: str | None = None) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    ts = ts or int(time.time())
    headers = {"content-type": "application/json", "X-Prospector-Timestamp": str(ts), "X-Prospector-Signature": sign(secret, ts, body)}
    if key:
        headers["X-Prospector-Idempotency-Key"] = key
    return body, headers


PAYLOAD = {
    "name": "Priya Rao",
    "email": "priya@coastalbakes.example",
    "company": "Coastal Bakes",
    "website": "coastalbakes.example",
    "phone": "+91 98450 12345",
    "city": "Mangaluru",
    "message": "Our checkout is slow on phones.",
    "consent": {"granted": True, "text_version": "2026-10-v1"},
    "source_url": "https://www.pb4host.com/speed-evaluation/",
    "submitted_at": "2026-10-14T09:30:00Z",
    "current_host": "shared cPanel",
}


# --- signature + parsing -----------------------------------------------------------------------
def test_signature_verification_rejects_tampering_and_replay():
    body = b'{"a":1}'
    ts = int(time.time())
    good = sign(SECRET, ts, body)
    verify_signature(SECRET, str(ts), good, body)
    with pytest.raises(SignatureError):
        verify_signature(SECRET, str(ts), good, b'{"a":2}')
    with pytest.raises(SignatureError):
        verify_signature("other-secret", str(ts), good, body)
    with pytest.raises(SignatureError):
        verify_signature(SECRET, str(ts - 3600), sign(SECRET, ts - 3600, body), body)  # stale
    with pytest.raises(SignatureError):
        verify_signature(None, str(ts), good, body)  # not configured → nothing is accepted


def test_parse_payload_requires_consent_and_identity(settings):
    with pytest.raises(SubmissionError, match="consent"):
        parse_payload("speed_evaluation", {**PAYLOAD, "consent": False}, settings=settings)
    with pytest.raises(SubmissionError, match="e-mail"):
        parse_payload("speed_evaluation", {**PAYLOAD, "email": "not-an-email"}, settings=settings)
    with pytest.raises(SubmissionError, match="unknown form"):
        parse_payload("newsletter", PAYLOAD, settings=settings)
    sub = parse_payload("speed_evaluation", PAYLOAD, settings=settings)
    assert sub.website == "https://coastalbakes.example" and sub.extra == {"current_host": "shared cPanel"}
    assert sub.consent_version == "2026-10-v1" and sub.submitted_at.hour == 9


# --- webhook ---------------------------------------------------------------------------------------
def test_webhook_creates_org_person_consent_and_queues_followups(client, session, inbound_settings):
    body, headers = _signed(PAYLOAD, key="wpforms-1001")
    r = client.post("/inbound/forms/speed_evaluation", content=body, headers=headers)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["accepted"] is True and data["duplicate"] is False
    org = session.get(Organization, data["organization_id"])
    assert org.domain == "coastalbakes.example" and org.display_name == "Coastal Bakes" and org.track == "smb"
    assert org.lead_source == "inbound" and org.inbound_at is not None and org.status == "reviewing"
    assert org.phones == ["+919845012345"] and org.city == "Mangaluru"
    assert all(e.source == "inbound_form" and e.source_url.startswith("inbound://speed_evaluation/") for e in org.evidence)
    person = org.persons[0]
    assert person.lawful_basis == "consent" and person.notice_status == "sent" and person.source == "inbound_form"
    assert person.email == "priya@coastalbakes.example" and person.meta["form"] == "speed_evaluation"
    consent = session.get(ConsentRecord, person.consent_id)
    assert consent.purpose == "speed_evaluation_contact" and consent.text_version == "2026-10-v1" and consent.captured_via == "webhook"
    assert consent.proof_ref == "https://www.pb4host.com/speed-evaluation/" and consent.captured_at.hour == 9
    sub = session.scalar(select(InboundSubmission))
    assert sub.idempotency_key == "wpforms-1001" and sub.status == "processed" and sub.message.startswith("Our checkout")
    types = sorted(j.type for j in session.scalars(select(Job)))
    assert "website_probe" in types and "crm_sync" in types
    probe = session.scalar(select(Job).where(Job.type == "website_probe"))
    assert probe.payload["force_pagespeed"] is True

    # redelivery with the same idempotency key is a no-op
    r2 = client.post("/inbound/forms/speed_evaluation", content=body, headers=headers)
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    assert session.scalar(select(Person).where(Person.email == "priya@coastalbakes.example")) is not None
    assert len(session.scalars(select(Person)).all()) == 1


def test_webhook_rejects_bad_signature_and_missing_consent(client, session, inbound_settings):
    body, headers = _signed(PAYLOAD)
    headers["X-Prospector-Signature"] = "sha256=" + "0" * 64
    assert client.post("/inbound/forms/speed_evaluation", content=body, headers=headers).status_code == 401
    body, headers = _signed({**PAYLOAD, "consent": {"granted": False}})
    r = client.post("/inbound/forms/speed_evaluation", content=body, headers=headers)
    assert r.status_code == 422 and "consent" in r.json()["error"]
    assert session.scalar(select(Organization)) is None  # nothing stored without consent
    assert client.post("/inbound/forms/unknown", content=body, headers=headers).status_code == 404


def test_webhook_business_mailbox_implies_website_and_freemail_does_not(client, session, inbound_settings):
    body, headers = _signed({**PAYLOAD, "website": "", "email": "ops@pixelgrove.example", "company": "Pixelgrove"}, key="k1")
    r = client.post("/inbound/forms/agency_partner", content=body, headers=headers)
    org = session.get(Organization, r.json()["organization_id"])
    assert org.domain == "pixelgrove.example" and org.track == "agency"
    body, headers = _signed({**PAYLOAD, "website": "", "email": "someone@gmail.com", "company": "Solo Studio"}, key="k2")
    r = client.post("/inbound/forms/speed_evaluation", content=body, headers=headers)
    session.rollback()  # start a fresh read transaction (WAL snapshot)
    org = session.get(Organization, r.json()["organization_id"])
    assert org.domain is None and org.display_name == "Solo Studio"


# --- hosted form -----------------------------------------------------------------------------------
def test_hosted_form_submission_and_honeypot(client, session, settings):
    r = client.get("/forms/speed_evaluation")
    assert r.status_code == 200 and "Request a free WordPress speed evaluation" in r.text
    assert "Sign in with LinkedIn" not in r.text  # not configured
    token = re.search(r'name="form_token" value="([^"]+)"', r.text).group(1)
    data = {"form_token": token, "name": "Arun K", "email": "arun@zenclinic.example", "company": "Zen Clinic",
            "website": "https://zenclinic.example/", "consent": "yes", "consent_version": settings.inbound_consent_version}
    r = client.post("/forms/speed_evaluation", data=data)
    assert r.status_code == 200 and "Thank you" in r.text
    person = session.scalar(select(Person).where(Person.email == "arun@zenclinic.example"))
    assert person is not None and person.lawful_basis == "consent"
    sub = session.scalar(select(InboundSubmission).where(InboundSubmission.person_id == person.id))
    assert sub.captured_via == "hosted_form"

    # honeypot filled → pretend success, store nothing
    r = client.post("/forms/speed_evaluation", data={**data, "email": "bot@spam.example", "website_confirm": "http://spam"})
    assert r.status_code == 200 and session.scalar(select(Person).where(Person.email == "bot@spam.example")) is None
    # missing/forged token → rejected
    r = client.post("/forms/speed_evaluation", data={**data, "form_token": "forged"})
    assert r.status_code == 400
    # missing consent → validation error, nothing stored
    r = client.post("/forms/speed_evaluation", data={**data, "consent": "", "email": "noconsent@zenclinic.example"})
    assert r.status_code == 422 and session.scalar(select(Person).where(Person.email == "noconsent@zenclinic.example")) is None


# --- LinkedIn OIDC -----------------------------------------------------------------------------------
def test_parse_userinfo_keeps_only_identity_fields():
    ident = parse_userinfo({"sub": "abc123", "name": "Priya Rao", "given_name": "Priya", "family_name": "Rao",
                            "email": "priya@coastalbakes.example", "email_verified": True, "picture": "https://media.licdn.example/p.jpg",
                            "locale": {"country": "IN", "language": "en"}, "headline": "should not be kept"})
    assert ident.as_dict() == {"provider": "linkedin", "sub": "abc123", "name": "Priya Rao", "email": "priya@coastalbakes.example",
                               "email_verified": True, "picture": "https://media.licdn.example/p.jpg"}


@respx.mock
def test_linkedin_signin_prefills_and_binds_identity(client, session, settings, monkeypatch):
    monkeypatch.setattr(settings, "linkedin_client_id", "cid")
    monkeypatch.setattr(settings, "linkedin_client_secret", "csecret")
    monkeypatch.setattr(settings, "base_url", "http://testserver")
    r = client.get("/forms/agency_partner")
    assert "Sign in with LinkedIn" in r.text
    r = client.get("/forms/agency_partner/linkedin/start", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("https://www.linkedin.com/oauth/v2/authorization?") and "scope=openid+profile+email" in location
    state = re.search(r"state=([^&]+)", location).group(1)

    token_route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600}))
    respx.get(USERINFO_URL).mock(return_value=httpx.Response(200, json={
        "sub": "li-sub-42", "name": "Dev Patel", "email": "dev@pixelgrove.example", "email_verified": True, "picture": None}))
    r = client.get(f"/auth/linkedin/callback?code=authcode&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/forms/agency_partner"
    assert "code=authcode" in token_route.calls.last.request.content.decode()
    r = client.get("/forms/agency_partner")
    assert "Signed in with LinkedIn as <strong>Dev Patel</strong>" in r.text and "readonly" in r.text
    token = re.search(r'name="form_token" value="([^"]+)"', r.text).group(1)
    r = client.post("/forms/agency_partner", data={"form_token": token, "name": "ignored", "email": "ignored@x.example",
                                                   "company": "Pixelgrove", "website": "pixelgrove.example", "team_size": "2-5",
                                                   "consent": "yes"})
    assert r.status_code == 200 and "identity confirmed via LinkedIn" in r.text
    person = session.scalar(select(Person).where(Person.external_id == "li-sub-42"))
    assert person.full_name == "Dev Patel" and person.email == "dev@pixelgrove.example" and person.source == "linkedin_oidc"
    assert person.meta["email_verified"] is True and person.meta["team_size"] == "2-5"
    consent = session.get(ConsentRecord, person.consent_id)
    assert consent.captured_via == "linkedin_oidc"
    assert person.organization.domain == "pixelgrove.example" and person.organization.track == "agency"

    # bad / expired state is refused
    assert client.get("/auth/linkedin/callback?code=x&state=forged").status_code == 400
