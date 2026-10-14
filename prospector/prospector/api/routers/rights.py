"""Rights-request workflow, notice tracking and known contacts (Phase 2)."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...compliance.rights import fulfil_erasure, fulfil_objection
from ...compliance.suppression import find_subjects
from ...config import get_settings
from ...models import (
    NOTICE_CHANNELS,
    RIGHTS_KINDS,
    RIGHTS_STATUSES,
    NoticeEvent,
    Organization,
    Person,
    RightsRequest,
    User,
)
from ...util import utcnow
from ..deps import csrf_protect, current_user, flash, get_db, redirect, render, require_role
from .exports import notice_text

router = APIRouter(tags=["rights"])


# --- rights requests ------------------------------------------------------------------------------
def open_requests(db: Session) -> list[RightsRequest]:
    return db.scalars(select(RightsRequest).where(RightsRequest.status.in_(("open", "in_progress"))).order_by(RightsRequest.due_at)).all()


@router.get("/rights/requests")
def list_requests(request: Request, status: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    stmt = select(RightsRequest).order_by(RightsRequest.status.in_(("completed", "rejected")), RightsRequest.due_at)
    if status in RIGHTS_STATUSES:
        stmt = stmt.where(RightsRequest.status == status)
    rows = db.scalars(stmt.limit(200)).all()
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.email)).all()
    return render(request, "rights/requests.html", {"rows": rows, "kinds": RIGHTS_KINDS, "statuses": RIGHTS_STATUSES, "users": users,
                                                    "filter_status": status, "now": utcnow(), "due_days": get_settings().rights_due_days})


@router.post("/rights/requests", dependencies=[Depends(csrf_protect)])
def create_request(request: Request, kind: str = Form(...), subject: str = Form(...), channel: str = Form("email"), details: str = Form(""),
                   received_at: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    if kind not in RIGHTS_KINDS or not subject.strip():
        raise HTTPException(400, "kind and subject are required")
    settings = get_settings()
    received = utcnow()
    if received_at.strip():
        try:
            received = __import__("datetime").datetime.fromisoformat(received_at.strip())
        except ValueError as exc:
            raise HTTPException(400, "received_at must be ISO-8601") from exc
    matches = find_subjects(db, subject.strip())
    row = RightsRequest(kind=kind, subject=subject.strip()[:255], channel=channel if channel in NOTICE_CHANNELS else "email",
                        details=details.strip() or None, received_at=received, due_at=received + timedelta(days=settings.rights_due_days),
                        organization_id=matches.organizations[0].id if matches.organizations else None,
                        person_id=matches.persons[0].id if matches.persons else None, assigned_to=user.id, created_by=user.id)
    db.add(row)
    db.flush()
    audit.log(db, "rights.request_open", actor=user, entity_type="rights_request", entity_id=row.id, kind=kind,
              organizations=len(matches.organizations), persons=len(matches.persons))
    flash(request, f"Request #{row.id} opened; due {row.due_at:%Y-%m-%d}. Matched {len(matches.organizations)} organisation(s) and "
                   f"{len(matches.persons)} contact(s).", "success")
    return redirect(f"/rights/requests/{row.id}")


@router.get("/rights/requests/{req_id}")
def request_detail(req_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    row = db.get(RightsRequest, req_id)
    if row is None:
        raise HTTPException(404)
    matches = find_subjects(db, row.subject)
    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.email)).all()
    return render(request, "rights/request_detail.html", {"req": row, "matches": matches, "statuses": RIGHTS_STATUSES, "users": users,
                                                          "now": utcnow()})


@router.post("/rights/requests/{req_id}", dependencies=[Depends(csrf_protect)])
def update_request(req_id: int, request: Request, status: str = Form(...), resolution: str = Form(""), assigned_to: int = Form(0),
                   db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    row = db.get(RightsRequest, req_id)
    if row is None:
        raise HTTPException(404)
    if status not in RIGHTS_STATUSES:
        raise HTTPException(400, "invalid status")
    if status in {"completed", "rejected"} and not resolution.strip() and not row.resolution:
        flash(request, "Record what was done (resolution) before closing a request.", "error")
        return redirect(f"/rights/requests/{row.id}")
    before = row.status
    row.status = status
    if resolution.strip():
        stamp = utcnow().strftime("%Y-%m-%d %H:%M")
        row.resolution = f"{row.resolution or ''}\n[{stamp} {user.email}] {resolution.strip()}".strip()
    if assigned_to:
        row.assigned_to = assigned_to
    if status in {"completed", "rejected"}:
        row.completed_at = utcnow()
        row.completed_by = user.id
    audit.log(db, "rights.request_update", actor=user, entity_type="rights_request", entity_id=row.id, before=before, after=status)
    flash(request, f"Request #{row.id} is now {status}.", "success")
    return redirect(f"/rights/requests/{row.id}")


@router.post("/rights/requests/{req_id}/fulfil", dependencies=[Depends(csrf_protect)])
def fulfil_request(req_id: int, request: Request, action: str = Form(...), include_org: str = Form("0"), db: Session = Depends(get_db),
                   user: User = Depends(require_role("owner"))):
    """One-click fulfilment: erase (person + organisation history) or stop-contact; closes the request."""
    row = db.get(RightsRequest, req_id)
    if row is None:
        raise HTTPException(404)
    if row.status in {"completed", "rejected"}:
        flash(request, "Request already closed.", "error")
        return redirect(f"/rights/requests/{row.id}")
    if action == "erase":
        summary = fulfil_erasure(db, row, actor=user, include_person_orgs=include_org in {"1", "on", "true", "yes"})
    elif action == "stop_contact":
        summary = fulfil_objection(db, row, actor=user)
    else:
        raise HTTPException(400, "unknown action")
    flash(request, f"Request #{row.id} fulfilled: {summary.text()}.", "success")
    return redirect(f"/rights/requests/{row.id}")


# --- notice tracking ------------------------------------------------------------------------------
@router.post("/organizations/{org_id}/notice", dependencies=[Depends(csrf_protect)])
def record_notice(org_id: int, request: Request, channel: str = Form(...), person_id: int = Form(0), notes: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404)
    if channel not in NOTICE_CHANNELS:
        raise HTTPException(400, "invalid channel")
    settings = get_settings()
    text = notice_text(org)
    event = NoticeEvent(organization=org, person_id=person_id or None, channel=channel, template_version=settings.inbound_consent_version,
                        text_hash=hashlib.sha256(text.encode()).hexdigest(), sent_by=user.id, notes=notes.strip() or None)
    db.add(event)
    now = utcnow()
    org.notice_sent_at = org.notice_sent_at or now
    org.last_engaged_at = now
    targets = [p for p in org.persons if (not person_id or p.id == person_id)]
    for person in targets:
        if person.notice_status in {"pending", "not_required"}:
            person.notice_status = "sent"
            person.notice_sent_at = person.notice_sent_at or now
    db.flush()
    audit.log(db, "notice.sent", actor=user, entity_type="organization", entity_id=org.id, channel=channel, persons=len(targets))
    flash(request, f"Notice recorded ({channel}); {len(targets)} contact(s) marked as notified.", "success")
    return redirect(f"/organizations/{org.id}")


@router.get("/organizations/{org_id}/notice-text")
def notice_preview(org_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404)
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(notice_text(org))


# --- people you know -------------------------------------------------------------------------------
@router.get("/people")
def people(request: Request, linked: str | None = None, q: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    stmt = select(Person).where(Person.source == "linkedin_connections")
    if linked == "yes":
        stmt = stmt.where(Person.organization_id.is_not(None))
    elif linked == "no":
        stmt = stmt.where(Person.organization_id.is_(None))
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(Person.full_name.ilike(like) | Person.role_title.ilike(like))
    rows = db.scalars(stmt.order_by(Person.organization_id.is_(None), Person.full_name).limit(300)).all()
    if rows:
        audit.log(db, "person.view", actor=user, entity_type="people", persons=len(rows))
    counts = {
        "total": db.scalar(select(func.count(Person.id)).where(Person.source == "linkedin_connections")) or 0,
        "linked": db.scalar(select(func.count(Person.id)).where(Person.source == "linkedin_connections", Person.organization_id.is_not(None))) or 0,
    }
    # organisations with a warm/hot score and a known contact: the "people you already know" list
    warm = [p for p in rows if p.organization is not None and (p.organization.score_band in {"hot", "warm"})]
    return render(request, "people.html", {"rows": rows, "counts": counts, "linked": linked, "q": q or "", "warm": warm})


@router.post("/people/{person_id}/link", dependencies=[Depends(csrf_protect)])
def link_person(person_id: int, request: Request, organization_id: int = Form(...), db: Session = Depends(get_db),
                user: User = Depends(require_role("owner"))):
    person = db.get(Person, person_id)
    org = db.get(Organization, organization_id)
    if person is None or org is None:
        raise HTTPException(404)
    person.organization = org
    audit.log(db, "person.link", actor=user, entity_type="person", entity_id=person.id, organization_id=org.id)
    flash(request, f"Linked {person.full_name} to organisation #{org.id}.", "success")
    return redirect("/people")
