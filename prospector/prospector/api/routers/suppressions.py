"""Suppression list and rights requests (access / erasure)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...compliance.suppression import (
    add_suppression,
    anonymise_organization,
    erase_person,
    find_subjects,
)
from ...models import SUPPRESSION_KINDS, Organization, Person, Suppression, User
from ..deps import csrf_protect, current_user, flash, get_db, redirect, render, require_role

router = APIRouter(tags=["rights"])


@router.get("/suppressions")
def suppressions(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    rows = db.scalars(select(Suppression).order_by(Suppression.id.desc()).limit(200)).all()
    return render(request, "suppressions.html", {"rows": rows, "kinds": SUPPRESSION_KINDS, "matches": None, "query": ""})


@router.post("/suppressions", dependencies=[Depends(csrf_protect)])
def add(request: Request, kind: str = Form(...), value: str = Form(...), reason: str = Form(""),
        db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    if kind not in SUPPRESSION_KINDS:
        raise HTTPException(400)
    try:
        add_suppression(db, kind, value, reason=reason.strip() or "requested", requested_by=user.email, actor=user)
    except ValueError as exc:
        flash(request, str(exc), "error")
        return redirect("/suppressions")
    flash(request, "Added to the do-not-contact list (stored as a salted hash).", "success")
    return redirect("/suppressions")


@router.post("/rights/lookup", dependencies=[Depends(csrf_protect)])
def lookup(request: Request, query: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    request.state.user = user
    matches = find_subjects(db, query)
    audit.log(db, "rights.lookup", actor=user, organizations=len(matches.organizations), persons=len(matches.persons))
    rows = db.scalars(select(Suppression).order_by(Suppression.id.desc()).limit(200)).all()
    return render(request, "suppressions.html", {"rows": rows, "kinds": SUPPRESSION_KINDS, "matches": matches, "query": query})


@router.get("/rights/export/{org_id}")
def export_subject(org_id: int, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    """Access request: everything held about an organisation and its contacts, as JSON."""
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404)
    audit.log(db, "rights.export", actor=user, entity_type="organization", entity_id=org.id)
    data = {
        "organization": {c: getattr(org, c) for c in ("id", "display_name", "legal_name", "domain", "website_url", "country",
                                                       "region", "city", "address_text", "phones", "emails", "social_links",
                                                       "registry_ids", "track", "status", "score_total", "notes")},
        "websites": [{c: getattr(s, c) for c in ("url", "final_url", "cms", "cms_version", "host_provider", "error_state")} for s in org.websites],
        "persons": [{c: getattr(p, c) for c in ("full_name", "role_title", "email", "phone_e164", "linkedin_url", "source",
                                                "lawful_basis", "notice_status", "do_not_contact")} for p in org.persons],
        "evidence": [{"field": e.field, "value": e.value, "source": e.source, "source_url": e.source_url,
                      "fetched_at": e.fetched_at.isoformat()} for e in org.evidence],
    }
    return JSONResponse(data)


@router.post("/rights/erase", dependencies=[Depends(csrf_protect)])
def erase(request: Request, organization_id: int = Form(0), person_id: int = Form(0), reason: str = Form("erasure request"),
          db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    if person_id:
        person = db.get(Person, person_id)
        if person is None:
            raise HTTPException(404)
        erase_person(db, person, reason=reason, actor=user)
        flash(request, "Contact erased and suppressed.", "success")
    elif organization_id:
        org = db.get(Organization, organization_id)
        if org is None:
            raise HTTPException(404)
        anonymise_organization(db, org, reason=reason, actor=user)
        flash(request, "Organisation anonymised; identifiers suppressed.", "success")
    return redirect("/suppressions")
