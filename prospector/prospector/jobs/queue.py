"""Database-backed job queue.

One VPS, a few thousand jobs a day: a table with an atomic claim is enough and keeps the
deployment to API + worker + PostgreSQL. Swap for Redis/RQ later if volume demands it; the
task handlers do not care.
"""

from __future__ import annotations

import logging
import socket
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Job
from ..util import utcnow

log = logging.getLogger(__name__)

Handler = Callable[[Session, dict[str, Any]], None]
HANDLERS: dict[str, Handler] = {}


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[job_type] = fn
        return fn

    return deco


def enqueue(
    session: Session,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 3,
    scrub_on_done: bool = False,
) -> Job | None:
    """Create a job. Returns None when an identical idempotency key already exists."""
    if idempotency_key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            return None
    job = Job(
        type=job_type,
        payload=payload or {},
        idempotency_key=idempotency_key,
        run_after=run_after or utcnow(),
        max_attempts=max_attempts,
        scrub_on_done=scrub_on_done,
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return job


def claim_next(session: Session, worker_id: str, types: list[str] | None = None) -> Job | None:
    now = utcnow()
    stmt = (
        select(Job)
        .where(Job.status == "queued", Job.run_after <= now)
        .order_by(Job.run_after, Job.id)
        .limit(5)
    )
    if types:
        stmt = stmt.where(Job.type.in_(types))
    for job in session.scalars(stmt).all():
        result = session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == "queued")
            .values(status="running", started_at=now, worker_id=worker_id, attempts=Job.attempts + 1)
        )
        if result.rowcount == 1:
            session.commit()
            session.refresh(job)
            return job
    session.rollback()
    return None


def complete(session: Session, job: Job) -> None:
    job.status = "done"
    job.finished_at = utcnow()
    job.last_error = None
    if job.scrub_on_done:
        # Drop transient third-party content (e.g. a Places website hint) once consumed.
        job.payload = {k: v for k, v in job.payload.items() if k in {"organization_id", "run_id", "website_id"}}
        job.payload["scrubbed"] = True
    session.commit()


def fail(session: Session, job: Job, error: str, *, retry_in: timedelta | None = None, permanent: bool = False) -> None:
    job.last_error = error[:4000]
    if not permanent and job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = utcnow() + (retry_in or timedelta(minutes=5 * job.attempts))
    else:
        job.status = "failed"
        job.finished_at = utcnow()
    session.commit()


def execute(session: Session, job: Job) -> bool:
    """Run one claimed job. Returns True on success."""
    from ..connectors.base import (  # local: avoid cycles
        CapExceeded,
        ConnectorDisabled,
        TermsReviewRequired,
    )

    fn = HANDLERS.get(job.type)
    if fn is None:
        fail(session, job, f"no handler for job type {job.type}", permanent=True)
        return False
    try:
        fn(session, dict(job.payload))
        session.commit()
        complete(session, job)
        return True
    except (ConnectorDisabled, TermsReviewRequired) as exc:
        session.rollback()
        fail(session, job, str(exc), permanent=True)
        return False
    except CapExceeded as exc:
        session.rollback()
        tomorrow = (utcnow() + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        job.attempts = max(0, job.attempts - 1)  # waiting for the cap is not a failure
        fail(session, job, str(exc), retry_in=tomorrow - utcnow())
        return False
    except Exception as exc:
        session.rollback()
        log.exception("job %s (%s) failed", job.id, job.type)
        fail(session, job, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")
        return False


def run_pending(session_factory, *, limit: int | None = None, worker_id: str | None = None, types: list[str] | None = None) -> int:
    """Synchronously drain the queue (CLI spike, tests). Returns the number of jobs executed."""
    worker_id = worker_id or f"inline-{socket.gethostname()}"
    executed = 0
    while limit is None or executed < limit:
        session = session_factory()
        try:
            job = claim_next(session, worker_id, types)
            if job is None:
                break
            execute(session, job)
            executed += 1
        finally:
            session.close()
    return executed
