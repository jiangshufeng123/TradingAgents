"""The job worker: takes queued jobs one at a time and runs them.

One at a time on purpose: an analysis already runs many model calls in
parallel, the providers' rate limits are per account, and the memory log
settles past decisions as each run starts. A queued job waits its turn.
"""

from __future__ import annotations

import logging
import threading
import traceback

from server import db
from server.models import Job, User
from server.services import costs, jobs as job_svc

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: dict, runners: dict | None = None, poll: float = 2.0):
        from server import runner

        self.config = config
        self.runners = runners or runner.RUNNERS
        self.poll = poll
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: int | None = None

    # control
    def start(self) -> None:
        with db.session() as s:
            n = job_svc.interrupted(s)
        if n:
            logger.warning("%d job(s) were left running by the last server and are marked failed", n)
        self._thread = threading.Thread(target=self._loop, name="ta-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        """A job was queued: look now rather than at the next poll."""
        self._wake.set()

    # the loop
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self.run_next()
            except Exception:              # never let the worker die
                logger.exception("worker step failed")
                ran = False
            if not ran:
                self._wake.wait(self.poll)
                self._wake.clear()

    def run_next(self) -> bool:
        """Run the oldest queued job, if any; True when one ran."""
        with db.session() as s:
            job = job_svc.next_queued(s)
            if job is None or not job_svc.claim(s, job.id):
                return False
            job_id = job.id
        self.run(job_id)
        return True

    def _should_stop(self, job_id: int):
        def check() -> bool:
            if self._stop.is_set():
                return True
            with db.session() as s:
                job = s.get(Job, job_id)
                return job is None or job.status == "cancelling"
        return check

    def run(self, job_id: int) -> None:
        from server import runner

        with db.session() as s:
            job = s.get(Job, job_id)
            user = s.get(User, job.user_id)
            job.log_path = str(runner.job_log(job_id))
        self.current = job_id
        try:
            fn = self.runners.get(job.kind)
            if fn is None:
                raise RuntimeError(f"unknown job kind {job.kind!r}")
            result = fn(job, user, self.config, self._should_stop(job_id))
        except runner.Cancelled:
            with db.session() as s:
                job_svc.cancelled(s.get(Job, job_id))
            return
        except Exception as exc:
            logger.warning("job %s failed: %s", job_id, exc)
            with db.session() as s:
                job_svc.fail(s, s.get(Job, job_id), f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}")
            return
        finally:
            self.current = None
        with db.session() as s:
            job = s.get(Job, job_id)
            total = None
            for r in (result or {}).get("runs", []):
                costs.record(s, job.user_id, job_id=job_id, ticker=r.get("ticker") or "",
                             trade_date=r.get("trade_date") or "", rating=r.get("rating") or "",
                             llm_calls=r.get("llm_calls") or 0, input_tokens=r.get("input_tokens") or 0,
                             output_tokens=r.get("output_tokens") or 0, cost=r.get("cost"),
                             currency=r.get("currency") or "", report_dir=r.get("report_dir") or "")
                if r.get("cost") is not None:
                    total = (total or 0.0) + r["cost"]
            # A stop asked for just as the job finished: it is done, and what it did is kept.
            job_svc.finish(s, job, result, total)
