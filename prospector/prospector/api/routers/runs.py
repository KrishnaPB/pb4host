from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...config import get_settings
from ...jobs.queue import enqueue
from ...models import DiscoveryRun, Job, Organization, PlaceLink, User
from ..deps import csrf_protect, current_user, flash, get_db, redirect, render, require_role

router = APIRouter(prefix="/runs", tags=["runs"])


def create_run(db: Session, *, query: str, location: str | None, track: str, max_pages: int, region_code: str | None, user: User | None) -> DiscoveryRun:
    settings = get_settings()
    run = DiscoveryRun(
        track=track if track in {"smb", "agency"} else "smb",
        provider="google_places",
        query=query.strip(),
        location=(location or "").strip() or None,
        region_code=(region_code or settings.places_region_code or None),
        max_pages=max(1, min(int(max_pages or 1), settings.places_pages_per_run)),
        started_by=user.id if user and user.id else None,
    )
    db.add(run)
    db.flush()
    enqueue(db, "places_text_search", {"run_id": run.id, "page_no": 1}, idempotency_key=f"run:{run.id}:page:1", scrub_on_done=True)
    audit.log(db, "run.create", actor=user, entity_type="discovery_run", entity_id=run.id, query=run.query, location=run.location, track=run.track)
    return run


@router.get("")
def list_runs(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    runs = db.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc()).limit(50)).all()
    return render(request, "runs/list.html", {"runs": runs})


@router.post("", dependencies=[Depends(csrf_protect)])
def new_run(
    request: Request,
    query: str = Form(...),
    location: str = Form(""),
    track: str = Form("smb"),
    max_pages: int = Form(2),
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst")),
):
    if not query.strip():
        flash(request, "Query is required.", "error")
        return redirect("/runs")
    run = create_run(db, query=query, location=location, track=track, max_pages=max_pages, region_code=None, user=user)
    flash(request, f"Run #{run.id} queued. The worker will fetch up to {run.max_pages} page(s) of results.", "success")
    return redirect(f"/runs/{run.id}")


@router.get("/{run_id}")
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    request.state.user = user
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        raise HTTPException(404)
    orgs = db.scalars(
        select(Organization)
        .join(PlaceLink, PlaceLink.organization_id == Organization.id)
        .where(PlaceLink.first_seen_run_id == run.id)
        .order_by(Organization.score_total.desc())
    ).unique().all()
    jobs = db.scalars(select(Job).where(Job.type == "places_text_search").order_by(Job.id.desc()).limit(20)).all()
    jobs = [j for j in jobs if j.payload.get("run_id") == run.id]
    return render(request, "runs/detail.html", {"run": run, "orgs": orgs, "jobs": jobs})


@router.post("/{run_id}/cancel", dependencies=[Depends(csrf_protect)])
def cancel_run(run_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    run = db.get(DiscoveryRun, run_id)
    if run is None:
        raise HTTPException(404)
    run.status = "cancelled"
    for job in db.scalars(select(Job).where(Job.type == "places_text_search", Job.status == "queued")):
        if job.payload.get("run_id") == run.id:
            job.status = "cancelled"
    audit.log(db, "run.cancel", actor=user, entity_type="discovery_run", entity_id=run.id)
    flash(request, f"Run #{run.id} cancelled.", "info")
    return redirect(f"/runs/{run.id}")
