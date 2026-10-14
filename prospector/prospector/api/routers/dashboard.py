from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import ActivityLog, ConnectorState, DiscoveryRun, Job, Organization, Person, User
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
        },
    )
