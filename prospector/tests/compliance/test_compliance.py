"""Compliance rules R1–R11 as executable checks."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from prospector.compliance.policies import PlacesStoragePolicy
from prospector.compliance.retention import run_retention
from prospector.compliance.suppression import (
    add_suppression,
    anonymise_organization,
    erase_person,
    find_subjects,
    hash_value,
    is_suppressed,
    organization_is_suppressed,
    suppress_organization,
)
from prospector.connectors.base import REGISTRY, PersonFactRejected, ensure_connector_states
from prospector.connectors.csv_import import CsvImportConnector
from prospector.connectors.dns_asn import DnsAsnConnector
from prospector.core.facts import Fact, apply_facts, evidence_coverage
from prospector.core.organizations import ensure_website
from prospector.models import (
    ActivityLog,
    Evidence,
    ImportBatch,
    Job,
    Organization,
    Person,
    PlaceLink,
    RawSnapshot,
    Suppression,
)
from prospector.util import utcnow


def org_with_contacts(session, domain="acme.example", name="Acme Interiors"):
    org = Organization(domain=domain, website_url=f"https://{domain}/", track="smb", status="new", country="IN", city="Udupi")
    session.add(org)
    session.flush()
    site = ensure_website(session, org, f"https://{domain}/")
    apply_facts(session, org, [
        Fact("organization.display_name", name, "own_website", f"https://{domain}/", 0.9),
        Fact("organization.phone", "+918202520000", "own_website", f"https://{domain}/contact/", 0.9),
        Fact("organization.email", f"info@{domain}", "own_website", f"https://{domain}/contact/", 0.9),
    ], site)
    return org, site


# R1 — provenance
def test_r1_every_fact_has_evidence(session):
    org, site = org_with_contacts(session)
    rows = session.scalars(select(Evidence).where(Evidence.organization_id == org.id)).all()
    assert {e.field for e in rows} == {"organization.display_name", "organization.phone", "organization.email"}
    assert all(e.source_url.startswith("https://acme.example/") and e.fetched_at for e in rows)
    gaps = evidence_coverage(session, org)
    assert not {"organization.display_name", "organization.phone", "organization.email"} & set(gaps)
    assert "organization.website_url" in gaps  # set directly by the helper, so flagged as lacking evidence
    with pytest.raises(ValueError):
        apply_facts(session, org, [Fact("organization.phone", "+911234567890", "own_website", "", 0.9)], site)


# R2 — automated connectors never create person data
def test_r2_automated_connectors_cannot_create_persons(session):
    ensure_connector_states(session)
    automated = {name for name, cls in REGISTRY.items() if name != "csv_import"}
    assert automated >= {"google_places", "website_probe", "dns_asn", "pagespeed"}
    assert all(REGISTRY[n].creates_persons is False for n in automated)
    with pytest.raises(PersonFactRejected):
        DnsAsnConnector().check_facts([Fact("person.email", "a@b.example", "dns", "dns://x", 1.0)])
    assert "person" not in Fact("organization.email", "x@y.example", "own_website", "https://y.example", 1).entity_type


def test_r2_import_without_lawful_basis_creates_no_persons(session, settings):
    batch = ImportBatch(kind="generic", filename="x.csv", track="smb", column_mapping={})
    session.add(batch)
    session.flush()
    rows = [{"company": "Zed Co", "website": "https://zed.example", "first_name": "Zara", "last_name": "Z", "person_email": "zara@zed.example"}]
    mapping = {"company": "company", "website": "website", "first_name": "first_name", "last_name": "last_name", "person_email": "person_email"}
    summary = CsvImportConnector(settings).import_rows(session, batch, rows, mapping, lawful_basis=None)
    assert summary.organizations_created == 1 and summary.persons_created == 0
    assert session.scalar(select(Person)) is None
    summary = CsvImportConnector(settings).import_rows(session, batch, rows, mapping, lawful_basis="contract", notice_status="not_required")
    assert summary.persons_created == 1
    person = session.scalar(select(Person))
    assert person.lawful_basis == "contract" and person.notice_status == "not_required"


# R3 — Places storage policy
def test_r3_places_policy_keeps_only_id_and_coords():
    raw = {"id": "ChIJx", "displayName": {"text": "Secret Name"}, "nationalPhoneNumber": "123", "websiteUri": "https://x.example",
           "rating": 4.9, "location": {"latitude": 1.0, "longitude": 2.0}}
    kept = PlacesStoragePolicy().apply(raw)
    assert set(kept) == {"place_id", "lat", "lng", "coords_expires_at"}
    assert kept["coords_expires_at"] <= utcnow() + timedelta(days=30, minutes=1)


# R5/R7 — suppression hashing, lookup and exports
def test_r5_suppression_is_hashed_and_blocks(session, settings):
    org, _ = org_with_contacts(session)
    add_suppression(session, "domain", "ACME.example", reason="opt-out", requested_by="tester")
    row = session.scalar(select(Suppression))
    assert row.value_hash == hash_value("domain", "acme.example") and "acme" not in row.value_hash
    assert is_suppressed(session, "domain", "https://www.acme.example/page")
    assert organization_is_suppressed(session, org)
    assert not is_suppressed(session, "domain", "other.example")
    add_suppression(session, "phone", "0820 252 0000", reason="opt-out")
    assert is_suppressed(session, "phone", "+918202520000")


def test_r5_rights_lookup_and_erasure(session, settings):
    org, _ = org_with_contacts(session)
    person = Person(organization=org, full_name="Rita Rao", email="rita@acme.example", source="manual_research",
                    lawful_basis="manual_research", notice_status="pending")
    session.add(person)
    session.flush()
    matches = find_subjects(session, "acme.example")
    assert org in matches.organizations
    matches = find_subjects(session, "rita@acme.example")
    assert person in matches.persons
    erase_person(session, person, reason="erasure request")
    assert session.scalar(select(Person)) is None
    assert is_suppressed(session, "email", "rita@acme.example")
    anonymise_organization(session, org, reason="erasure request")
    assert org.status == "anonymised" and org.phones == [] and org.emails == [] and org.display_name is None
    assert session.scalars(select(Evidence).where(Evidence.organization_id == org.id)).all() == []
    assert is_suppressed(session, "domain", "acme.example")
    actions = {a.action for a in session.scalars(select(ActivityLog))}
    assert {"person.erase", "organization.anonymise", "suppression.add"} <= actions


def test_r7_suppress_organization_adds_all_identifiers(session):
    org, _ = org_with_contacts(session)
    session.add(PlaceLink(organization=org, provider="google_places", place_id="ChIJ123"))
    session.flush()
    suppress_organization(session, org, reason="requested")
    assert org.status == "suppressed"
    assert is_suppressed(session, "place_id", "ChIJ123") and is_suppressed(session, "email", "info@acme.example")


# R6 — retention
def test_r6_retention_purges_on_schedule(session, settings, tmp_path):
    now = utcnow()
    old = now - timedelta(days=400)
    org, site = org_with_contacts(session, "old.example", "Old Co")
    org.created_at = old
    fresh, _ = org_with_contacts(session, "fresh.example", "Fresh Co")
    engaged, _ = org_with_contacts(session, "engaged.example", "Engaged Co")
    engaged.created_at = old
    engaged.status = "customer"
    snap_file = tmp_path / "snap.html.gz"
    snap_file.write_bytes(b"x")
    session.add(RawSnapshot(website=site, kind="html", path=str(snap_file), source_url=site.url, expires_at=now - timedelta(days=1)))
    session.add(PlaceLink(organization=fresh, provider="google_places", place_id="ChIJfresh", lat=1.0, lng=2.0, coords_expires_at=now - timedelta(days=1)))
    p_old = Person(organization=fresh, full_name="Old Person", source="manual_research", lawful_basis="manual_research", notice_status="pending")
    p_consent = Person(organization=fresh, full_name="Consented", source="form", lawful_basis="consent", notice_status="not_required")
    session.add_all([p_old, p_consent])
    session.flush()
    p_old.created_at = now - timedelta(days=200)
    p_consent.created_at = now - timedelta(days=200)
    from prospector.models import ConsentRecord

    consent = ConsentRecord(person_id=p_consent.id, purpose="b2b_contact", text_version="v1", captured_via="form", captured_at=now)
    session.add(consent)
    session.flush()
    p_consent.consent_id = consent.id
    job = Job(type="noop", payload={}, status="done", run_after=now)
    session.add(job)
    session.flush()
    job.created_at = now - timedelta(days=45)
    session.commit()

    report = run_retention(session, settings, now=now)
    assert report["raw_snapshots_deleted"] == 1 and not snap_file.exists()
    assert report["coords_expired"] == 1
    assert report["organizations_anonymised"] == 1 and org.status == "anonymised" and engaged.status == "customer"
    assert report["persons_deleted"] == 1
    assert {p.full_name for p in session.scalars(select(Person))} == {"Consented"}
    assert report["jobs_deleted"] == 1
    # anonymisation by retention does not add to the suppression list (no request was made)
    assert session.scalar(select(Suppression)) is None


# R9 — audit trail for sensitive actions is covered in the API tests; R10 kill switch + review here
def test_r10_connector_state_defaults(session):
    states = {s.name: s for s in ensure_connector_states(session)}
    assert states["google_places"].requires_terms_review and states["pagespeed"].requires_terms_review and states["dns_asn"].requires_terms_review
    assert states["website_probe"].requires_terms_review is False and states["csv_import"].requires_terms_review is False
    assert states["google_places"].daily_cap == 200 and states["pagespeed"].daily_cap == 2000
