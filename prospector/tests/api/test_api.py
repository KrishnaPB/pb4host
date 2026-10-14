import csv
import io
import re

from sqlalchemy import select

from prospector.api.security import hash_password
from prospector.compliance.suppression import add_suppression
from prospector.core.facts import Fact, apply_facts
from prospector.core.organizations import ensure_website
from prospector.models import ActivityLog, Job, Organization, Person, User


def seed_org(session, domain="seed.example", name="Seed Co", status="qualified", score=80):
    org = Organization(domain=domain, website_url=f"https://{domain}/", track="smb", status=status, country="IN", city="Udupi")
    session.add(org)
    session.flush()
    site = ensure_website(session, org, f"https://{domain}/")
    apply_facts(session, org, [
        Fact("organization.display_name", name, "own_website", f"https://{domain}/", 0.9),
        Fact("organization.email", f"info@{domain}", "own_website", f"https://{domain}/contact/", 0.9),
        Fact("website.cms", "wordpress", "own_website", f"https://{domain}/", 0.95),
    ], site)
    org.score_total, org.score_band = score, "hot"
    session.commit()
    return org


def test_login_required_and_bad_password(client, session):
    r = client.get("/organizations", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = client.post("/login", data={"email": "owner@test.example", "password": "wrong", "csrf_token": token})
    assert r.status_code == 401
    assert session.scalar(select(ActivityLog).where(ActivityLog.action == "auth.login_failed")) is not None


def test_csrf_required_for_posts(logged_in, session):
    org = seed_org(session)
    headers = dict(logged_in.headers)
    logged_in.headers.pop("X-CSRF-Token")
    r = logged_in.post(f"/organizations/{org.id}/status", data={"status": "reviewing"})
    assert r.status_code == 403
    logged_in.headers.update(headers)
    r = logged_in.post(f"/organizations/{org.id}/status", data={"status": "reviewing"}, follow_redirects=True)
    assert r.status_code == 200
    session.expire_all()
    assert session.get(Organization, org.id).status == "reviewing"


def test_pages_render(logged_in, session):
    org = seed_org(session)
    for path in ["/", "/runs", "/organizations", f"/organizations/{org.id}", "/capture", "/imports", "/exports",
                 "/suppressions", "/admin/connectors", "/admin/jobs", "/admin/audit", "/admin/users", "/healthz", "/metrics"]:
        r = logged_in.get(path)
        assert r.status_code == 200, path
    r = logged_in.get(f"/organizations/{org.id}")
    assert "Seed Co" in r.text and "own_website" in r.text and "info@seed.example" in r.text


def test_capture_creates_org_and_queues_probe(logged_in, session):
    r = logged_in.post("/capture", data={
        "company": "Captured Agency", "website": "https://captured.example", "reference_url": "https://www.linkedin.com/company/captured",
        "track": "agency", "city": "Udupi", "country": "IN", "phone": "", "email": "", "services": "WordPress", "notes": "",
        "person_name": "Dev Lead", "person_role": "Founder", "person_profile_url": "https://www.linkedin.com/in/devlead",
        "person_email": "", "lawful_basis": "manual_research", "lia_reference": "", "notice_status": "pending",
    }, follow_redirects=True)
    assert r.status_code == 200
    org = session.scalar(select(Organization).where(Organization.domain == "captured.example"))
    assert org is not None and org.track == "agency" and org.display_name == "Captured Agency"
    assert org.social_links.get("linkedin") == "https://www.linkedin.com/company/captured"
    person = session.scalar(select(Person).where(Person.organization_id == org.id))
    assert person.full_name == "Dev Lead" and person.lawful_basis == "manual_research" and person.created_by is not None
    assert session.scalar(select(Job).where(Job.type == "website_probe")) is not None
    assert all(e.source_url == "https://www.linkedin.com/company/captured" for e in org.evidence)


def test_person_requires_lawful_basis_and_lia(logged_in, session):
    org = seed_org(session)
    r = logged_in.post(f"/organizations/{org.id}/persons", data={"full_name": "X", "lawful_basis": "legitimate_interest", "lia_reference": "", "notice_status": "pending"}, follow_redirects=True)
    assert "requires a reference" in r.text
    assert session.scalar(select(Person)) is None
    r = logged_in.post(f"/organizations/{org.id}/persons", data={"full_name": "X", "lawful_basis": "bogus", "notice_status": "pending"})
    assert r.status_code == 400


def test_exports_exclude_suppressed_and_log(logged_in, session):
    keep = seed_org(session, "keep.example", "Keep Co")
    gone = seed_org(session, "gone.example", "Gone Co")
    add_suppression(session, "domain", "gone.example", reason="opt-out")
    session.commit()
    r = logged_in.get("/exports/organizations.csv?status=qualified&min_score=0&mark_exported=true")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["organisation"] for row in rows] == ["Keep Co"]
    assert "PB4HOST identified Keep Co" in rows[0]["notice_text"] and rows[0]["source_summary"] == "own_website"
    session.expire_all()
    assert session.get(Organization, keep.id).status == "exported" and session.get(Organization, gone.id).status == "qualified"
    log = session.scalar(select(ActivityLog).where(ActivityLog.action == "export.organizations"))
    assert log is not None and log.meta["count"] == 1 and log.actor_label == "owner@test.example"


def test_suppress_rights_flow_and_audit(logged_in, session):
    org = seed_org(session)
    r = logged_in.post("/rights/lookup", data={"query": "seed.example"})
    assert r.status_code == 200 and "Seed Co" in r.text
    r = logged_in.get(f"/rights/export/{org.id}")
    assert r.status_code == 200 and r.json()["organization"]["domain"] == "seed.example"
    r = logged_in.post("/rights/erase", data={"organization_id": org.id, "reason": "request"}, follow_redirects=True)
    assert r.status_code == 200
    session.expire_all()
    assert session.get(Organization, org.id).status == "anonymised"
    actions = [a.action for a in session.scalars(select(ActivityLog))]
    assert {"rights.lookup", "rights.export", "organization.anonymise", "suppression.add"} <= set(actions)


def test_roles_viewer_cannot_mutate(client, session):
    session.add(User(email="viewer@test.example", name="V", role="viewer", password_hash=hash_password("viewer-password-123")))
    session.commit()
    r = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r = client.post("/login", data={"email": "viewer@test.example", "password": "viewer-password-123", "csrf_token": token, "next": "/"}, follow_redirects=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', r.text).group(1)
    org = seed_org(session)
    r = client.post(f"/organizations/{org.id}/status", data={"status": "reviewing", "csrf_token": token})
    assert r.status_code == 403
    assert client.get("/admin/audit").status_code == 403
    assert client.get("/organizations").status_code == 200


def test_json_api_with_token(client, session, settings, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "secret-token")
    org = seed_org(session)
    assert client.get("/api/v1/organizations").status_code == 401
    r = client.get("/api/v1/organizations?min_score=50", headers={"X-API-Key": "secret-token"})
    assert r.status_code == 200 and r.json()["items"][0]["domain"] == "seed.example"
    r = client.get(f"/api/v1/organizations/{org.id}", headers={"X-API-Key": "secret-token"})
    assert r.json()["evidence"][0]["source"] == "own_website"
    r = client.post("/api/v1/runs", json={"query": "bakery", "location": "Udupi", "track": "smb", "max_pages": 1}, headers={"X-API-Key": "secret-token"})
    assert r.status_code == 201
    job = session.scalar(select(Job).where(Job.type == "places_text_search"))
    assert job is not None and job.payload["run_id"] == r.json()["id"]
    r = client.get(f"/api/v1/jobs/{job.id}", headers={"X-API-Key": "secret-token"})
    assert r.json()["status"] == "queued"


def test_connector_admin_actions(logged_in, session):
    r = logged_in.post("/admin/connectors/google_places", data={"action": "disable"}, follow_redirects=True)
    assert r.status_code == 200 and "disable applied" in r.text
    r = logged_in.post("/admin/connectors/google_places", data={"action": "cap", "daily_cap": "50"}, follow_redirects=True)
    assert r.status_code == 200
    from prospector.models import ConnectorState

    state = session.scalar(select(ConnectorState).where(ConnectorState.name == "google_places"))
    session.refresh(state)
    assert state.enabled is False and state.daily_cap == 50
