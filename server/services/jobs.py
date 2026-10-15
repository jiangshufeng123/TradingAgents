"""Long tasks: created queued, then run by the worker, then done or failed."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import Job

ACTIVE = ("queued", "running", "cancelling")
FINAL = ("done", "failed", "cancelled")


def create(s: Session, user_id: int, kind: str, params: dict) -> Job:
    job = Job(user_id=user_id, kind=kind, params=params, status="queued")
    s.add(job)
    s.flush()
    return job


def start(s: Session, job: Job, log_path: str | None = None) -> None:
    job.status, job.started_at, job.log_path = "running", datetime.now(), log_path


def finish(s: Session, job: Job, result: dict | None = None, cost: float | None = None) -> None:
    job.status, job.finished_at, job.result, job.cost = "done", datetime.now(), result, cost


def fail(s: Session, job: Job, error: str) -> None:
    job.status, job.finished_at, job.error = "failed", datetime.now(), error[:4000]


def cancel(s: Session, job: Job) -> bool:
    """A queued job is cancelled at once; a running one is marked for the worker to stop."""
    if job.status == "queued":
        job.status, job.finished_at = "cancelled", datetime.now()
        return True
    if job.status == "running":
        job.status = "cancelling"
        return True
    return False


def claim(s: Session, job_id: int) -> bool:
    """Move a queued job to running; False when another worker took it first."""
    from sqlalchemy import update

    done = s.execute(update(Job).where(Job.id == job_id, Job.status == "queued")
                     .values(status="running", started_at=datetime.now()))
    return done.rowcount == 1


def cancelled(job: Job) -> None:
    job.status, job.finished_at = "cancelled", datetime.now()


def interrupted(s: Session) -> int:
    """Jobs left running by a stopped server are marked failed (at start-up)."""
    n = 0
    for job in s.scalars(select(Job).where(Job.status.in_(("running", "cancelling")))):
        job.status, job.finished_at = "failed", datetime.now()
        job.error = "the server stopped while this job was running"
        n += 1
    return n


def recent(s: Session, user_id: int, limit: int = 20) -> list[Job]:
    return list(s.scalars(select(Job).where(Job.user_id == user_id).order_by(Job.id.desc()).limit(limit)))


def next_queued(s: Session) -> Job | None:
    return s.scalar(select(Job).where(Job.status == "queued").order_by(Job.id).limit(1))


def active(s: Session, user_id: int | None = None) -> list[Job]:
    q = select(Job).where(Job.status.in_(ACTIVE))
    if user_id is not None:
        q = q.where(Job.user_id == user_id)
    return list(s.scalars(q.order_by(Job.id)))
