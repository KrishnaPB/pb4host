"""Thin JSON API for scripts and future integrations (session cookie or X-API-Key)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...models import Job, Organization, User
from ..deps import current_user, get_db, require_role
from .organizations import org_query
from .runs import create_run

router = APIRouter(prefix="/api/v1", tags=["api"])


def org_to_dict(org: Organization, detail: bool = False) -> dict:
    site = org.primary_website
    perf = site.latest_performance("mobile") if site else None
    data = {
        "id": org.id,
        "name": org.label,
        "domain": org.domain,
        "website_url": org.website_url,
        "city": org.city,
        "country": org.country,
        "track": org.track,
        "status": org.status,
        "score": org.score_total,
        "band": org.score_band,
        "signals": org.signals,
        "website": {
            "cms": site.cms, "cms_version": site.cms_version, "woocommerce": site.is_woocommerce,
            "page_builder": site.page_builder, "caching_layer": site.caching_layer, "cdn": site.cdn,
            "php_version": site.php_version, "host_provider": site.host_provider, "host_class": site.host_class,
            "error_state": site.error_state, "error_confirmed": site.error_confirmed_at is not None,
        } if site else None,
        "performance_mobile": {
            "lh_performance": perf.lh_performance, "lcp_ms": perf.lcp_ms, "cls": perf.cls,
            "field_lcp_p75": perf.field_lcp_p75, "field_inp_p75": perf.field_inp_p75,
        } if perf else None,
    }
    if detail:
        data["phones"] = org.phones
        data["emails"] = org.emails
        data["registry_ids"] = org.registry_ids
        data["score_breakdown"] = org.score_breakdown
        data["evidence"] = [
            {"field": e.field, "value": e.value, "source": e.source, "source_url": e.source_url,
             "confidence": e.confidence, "fetched_at": e.fetched_at.isoformat()}
            for e in org.evidence
        ]
    return data


@router.get("/organizations")
def list_orgs(
    q: str | None = None, track: str | None = None, status: str | None = None, band: str | None = None,
    min_score: int | None = Query(None, ge=0), limit: int = Query(50, le=200), offset: int = 0,
    db: Session = Depends(get_db), user: User = Depends(current_user),
):
    rows = db.scalars(org_query(db, q=q, track=track, status=status, band=band, min_score=min_score, limit=limit, offset=offset)).unique().all()
    return {"items": [org_to_dict(o) for o in rows], "limit": limit, "offset": offset}


@router.get("/organizations/{org_id}")
def get_org(org_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    org = db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404)
    return org_to_dict(org, detail=True)


class RunIn(BaseModel):
    query: str = Field(min_length=2, max_length=255)
    location: str | None = None
    track: str = "smb"
    max_pages: int = Field(2, ge=1, le=20)
    region_code: str | None = None


@router.post("/runs", status_code=201)
def post_run(body: RunIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    run = create_run(db, query=body.query, location=body.location, track=body.track, max_pages=body.max_pages,
                     region_code=body.region_code, user=user if user.id else None)
    return {"id": run.id, "status": run.status}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404)
    return {"id": job.id, "type": job.type, "status": job.status, "attempts": job.attempts, "last_error": job.last_error,
            "run_after": job.run_after.isoformat(), "finished_at": job.finished_at.isoformat() if job.finished_at else None}
