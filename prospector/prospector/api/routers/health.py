from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ...models import ConnectorState, Job, Organization
from ..deps import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    queued = db.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")) or 0
    return {"status": "ok", "queued_jobs": queued}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(db: Session = Depends(get_db)):
    lines = ["# HELP prospector_jobs Jobs by status", "# TYPE prospector_jobs gauge"]
    for status, count in db.execute(select(Job.status, func.count()).group_by(Job.status)).all():
        lines.append(f'prospector_jobs{{status="{status}"}} {count}')
    lines += ["# HELP prospector_organizations Organisations by status", "# TYPE prospector_organizations gauge"]
    for status, count in db.execute(select(Organization.status, func.count()).group_by(Organization.status)).all():
        lines.append(f'prospector_organizations{{status="{status}"}} {count}')
    lines += ["# HELP prospector_connector_calls_today Calls made today per connector", "# TYPE prospector_connector_calls_today gauge"]
    for row in db.scalars(select(ConnectorState)):
        lines.append(f'prospector_connector_calls_today{{connector="{row.name}"}} {row.calls_today}')
        lines.append(f'prospector_connector_daily_cap{{connector="{row.name}"}} {row.daily_cap}')
        lines.append(f'prospector_connector_enabled{{connector="{row.name}"}} {int(row.enabled)}')
    return "\n".join(lines) + "\n"
