"""Manual research capture (source #5): a human records what they read, one contact at most."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ...compliance import audit
from ...core.facts import Fact, apply_facts
from ...core.normalize import normalize_email
from ...core.organizations import ensure_website, resolve_organization
from ...core.scoring import score_organization
from ...jobs.tasks import enqueue_enrichment
from ...models import LAWFUL_BASES, NOTICE_STATUSES, Person, User
from ..deps import csrf_protect, current_user, flash, get_db, redirect, render, require_role

router = APIRouter(prefix="/capture", tags=["capture"])


@router.get("")
def capture_form(request: Request, user: User = Depends(current_user)):
    request.state.user = user
    return render(request, "capture.html", {"lawful_bases": LAWFUL_BASES, "notice_statuses": NOTICE_STATUSES})


@router.post("", dependencies=[Depends(csrf_protect)])
def capture(
    request: Request,
    company: str = Form(...),
    website: str = Form(""),
    reference_url: str = Form(...),
    track: str = Form("unknown"),
    city: str = Form(""),
    country: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    services: str = Form(""),
    notes: str = Form(""),
    person_name: str = Form(""),
    person_role: str = Form(""),
    person_profile_url: str = Form(""),
    person_email: str = Form(""),
    lawful_basis: str = Form(""),
    lia_reference: str = Form(""),
    notice_status: str = Form("pending"),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst")),
):
    reference_url = reference_url.strip()
    if not reference_url.startswith(("http://", "https://")):
        flash(request, "A public reference URL is required for provenance.", "error")
        return redirect("/capture")
    resolved = resolve_organization(db, website=website or None, name=company, city=city or None,
                                    country=(country or None), track=track)
    org = resolved.organization
    site = ensure_website(db, org, website) if website.strip() else None
    facts = [Fact("organization.display_name", company.strip(), "manual_research", reference_url, 0.8)]
    if website.strip():
        facts.append(Fact("organization.website_url", website.strip(), "manual_research", reference_url, 0.9))
    if city.strip():
        facts.append(Fact("organization.city", city.strip(), "manual_research", reference_url, 0.8))
    if country.strip():
        facts.append(Fact("organization.country", country.strip()[:2].upper(), "manual_research", reference_url, 0.8))
    if phone.strip():
        facts.append(Fact("organization.phone", phone.strip(), "manual_research", reference_url, 0.8))
    if email.strip() and normalize_email(email):
        facts.append(Fact("organization.email", email.strip(), "manual_research", reference_url, 0.8))
    if track in {"smb", "agency"}:
        facts.append(Fact("organization.track", track, "manual_research", reference_url, 1.0))
    if "linkedin.com/company" in reference_url:
        facts.append(Fact("organization.social", f"linkedin={reference_url}", "manual_research", reference_url, 1.0))
    apply_facts(db, org, facts, site)
    extra = "\n".join(s for s in (f"Services: {services.strip()}" if services.strip() else "", notes.strip()) if s)
    if extra:
        org.notes = f"{org.notes or ''}\n[{user.email}] {extra}".strip()

    if person_name.strip():
        if lawful_basis not in LAWFUL_BASES:
            flash(request, "Organisation saved; the contact was not: choose a lawful basis.", "error")
            return redirect(f"/organizations/{org.id}")
        if lawful_basis == "legitimate_interest" and not lia_reference.strip():
            flash(request, "Organisation saved; the contact was not: legitimate interest needs an LIA reference.", "error")
            return redirect(f"/organizations/{org.id}")
        person = Person(
            organization=org, full_name=person_name.strip()[:200], role_title=person_role.strip() or None,
            email=normalize_email(person_email) if person_email.strip() else None,
            linkedin_url=person_profile_url.strip() or None, source="manual_research",
            lawful_basis=lawful_basis, lia_reference=lia_reference.strip() or None,
            notice_status=notice_status if notice_status in NOTICE_STATUSES else "pending", created_by=user.id,
        )
        db.add(person)
        db.flush()
        audit.log(db, "person.create", actor=user, entity_type="person", entity_id=person.id, organization_id=org.id, lawful_basis=lawful_basis)
    if site is not None:
        enqueue_enrichment(db, org.id, site.url, country=org.country, reason="capture")
    score_organization(db, org)
    audit.log(db, "capture.create", actor=user, entity_type="organization", entity_id=org.id, created=resolved.created)
    flash(request, ("Created" if resolved.created else "Updated") + f" organisation #{org.id} from manual research.", "success")
    return redirect(f"/organizations/{org.id}")
