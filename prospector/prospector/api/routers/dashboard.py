from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import ActivityLog, ConnectorState, CrmLink, DiscoveryRun, InboundSubmission, Job, Organization, Person, RightsRequest, User
from ...util import utcnow
from ..deps import current_user, get_db, render

router = APIRouter(tags=["dashboard"])


def counts_by(db: Session, column, filters=()):
    stmt = select(column, func.count()).group_by(column)
    for f in filters:
        stmt = stmt.where(f)
    return {k or "—": v for k, v in db.execute(stmt).all()}


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    by_status = counts_by(db, Organization.status)
    by_band = counts_by(db, Organization.score_band, [Organization.status.in_(("new", "reviewing", "qualified"))])
    by_track = counts_by(db, Organization.track, [Organization.status.notin_(("merged", "anonymised"))])
    jobs = counts_by(db, Job.status)
    recent_runs = db.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(5)).all()
    connectors = db.scalars(select(ConnectorState).order_by(ConnectorState.name)).all()
    persons = db.scalar(select(func.count()).select_from(Person)) or 0
    last_retention = db.scalar(
        select(ActivityLog).where(ActivityLog.action == "retention.run").order_by(ActivityLog.id.desc()).limit(1)
    )
    top = db.scalars(
        select(Organization)
        .where(Organization.status.in_(("new", "reviewing")))
        .order_by(Organization.score_total.desc(), Organization.id.desc())
        .limit(8)
    ).all()
    now = utcnow()
    open_rights = db.scalars(select(RightsRequest).where(RightsRequest.status.in_(("open", "in_progress"))).order_by(RightsRequest.due_at)).all()
    phase2 = {
        "inbound_7d": db.scalar(select(func.count(InboundSubmission.id)).where(InboundSubmission.received_at >= now - __import__("datetime").timedelta(days=7))) or 0,
        "inbound_open": db.scalar(select(func.count(Organization.id)).where(Organization.lead_source == "inbound", Organization.status.in_(("new", "reviewing")))) or 0,
        "known": db.scalar(select(func.count(Person.id)).where(Person.source == "linkedin_connections", Person.organization_id.is_not(None))) or 0,
        "tracked": db.scalar(select(func.count(Organization.id)).where(Organization.tracked.is_(True))) or 0,
        "recheck_due": db.scalar(select(func.count(Organization.id)).where(Organization.tracked.is_(True), Organization.next_recheck_at <= now)) or 0,
        "crm_linked": db.scalar(select(func.count(CrmLink.id)).where(CrmLink.entity_type == "organization")) or 0,
        "rights_open": len(open_rights),
        "rights_overdue": sum(1 for r in open_rights if r.due_at and r.due_at <= now),
        "rights_next_due": open_rights[0].due_at if open_rights else None,
    }
    return render(
        request,
        "dashboard.html",
        {
            "by_status": by_status,
            "by_band": by_band,
            "by_track": by_track,
            "jobs": jobs,
            "recent_runs": recent_runs,
            "connectors": connectors,
            "persons": persons,
            "last_retention": last_retention,
            "top": top,
            "phase2": phase2,
        },
    )
