from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from ...compliance import audit
from ...compliance.suppression import suppress_organization
from ...config import get_settings
from ...connectors.base import ConnectorError
from ...core.dedupe import merge_organizations
from ...core.facts import Fact, apply_facts, evidence_coverage
from ...core.normalize import normalize_email, normalize_phone
from ...core.scoring import score_organization
from ...jobs.tasks import connector, enqueue_crm_sync, enqueue_enrichment
from ...models import LAWFUL_BASES, NOTICE_CHANNELS, NOTICE_STATUSES, ORG_STATUSES, Organization, Person, User
from ...util import utcnow
from ..deps import csrf_protect, current_user, flash, get_db, redirect, render, require_role

router = APIRouter(tags=["organizations"])

MANUAL_STATUSES = ("new", "reviewing", "qualified", "exported", "contacted", "customer", "disqualified")


def org_query(
    db: Session,
    *,
    q: str | None = None,
    track: str | None = None,
    status: str | None = None,
    band: str | None = None,
    min_score: int | None = None,
    cms: str | None = None,
    error_state: str | None = None,
    host_class: str | None = None,
    lead_source: str | None = None,
    known: str | None = None,
    tracked: str | None = None,
    sort: str = "score",
    limit: int = 100,
    offset: int = 0,
):
    from ...models import Website

    stmt = select(Organization).options(selectinload(Organization.websites), selectinload(Organization.place_links))
    stmt = stmt.where(Organization.status.notin_(("merged",)))
    if status:
        stmt = stmt.where(Organization.status == status)
    else:
        stmt = stmt.where(Organization.status.notin_(("anonymised", "suppressed")))
    if track:
        stmt = stmt.where(Organization.track == track)
    if band:
        stmt = stmt.where(Organization.score_band == band)
    if min_score:
        stmt = stmt.where(Organization.score_total >= min_score)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Organization.display_name.ilike(like), Organization.domain.ilike(like), Organization.city.ilike(like)))
    if cms or error_state or host_class:
        stmt = stmt.join(Website, Website.organization_id == Organization.id)
        if cms:
            stmt = stmt.where(Website.cms == cms) if cms != "none" else stmt.where(Website.cms.is_(None))
        if error_state:
            stmt = stmt.where(Website.error_state == error_state) if error_state != "any" else stmt.where(Website.error_state != "none")
        if host_class:
            stmt = stmt.where(Website.host_class == host_class)
    if lead_source:
        stmt = stmt.where(Organization.lead_source == lead_source)
    if known == "yes":
        stmt = stmt.where(exists().where(Person.organization_id == Organization.id, Person.source == "linkedin_connections"))
    if tracked == "yes":
        stmt = stmt.where(Organization.tracked.is_(True))
    if sort == "inbound":
        stmt = stmt.order_by(Organization.inbound_at.desc().nulls_last(), Organization.id.desc())
    elif sort == "newest":
        stmt = stmt.order_by(Organization.id.desc())
    elif sort == "name":
        stmt = stmt.order_by(Organization.display_name.asc().nulls_last(), Organization.domain.asc())
    else:
        stmt = stmt.order_by(Organization.score_total.desc(), Organization.id.desc())
    return stmt.limit(limit).offset(offset)


@router.get("/organizations")
def list_organizations(
    request: Request,
    q: str | None = None,
    track: str | None = None,
    status: str | None = None,
    band: str | None = None,
    min_score: int | None = Query(None, ge=0),
    cms: str | None = None,
    error_state: str | None = None,
    host_class: str | None = None,
    lead_source: str | None = None,
    known: str | None = None,
    tracked: str | None = None,
    sort: str = "score",
    page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    request.state.user = user
    per_page = 50
    stmt = org_query(db, q=q, track=track, status=status, band=band, min_score=min_score, cms=cms,
                     error_state=error_state, host_class=host_class, lead_source=lead_source, known=known, tracked=tracked,
                     sort=sort, limit=per_page + 1, offset=(page - 1) * per_page)
    rows = db.scalars(stmt).unique().all()
    has_next = len(rows) > per_page
    filters = {"q": q or "", "track": track or "", "status": status or "", "band": band or "",
               "min_score": min_score or "", "cms": cms or "", "error_state": error_state or "",
               "host_class": host_class or "", "lead_source": lead_source or "", "known": known or "", "tracked": tracked or "", "sort": sort}
    return render(
        request,
        "organizations/list.html",
        {
            "orgs": rows[:per_page],
            "filters": filters,
            "qs": urlencode({k: v for k, v in filters.items() if v != ""}),
            "page": page,
            "has_next": has_next,
            "statuses": ORG_STATUSES,
        },
    )


def _load(db: Session, org_id: int) -> Organization:
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, "organisation not found")
    return org


@router.get("/organizations/{org_id}")
def organization_detail(org_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    org = _load(db, org_id)
    if org.persons:
        audit.log(db, "person.view", actor=user, entity_type="organization", entity_id=org.id, persons=len(org.persons))
    site = org.primary_website
    evidence = sorted(org.evidence, key=lambda e: (e.field, -e.id))
    coverage_gaps = evidence_coverage(db, org)
    settings = get_settings()
    crm_link = next((link for link in org.crm_links if link.entity_type == "organization"), None)
    from ...crm.sync import eligibility

    crm_ok, crm_reason = eligibility(db, org)
    return render(
        request,
        "organizations/detail.html",
        {
            "org": org,
            "site": site,
            "perf_mobile": site.latest_performance("mobile") if site else None,
            "perf_desktop": site.latest_performance("desktop") if site else None,
            "field_mobile": site.latest_field_performance("mobile") if site else None,
            "evidence": evidence,
            "coverage_gaps": coverage_gaps,
            "manual_statuses": MANUAL_STATUSES,
            "lawful_bases": LAWFUL_BASES,
            "notice_statuses": NOTICE_STATUSES,
            "notice_channels": NOTICE_CHANNELS,
            "merged_from": db.scalars(select(Organization).where(Organization.merged_into_id == org.id)).all(),
            "crm_link": crm_link,
            "crm_configured": settings.crm_configured,
            "crm_target": settings.crm_target,
            "crm_ok": crm_ok,
            "crm_reason": crm_reason,
            "companies_house_available": bool(settings.companies_house_api_key),
            "notice_events": sorted(org.notice_events, key=lambda e: e.sent_at, reverse=True)[:10],
        },
    )


@router.get("/organizations/{org_id}/places-live")
def places_live(org_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """htmx partial: live Google lookup with attribution. Nothing from it is persisted."""
    org = _load(db, org_id)
    if not org.place_links:
        return render(request, "partials/places_live.html", {"details": None, "error": "No Google place linked."})
    try:
        details = connector("google_places").live_details(db, org.place_links[0].place_id)
        error = None
    except ConnectorError as exc:
        details, error = None, str(exc)
    db.rollback()  # belt and braces: the lookup must not leave anything to commit
    return render(request, "partials/places_live.html", {"details": details, "error": error})


@router.post("/organizations/{org_id}/status", dependencies=[Depends(csrf_protect)])
def set_status(org_id: int, request: Request, status: str = Form(...), note: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    if status not in MANUAL_STATUSES:
        raise HTTPException(400, "invalid status")
    before = org.status
    org.status = status
    settings = get_settings()
    if status in {"reviewing", "qualified", "contacted", "customer", "exported"}:
        org.last_engaged_at = utcnow()
    if status in {"qualified", "exported", "contacted", "customer"} and not org.tracked:
        org.tracked = True  # weekly light re-check from now on
        org.next_recheck_at = utcnow() + timedelta(days=settings.recheck_interval_days)
    if status in {"qualified", "contacted", "customer"} and settings.crm_configured and before != status:
        enqueue_crm_sync(db, org.id, reason=f"status:{status}")
    if status == "disqualified":
        org.disqualify_reason = note.strip() or "manual"
        org.tracked = False
    if note.strip():
        stamp = utcnow().strftime("%Y-%m-%d %H:%M")
        org.notes = f"{org.notes or ''}\n[{stamp} {user.email}] {note.strip()}".strip()
    audit.log(db, "organization.status", actor=user, entity_type="organization", entity_id=org.id, before=before, after=status)
    flash(request, f"Status set to {status}.", "success")
    return redirect(f"/organizations/{org.id}")


@router.post("/organizations/{org_id}/track", dependencies=[Depends(csrf_protect)])
def set_track(org_id: int, request: Request, track: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    if track not in {"smb", "agency", "unknown"}:
        raise HTTPException(400, "invalid track")
    apply_facts(db, org, [Fact("organization.track", track, "manual_research", f"manual://user/{user.id}", 1.0)], org.primary_website)
    score_organization(db, org)
    audit.log(db, "organization.track", actor=user, entity_type="organization", entity_id=org.id, track=track)
    return redirect(f"/organizations/{org.id}")


@router.post("/organizations/{org_id}/enrich", dependencies=[Depends(csrf_protect)])
def enrich(org_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    url = org.website_url or (org.primary_website.url if org.primary_website else None)
    if not url:
        flash(request, "No website URL to enrich.", "error")
        return redirect(f"/organizations/{org.id}")
    enqueue_enrichment(db, org.id, url, country=org.country, reason=f"manual{utcnow():%H%M%S}")
    audit.log(db, "organization.enrich", actor=user, entity_type="organization", entity_id=org.id)
    flash(request, "Re-enrichment queued.", "success")
    return redirect(f"/organizations/{org.id}")


@router.post("/organizations/{org_id}/rescore", dependencies=[Depends(csrf_protect)])
def rescore(org_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    result = score_organization(db, org)
    flash(request, f"Re-scored: {result.total} ({result.band}).", "success")
    return redirect(f"/organizations/{org.id}")


@router.post("/organizations/{org_id}/merge", dependencies=[Depends(csrf_protect)])
def merge(org_id: int, request: Request, duplicate_id: int = Form(...), db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    primary = _load(db, org_id)
    duplicate = _load(db, duplicate_id)
    merge_organizations(db, primary, duplicate)
    score_organization(db, primary)
    audit.log(db, "organization.merge", actor=user, entity_type="organization", entity_id=primary.id, duplicate_id=duplicate.id)
    flash(request, f"Merged #{duplicate.id} into #{primary.id}.", "success")
    return redirect(f"/organizations/{primary.id}")


@router.post("/organizations/{org_id}/suppress", dependencies=[Depends(csrf_protect)])
def suppress(org_id: int, request: Request, reason: str = Form("requested"), db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    suppress_organization(db, org, reason=reason.strip() or "requested", actor=user)
    flash(request, "Organisation suppressed; its identifiers were added to the do-not-contact list.", "success")
    return redirect(f"/organizations/{org.id}")


@router.post("/organizations/{org_id}/facts", dependencies=[Depends(csrf_protect)])
def add_fact(org_id: int, request: Request, field: str = Form(...), value: str = Form(...), source_url: str = Form(...),
             db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    """Manual correction/addition: requires a reference URL so provenance stays complete (R1)."""
    org = _load(db, org_id)
    allowed = {"organization.display_name", "organization.legal_name", "organization.phone", "organization.email",
               "organization.address_text", "organization.city", "organization.region", "organization.country",
               "organization.registry.gstin", "organization.registry.cin", "organization.registry.company_number",
               "organization.website_url"}
    if field not in allowed:
        raise HTTPException(400, "field not editable here")
    if not source_url.strip():
        raise HTTPException(400, "reference URL required")
    apply_facts(db, org, [Fact(field, value.strip(), "manual_research", source_url.strip(), 0.8)], org.primary_website)
    audit.log(db, "organization.fact", actor=user, entity_type="organization", entity_id=org.id, field=field)
    flash(request, f"Recorded {field}.", "success")
    return redirect(f"/organizations/{org.id}")


@router.post("/organizations/{org_id}/persons", dependencies=[Depends(csrf_protect)])
def add_person(
    org_id: int,
    request: Request,
    full_name: str = Form(...),
    role_title: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    linkedin_url: str = Form(""),
    lawful_basis: str = Form(...),
    lia_reference: str = Form(""),
    notice_status: str = Form("pending"),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst")),
):
    org = _load(db, org_id)
    if lawful_basis not in LAWFUL_BASES or notice_status not in NOTICE_STATUSES:
        raise HTTPException(400, "invalid lawful basis or notice status")
    if lawful_basis == "legitimate_interest" and not lia_reference.strip():
        flash(request, "Legitimate interest requires a reference to the documented LIA.", "error")
        return redirect(f"/organizations/{org.id}")
    person = Person(
        organization=org,
        full_name=full_name.strip()[:200],
        role_title=role_title.strip() or None,
        email=normalize_email(email) if email.strip() else None,
        phone_e164=normalize_phone(phone, org.country or "IN") if phone.strip() else None,
        linkedin_url=linkedin_url.strip() or None,
        source="manual_research",
        lawful_basis=lawful_basis,
        lia_reference=lia_reference.strip() or None,
        notice_status=notice_status,
        created_by=user.id,
    )
    db.add(person)
    db.flush()
    audit.log(db, "person.create", actor=user, entity_type="person", entity_id=person.id, organization_id=org.id, lawful_basis=lawful_basis)
    flash(request, "Contact recorded with lawful basis.", "success")
    return redirect(f"/organizations/{org.id}")


@router.post("/persons/{person_id}/notice", dependencies=[Depends(csrf_protect)])
def person_notice(person_id: int, request: Request, notice_status: str = Form(...), do_not_contact: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(404)
    if notice_status not in NOTICE_STATUSES:
        raise HTTPException(400)
    person.notice_status = notice_status
    if notice_status == "sent" and not person.notice_sent_at:
        person.notice_sent_at = utcnow()
    person.do_not_contact = bool(do_not_contact) or notice_status == "objected"
    audit.log(db, "person.notice", actor=user, entity_type="person", entity_id=person.id, notice_status=notice_status, dnc=person.do_not_contact)
    return redirect(f"/organizations/{person.organization_id}")


@router.post("/persons/{person_id}/delete", dependencies=[Depends(csrf_protect)])
def person_delete(person_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(404)
    org_id = person.organization_id
    audit.log(db, "person.delete", actor=user, entity_type="person", entity_id=person.id, organization_id=org_id)
    db.delete(person)
    flash(request, "Contact deleted.", "info")
    return redirect(f"/organizations/{org_id}")


# --- Phase 2: tracking, CRM, Companies House -------------------------------------------------------
@router.post("/organizations/{org_id}/tracking", dependencies=[Depends(csrf_protect)])
def set_tracking(org_id: int, request: Request, tracked: str = Form("0"), db: Session = Depends(get_db),
                 user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    org.tracked = tracked in {"1", "true", "on", "yes"}
    org.next_recheck_at = utcnow() + timedelta(days=get_settings().recheck_interval_days) if org.tracked else None
    audit.log(db, "organization.tracking", actor=user, entity_type="organization", entity_id=org.id, tracked=org.tracked)
    flash(request, "Weekly re-check " + ("enabled." if org.tracked else "disabled."), "success")
    return redirect(f"/organizations/{org.id}")


@router.post("/organizations/{org_id}/crm/push", dependencies=[Depends(csrf_protect)])
def crm_push(org_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    settings = get_settings()
    if not settings.crm_configured:
        flash(request, "CRM is not configured (ESPOCRM_BASE_URL / ESPOCRM_API_KEY).", "error")
        return redirect(f"/organizations/{org.id}")
    from ...crm.sync import eligibility

    ok, reason = eligibility(db, org)
    if not ok:
        flash(request, f"Not pushed: {reason}.", "error")
        return redirect(f"/organizations/{org.id}")
    enqueue_crm_sync(db, org.id, reason="manual", force=True)
    audit.log(db, "crm.push_requested", actor=user, entity_type="organization", entity_id=org.id)
    flash(request, "Queued for the CRM; the worker pushes it within a few seconds.", "success")
    return redirect(f"/organizations/{org.id}")


@router.get("/organizations/{org_id}/registry/search")
def registry_search(org_id: int, request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    """htmx partial: Companies House search; nothing stored until the analyst applies a result."""
    org = _load(db, org_id)
    query = (q or org.legal_name or org.display_name or "").strip()
    hits, error = [], None
    if query:
        try:
            hits = connector("companies_house").search(db, query)
        except ConnectorError as exc:
            error = str(exc)
    audit.log(db, "registry.search", actor=user, entity_type="organization", entity_id=org.id, query=query, hits=len(hits))
    return render(request, "partials/registry_results.html", {"org": org, "hits": hits, "query": query, "error": error})


@router.post("/organizations/{org_id}/registry/apply", dependencies=[Depends(csrf_protect)])
def registry_apply(org_id: int, request: Request, company_number: str = Form(...), db: Session = Depends(get_db),
                   user: User = Depends(require_role("analyst"))):
    org = _load(db, org_id)
    profile = connector("companies_house").profile(db, company_number)
    if profile is None:
        flash(request, "No company with that number.", "error")
        return redirect(f"/organizations/{org.id}")
    ref = profile.url
    facts = [
        Fact("organization.legal_name", profile.name, "companies_house", ref, 1.0),
        Fact("organization.registry.company_number", profile.company_number, "companies_house", ref, 1.0),
        Fact("organization.country", "GB", "companies_house", ref, 0.9),
    ]
    if profile.address:
        facts.append(Fact("organization.address_text", profile.address, "companies_house", ref, 0.9))
    if profile.postal_code:
        facts.append(Fact("organization.postal_code", profile.postal_code, "companies_house", ref, 0.9))
    if profile.locality and not org.city:
        facts.append(Fact("organization.city", profile.locality, "companies_house", ref, 0.8))
    apply_facts(db, org, facts)
    stamp = utcnow().strftime("%Y-%m-%d")
    org.notes = f"{org.notes or ''}\n[{stamp} {user.email}] Companies House {profile.company_number}: {profile.status or '?'}, " \
                f"incorporated {profile.created or '?'}, SIC {', '.join(profile.sic_codes) or '—'}".strip()
    audit.log(db, "registry.apply", actor=user, entity_type="organization", entity_id=org.id, company_number=profile.company_number)
    flash(request, f"Applied Companies House facts for {profile.name} ({profile.company_number}).", "success")
    return redirect(f"/organizations/{org.id}")


@router.get("/organizations/{org_id}/registry/officers/{company_number}")
def registry_officers(org_id: int, company_number: str, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_role("analyst"))):
    """Live officers view (R2: displayed, audited, never stored)."""
    org = _load(db, org_id)
    try:
        officers = connector("companies_house").officers_live(db, company_number)
        error = None
    except ConnectorError as exc:
        officers, error = [], str(exc)
    audit.log(db, "registry.officers_view", actor=user, entity_type="organization", entity_id=org.id, company_number=company_number)
    return render(request, "partials/registry_officers.html", {"org": org, "officers": officers, "error": error, "company_number": company_number})
