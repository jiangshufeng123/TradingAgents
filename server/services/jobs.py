"""Long tasks: created queued, then run by the worker, then done or failed."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.models import Job

ACTIVE = ("queued", "running", "cancelling")
FINAL = ("done", "failed", "cancelled")


# Jobs that get a number people can quote: YYMMDD of the day the job was created and that
# day's sequence across all accounts, two digits ("26101601", "26101602"...; a 100th screen
# in one day gets three). Stored, so it never changes once given.
SERIAL_KINDS = ("screen",)


def next_serial(s: Session, day) -> str:
    prefix = day.strftime("%y%m%d")
    used = s.scalars(select(Job.serial).where(Job.serial.like(prefix + "%"))).all()
    n = max((int(x[6:]) for x in used if x and x[6:].isdigit()), default=0) + 1
    return f"{prefix}{n:02d}"


def create(s: Session, user_id: int, kind: str, params: dict) -> Job:
    job = Job(user_id=user_id, kind=kind, params=params, status="queued")
    if kind not in SERIAL_KINDS:
        s.add(job)
        s.flush()
        return job
    job.created_at = datetime.now()
    for attempt in range(5):               # two screens started at once: the second takes the next number
        job.serial = next_serial(s, job.created_at)
        try:
            with s.begin_nested():
                s.add(job)
                s.flush()
            return job
        except IntegrityError:
            if attempt == 4:
                raise
    return job


def backfill_serials(s: Session) -> int:
    """Number the screens made before numbers existed, in the order they were created."""
    old = list(s.scalars(select(Job).where(Job.kind.in_(SERIAL_KINDS), Job.serial.is_(None))
                         .order_by(Job.created_at, Job.id)))
    for job in old:
        job.serial = next_serial(s, job.created_at or datetime.now())
        s.flush()
    return len(old)


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


def page(s: Session, user_id: int, kind: str, number: int = 1, size: int = 10) -> tuple[list[Job], int]:
    """One page (from 1) of the user's jobs of a kind, newest first, and how many there are."""
    from sqlalchemy import func

    where = (Job.user_id == user_id) & (Job.kind == kind)
    total = s.scalar(select(func.count()).select_from(Job).where(where)) or 0
    rows = s.scalars(select(Job).where(where).order_by(Job.id.desc())
                     .offset((max(1, number) - 1) * size).limit(size))
    return list(rows), total


def next_queued(s: Session) -> Job | None:
    return s.scalar(select(Job).where(Job.status == "queued").order_by(Job.id).limit(1))


def active(s: Session, user_id: int | None = None) -> list[Job]:
    q = select(Job).where(Job.status.in_(ACTIVE))
    if user_id is not None:
        q = q.where(Job.user_id == user_id)
    return list(s.scalars(q.order_by(Job.id)))
