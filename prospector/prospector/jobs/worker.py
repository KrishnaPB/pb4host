"""Worker loop: claims jobs from the database queue, runs handlers, does daily housekeeping."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time

from ..config import get_settings
from ..db import session_factory
from ..util import utcnow
from . import tasks  # noqa: F401  (registers handlers)
from .queue import claim_next, enqueue, execute

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, worker_id: str | None = None, poll_interval: float | None = None) -> None:
        self.settings = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.poll_interval = poll_interval or self.settings.worker_poll_interval_s
        self._stop = False
        self._last_housekeeping: str | None = None

    def stop(self, *_args) -> None:
        log.info("worker %s stopping after current job", self.worker_id)
        self._stop = True

    def housekeeping(self) -> None:
        today = utcnow().strftime("%Y%m%d")
        if self._last_housekeeping == today:
            return
        session = session_factory()()
        try:
            enqueue(session, "retention_purge", {}, idempotency_key=f"retention:{today}")
            session.commit()
        finally:
            session.close()
        self._last_housekeeping = today

    def run_once(self) -> bool:
        session = session_factory()()
        try:
            job = claim_next(session, self.worker_id)
            if job is None:
                return False
            log.info("job %s %s start", job.id, job.type)
            ok = execute(session, job)
            log.info("job %s %s %s", job.id, job.type, "done" if ok else "failed/retry")
            return True
        finally:
            session.close()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("worker %s started", self.worker_id)
        while not self._stop:
            self.housekeeping()
            try:
                busy = self.run_once()
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                time.sleep(self.poll_interval)
