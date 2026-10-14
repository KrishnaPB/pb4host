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


def create_run(db: Session, *, query: str, location: str | None, track: str, max_pages: int, region_code: str | None, user: User | None,
               provider: str = "google_places", feed: dict | None = None) -> DiscoveryRun:
    settings = get_settings()
    if provider == "github_orgs":
        pages_cap, job, extra = settings.github_pages_per_run, "github_org_search", {"page_no": 1}
    elif provider == "httparchive_feed":
        pages_cap, job, extra = 1, "httparchive_feed_run", dict(feed or {})
    else:
        provider, pages_cap, job, extra = "google_places", settings.places_pages_per_run, "places_text_search", {"page_no": 1}
    run = DiscoveryRun(
        track=track if track in {"smb", "agency"} else "smb",
        provider=provider,
        query=query.strip(),
        location=(location or "").strip() or None,
        region_code=(region_code or settings.places_region_code or None),
        max_pages=max(1, min(int(max_pages or 1), pages_cap)),
        started_by=user.id if user and user.id else None,
    )
    db.add(run)
    db.flush()
    enqueue(db, job, {"run_id": run.id, **extra}, idempotency_key=f"run:{run.id}:page:1", scrub_on_done=provider == "google_places")
    audit.log(db, "run.create", actor=user, entity_type="discovery_run", entity_id=run.id, provider=provider, query=run.query,
              location=run.location, track=run.track)
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


@router.post("/github", dependencies=[Depends(csrf_protect)])
def new_github_run(request: Request, location: str = Form(...), query: str = Form(""), max_pages: int = Form(1),
                   db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    if not location.strip():
        flash(request, "Location is required for GitHub organisation discovery.", "error")
        return redirect("/runs")
    run = create_run(db, query=query, location=location, track="agency", max_pages=max_pages, region_code=None, user=user, provider="github_orgs")
    flash(request, f"GitHub run #{run.id} queued (organisations in {run.location}).", "success")
    return redirect(f"/runs/{run.id}")


@router.post("/httparchive", dependencies=[Depends(csrf_protect)])
def new_httparchive_run(request: Request, month: str = Form(...), country: str = Form(""), limit: int = Form(0),
                        db: Session = Depends(get_db), user: User = Depends(require_role("owner"))):
    settings = get_settings()
    country = (country or settings.httparchive_default_country).lower().strip()
    limit = int(limit or settings.httparchive_default_limit)
    run = create_run(db, query=f"{country} {month.strip()}", location=None, track="smb", max_pages=1, region_code=country.upper(), user=user,
                     provider="httparchive_feed", feed={"month": month.strip(), "country": country, "limit": limit})
    flash(request, f"HTTP Archive feed run #{run.id} queued for {month} / {country}; it fails closed above the BigQuery byte budget.", "success")
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
