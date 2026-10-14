from datetime import timedelta

from prospector.jobs import tasks  # noqa: F401  registers handlers
from prospector.jobs.queue import HANDLERS, claim_next, enqueue, execute, handler, run_pending
from prospector.models import Job
from prospector.util import utcnow


def test_enqueue_is_idempotent(session):
    j1 = enqueue(session, "noop", {}, idempotency_key="k1")
    j2 = enqueue(session, "noop", {}, idempotency_key="k1")
    session.commit()
    assert j1 is not None and j2 is None


def test_claim_respects_run_after_and_marks_running(session):
    enqueue(session, "noop", {"a": 1}, run_after=utcnow() + timedelta(hours=1))
    now_job = enqueue(session, "noop", {"a": 2})
    session.commit()
    job = claim_next(session, "w1")
    assert job is not None and job.id == now_job.id and job.status == "running" and job.attempts == 1
    assert claim_next(session, "w1") is None


def test_failed_job_retries_then_fails(session):
    calls = {"n": 0}

    @handler("boom")
    def boom(sess, payload):
        calls["n"] += 1
        raise RuntimeError("kaboom")

    try:
        enqueue(session, "boom", {}, max_attempts=2)
        session.commit()
        job = claim_next(session, "w1")
        assert execute(session, job) is False
        session.refresh(job)
        assert job.status == "queued" and job.attempts == 1 and "kaboom" in job.last_error
        job.run_after = utcnow() - timedelta(seconds=1)
        session.commit()
        job = claim_next(session, "w1")
        assert execute(session, job) is False
        session.refresh(job)
        assert job.status == "failed" and job.attempts == 2
    finally:
        HANDLERS.pop("boom", None)


def test_scrub_on_done_drops_transient_payload(session):
    enqueue(session, "noop", {"organization_id": 1, "url_hint": "https://from-places.example"}, scrub_on_done=True)
    session.commit()
    job = claim_next(session, "w1")
    assert execute(session, job) is True
    session.refresh(job)
    assert job.status == "done" and job.payload == {"organization_id": 1, "scrubbed": True}


def test_run_pending_drains(session):
    from prospector.db import session_factory

    for i in range(3):
        enqueue(session, "noop", {"i": i})
    session.commit()
    assert run_pending(session_factory()) == 3
    assert session.query(Job).filter(Job.status == "done").count() == 3
