"""CSV exports for the CRM / outreach tool (R7): suppressed records never leave the system."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...compliance import audit
from ...compliance.suppression import organization_is_suppressed
from ...config import get_settings
from ...models import Organization, Person, User
from ...util import utcnow
from ..deps import current_user, get_db, render, require_role

router = APIRouter(prefix="/exports", tags=["exports"])


def notice_text(org: Organization) -> str:
    settings = get_settings()
    sources = sorted({e.source for e in org.evidence}) or ["public sources"]
    return settings.notice_template.format(
        business_name=settings.business_name,
        organisation=org.label,
        website=org.website_url or org.domain or "",
        sources=", ".join(sources),
        contact_email=settings.notice_contact_email,
    )


def exportable_organizations(db: Session, *, status: str, track: str | None, min_score: int) -> list[Organization]:
    stmt = select(Organization).where(Organization.status == status, Organization.score_total >= min_score)
    if track:
        stmt = stmt.where(Organization.track == track)
    rows = db.scalars(stmt.order_by(Organization.score_total.desc())).all()
    return [o for o in rows if not organization_is_suppressed(db, o)]


@router.get("")
def exports_page(request: Request, user: User = Depends(current_user)):
    request.state.user = user
    return render(request, "exports.html", {})


@router.get("/organizations.csv")
def organizations_csv(
    status: str = Query("qualified"),
    track: str | None = None,
    min_score: int = Query(0, ge=0),
    mark_exported: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst")),
):
    orgs = exportable_organizations(db, status=status, track=track, min_score=min_score)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "organisation", "website", "domain", "city", "country", "track", "score", "band", "top_signals",
                     "phone", "email", "cms", "cms_version", "woocommerce", "error_state", "lh_mobile", "field_lcp_p75",
                     "host_provider", "source_summary", "evidence_url", "notice_text"])
    for org in orgs:
        site = org.primary_website
        perf = site.latest_performance("mobile") if site else None
        rules = ", ".join(r["label"] for r in (org.score_breakdown or {}).get("rules", [])[:4])
        sources = ", ".join(sorted({e.source for e in org.evidence}))
        writer.writerow([
            org.id, org.label, org.website_url or "", org.domain or "", org.city or "", org.country or "", org.track,
            org.score_total, org.score_band or "", rules, (org.phones or [""])[0], (org.emails or [""])[0],
            site.cms if site else "", site.cms_version if site else "", site.is_woocommerce if site else "",
            site.error_state if site else "", perf.lh_performance if perf else "", perf.field_lcp_p75 if perf else "",
            site.host_provider if site else "", sources, org.website_url or "", notice_text(org),
        ])
        if mark_exported and org.status == "qualified":
            org.status = "exported"
            org.last_engaged_at = utcnow()
    audit.log(db, "export.organizations", actor=user, count=len(orgs), status=status, track=track, min_score=min_score, mark_exported=mark_exported)
    buf.seek(0)
    name = f"prospector-organisations-{utcnow():%Y%m%d-%H%M}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


@router.get("/persons.csv")
def persons_csv(db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    """Only notice-ready contacts: lawful basis present (schema), not objected, not do-not-contact,
    and whose organisation is not suppressed."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["person_id", "organisation_id", "organisation", "full_name", "role", "email", "phone", "lawful_basis",
                     "notice_status", "source", "notice_text"])
    count = 0
    for person in db.scalars(select(Person).order_by(Person.id)):
        org = person.organization
        if not person.exportable or org.status in {"suppressed", "anonymised", "merged"} or organization_is_suppressed(db, org):
            continue
        writer.writerow([person.id, org.id, org.label, person.full_name, person.role_title or "", person.email or "",
                         person.phone_e164 or "", person.lawful_basis, person.notice_status, person.source, notice_text(org)])
        count += 1
    audit.log(db, "export.persons", actor=user, count=count)
    buf.seek(0)
    name = f"prospector-contacts-{utcnow():%Y%m%d-%H%M}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})
