"""Each user's daily analysis, at the time they set (Beijing time by default).

At the set time the user's watchlist is queued as one analysis job; the worker
runs it like any other. Days use cron names: "mon-fri", "mon,wed,fri", "*".
Changing a schedule (API) calls ``reload``.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select

from server import db
from server.models import Schedule, User

logger = logging.getLogger(__name__)

TIMEZONE = os.environ.get("TRADINGAGENTS_TIMEZONE", "Asia/Shanghai")


def queue_daily(user_id: int, on_queued=None, config: dict | None = None) -> int | None:
    """Queue the user's watchlist for analysis now; the job id, or None when nothing to do.

    It is held to the spending caps like a run started from the web page: over a cap, the job
    is recorded as failed with the reason, so the analysis page shows why nothing ran."""
    from server import runner
    from server.services import costs, jobs, watchlist
    from tradingagents.default_config import DEFAULT_CONFIG

    config = config or DEFAULT_CONFIG

    with db.session() as s:
        user = s.get(User, user_id)
        sched = s.get(Schedule, user_id)
        if user is None or not user.is_active:
            return None
        tickers = watchlist.tickers(s, user_id)
        if not tickers:
            logger.info("scheduled run for %s skipped: empty watchlist", user.username)
            return None
        if any(j.kind == "analysis" and j.params.get("scheduled") for j in jobs.active(s, user_id)):
            logger.info("scheduled run for %s skipped: the previous one is still queued or running",
                        user.username)
            return None
        runs = (sched.runs or None) if sched else None
        est = runner.estimate(config, user, tickers, runs or int(config.get("vote_runs") or 1))
        ok, _, why = costs.check_budget(s, user, est or 0.0, config)
        job = jobs.create(s, user_id, "analysis", {"tickers": tickers, "runs": runs, "scheduled": True,
                                                    "estimate": est})
        job_id = job.id
        if not ok:
            jobs.fail(s, job, "定时分析没有运行：" + why)
            logger.warning("scheduled run for %s skipped: %s", user.username, why)
            return None
    if on_queued:
        on_queued()
    return job_id


def valid_time(text: str) -> tuple[int, int]:
    hh, mm = text.strip().split(":")
    hour, minute = int(hh), int(mm)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"not a time of day: {text!r}")
    return hour, minute


class DailyScheduler:
    def __init__(self, on_queued=None, timezone: str = TIMEZONE):
        from apscheduler.schedulers.background import BackgroundScheduler

        self.on_queued = on_queued
        self.timezone = timezone
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def start(self) -> None:
        self.reload()
        self.scheduler.start()

    @staticmethod
    def _backup() -> None:
        from server.backup import backup

        try:
            logger.info("database backup: %s", backup())
        except Exception as exc:          # a PostgreSQL database, or a full disk: say so, keep running
            logger.warning("database backup skipped: %s", exc)
        try:
            from server.logs import prune

            gone = prune()
            if gone:
                logger.info("deleted %d service log(s) older than 14 days", len(gone))
        except Exception as exc:
            logger.warning("old service logs not cleaned: %s", exc)

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reload(self) -> None:
        from apscheduler.triggers.cron import CronTrigger

        for job in self.scheduler.get_jobs():
            job.remove()
        self.scheduler.add_job(self._backup, CronTrigger(hour=3, minute=0, timezone=self.timezone),
                               id="backup", replace_existing=True, misfire_grace_time=6 * 3600, coalesce=True)
        with db.session() as s:
            rows = list(s.execute(select(Schedule, User).join(User, User.id == Schedule.user_id)
                                  .where(Schedule.enabled.is_(True), User.is_active.is_(True))))
            for sched, user in rows:
                try:
                    hour, minute = valid_time(sched.time_of_day)
                    trigger = CronTrigger(day_of_week=sched.days or "mon-fri", hour=hour, minute=minute,
                                          timezone=self.timezone)
                except ValueError as exc:
                    logger.warning("schedule for %s ignored: %s", user.username, exc)
                    continue
                self.scheduler.add_job(queue_daily, trigger, args=[user.id, self.on_queued],
                                       id=f"daily-{user.id}", replace_existing=True,
                                       misfire_grace_time=3600, coalesce=True)

    def next_run(self, user_id: int):
        job = self.scheduler.get_job(f"daily-{user_id}")
        return getattr(job, "next_run_time", None) if job else None
