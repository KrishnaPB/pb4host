"""Retention jobs (R5). Every period is configuration; defaults are in Settings."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Job, Organization, PerformanceSnapshot, Person, PlaceLink, RawSnapshot
from ..util import utcnow
from . import audit
from .suppression import anonymise_organization

ENGAGED_STATUSES = {"qualified", "exported", "contacted", "customer", "reviewing"}


def run_retention(session: Session, settings: Settings | None = None, now: datetime | None = None) -> dict[str, int]:
    settings = settings or get_settings()
    now = now or utcnow()
    report = {
        "raw_snapshots_deleted": 0,
        "perf_raw_purged": 0,
        "perf_snapshots_deleted": 0,
        "coords_expired": 0,
        "organizations_anonymised": 0,
        "persons_deleted": 0,
        "jobs_deleted": 0,
    }

    # Raw page snapshots past their expiry (files + rows)
    for snap in session.scalars(select(RawSnapshot).where(RawSnapshot.expires_at <= now)):
        with contextlib.suppress(OSError):
            Path(snap.path).unlink(missing_ok=True)
        session.delete(snap)
        report["raw_snapshots_deleted"] += 1

    # Raw PageSpeed JSON is kept only retention_raw_days; metrics stay until retention_perf_days
    raw_cutoff = now - timedelta(days=settings.retention_raw_days)
    for perf in session.scalars(
        select(PerformanceSnapshot).where(
            PerformanceSnapshot.fetched_at <= raw_cutoff, PerformanceSnapshot.raw_json.is_not(None)
        )
    ):
        perf.raw_json = None
        report["perf_raw_purged"] += 1
    perf_cutoff = now - timedelta(days=settings.retention_perf_days)
    for perf in session.scalars(select(PerformanceSnapshot).where(PerformanceSnapshot.fetched_at <= perf_cutoff)):
        session.delete(perf)
        report["perf_snapshots_deleted"] += 1

    # Places coordinates may be cached for at most 30 consecutive days
    for link in session.scalars(
        select(PlaceLink).where(PlaceLink.coords_expires_at <= now, PlaceLink.lat.is_not(None))
    ):
        link.lat = None
        link.lng = None
        report["coords_expired"] += 1

    # Persons without consent and without engagement
    person_cutoff = now - timedelta(days=settings.retention_person_days)
    for person in session.scalars(select(Person).where(Person.created_at <= person_cutoff)):
        org = person.organization
        if person.lawful_basis == "consent" and person.consent_id is not None:
            continue
        if org.status in ENGAGED_STATUSES or (org.last_engaged_at and org.last_engaged_at > person_cutoff):
            continue
        session.delete(person)
        report["persons_deleted"] += 1

    # Organisations never engaged with: anonymise, do not suppress (no request was made)
    org_cutoff = now - timedelta(days=settings.retention_unengaged_days)
    for org in session.scalars(
        select(Organization).where(
            Organization.created_at <= org_cutoff,
            Organization.status.in_(("new", "disqualified", "cold")),
        )
    ):
        if org.last_engaged_at and org.last_engaged_at > org_cutoff:
            continue
        anonymise_organization(session, org, reason="retention:unengaged", suppress=False)
        report["organizations_anonymised"] += 1

    # Finished jobs
    job_cutoff = now - timedelta(days=settings.retention_jobs_days)
    for job in session.scalars(
        select(Job).where(Job.status.in_(("done", "failed", "cancelled")), Job.created_at <= job_cutoff)
    ):
        session.delete(job)
        report["jobs_deleted"] += 1

    session.flush()
    audit.log(session, "retention.run", actor_label="system", **report)
    return report
