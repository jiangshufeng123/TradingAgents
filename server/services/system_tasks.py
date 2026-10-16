"""The service's own scheduled jobs that keep the scoreboard current, and their run records.

Two jobs, both on by default and switched from the scoreboard page (admins only; the
results are shared by every account):

* ``screen-check`` (trading days 17:40): the 5- and 20-day returns of screened picks old
  enough (services/screen_track.py).
* ``settle`` (trading days 17:45): settles the analysis decisions whose holding window has
  traded, writing each one's return, its return against the benchmark and a short
  reflection by the quick model, then revises the learned beliefs when enough have settled.
  Analyses settle due decisions as they run too; this job does it on days nothing is
  analysed. Its model calls are charged to the system owner (the first administrator, whose
  model account pays for them) in the cost table, as "决策结算", so they count toward that
  account's caps.

The switch is for admins: it changes what every account sees. "立即运行" is open to every
account: a run only settles what is already due, so a second run settles nothing more and
costs nothing; who started it is recorded.

Each run is recorded (``TaskRun``): when it started and finished, whether the schedule or
a person started it (and who), how it ended and a one-line summary. A switched-off job
does not run on its schedule (and is not recorded); "立即运行" still runs it once.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from server import db
from server.models import SystemTask, TaskRun

logger = logging.getLogger(__name__)

TASKS = {
    "screen-check": {"title": "选股检验", "schedule": "每个交易日 17:40",
                     "about": "给入选满 5 个、20 个交易日的选股记录补上涨跌和同期沪深300，更新选股策略成绩。不调用模型。"},
    "settle": {"title": "决策结算", "schedule": "每个交易日 17:45",
               "about": "结算持有期（5 个交易日）已走完的分析决策：算出涨跌和超额收益，每条用快速模型写一段复盘（每条约几分钱），"
                        "攒够一批后重写“学到的规则”。分析运行时也会顺带结算。模型费用记在系统管理员名下。"},
}
HISTORY = 10
STALE = timedelta(hours=3)     # a "running" record older than this belongs to a run that died

_lock = threading.Lock()
_running: set[str] = set()


def enabled(s: Session, name: str) -> bool:
    row = s.get(SystemTask, name)
    return True if row is None else bool(row.enabled)


def set_enabled(s: Session, name: str, on: bool, by: str = "") -> None:
    if name not in TASKS:
        raise KeyError(name)
    row = s.get(SystemTask, name) or SystemTask(name=name)
    row.enabled, row.updated_at, row.updated_by = bool(on), datetime.now(), by[:64]
    s.add(row)
    s.flush()


def close_interrupted(s: Session) -> int:
    """Runs left "running" by a service that stopped mid-run: recorded as failed."""
    rows = list(s.scalars(select(TaskRun).where(TaskRun.status == "running")))
    for row in rows:
        row.status, row.finished_at = "failed", datetime.now()
        row.summary = (row.summary + " " if row.summary else "") + "服务停止，运行中断。"
    s.flush()
    return len(rows)


def is_running(name: str) -> bool:
    with _lock:
        return name in _running


def run(name: str, work, trigger: str = "schedule", by: str = "") -> TaskRun | None:
    """Run ``work()`` as job ``name`` and record it. ``work`` returns (summary, detail).
    A job already running here is not started twice (returns None)."""
    with _lock:
        if name in _running:
            return None
        _running.add(name)
    try:
        with db.session() as s:
            record = TaskRun(name=name, trigger=trigger, status="running", started_at=datetime.now())
            s.add(record)
            s.flush()
            run_id = record.id
        try:
            summary, detail = work()
            status = "done"
        except Exception as exc:
            logger.warning("%s failed: %s", name, exc)
            summary, detail, status = f"出错：{type(exc).__name__}: {exc}"[:480], None, "failed"
        with db.session() as s:
            record = s.get(TaskRun, run_id)
            record.status, record.finished_at = status, datetime.now()
            record.summary = (summary or "")[:500]
            record.detail = {**(detail or {}), "by": by} if by else detail
            s.flush()
            s.expunge(record)
        return record
    finally:
        with _lock:
            _running.discard(name)


def scheduled(name: str, work) -> TaskRun | None:
    """The scheduler's entry: runs the job only while it is switched on."""
    with db.session() as s:
        on = enabled(s, name)
    if not on:
        logger.info("%s skipped: switched off", name)
        return None
    return run(name, work, "schedule")


def start_now(name: str, work, by: str = "") -> bool:
    """Run the job once in the background (the page's "立即运行"); False if already running."""
    if is_running(name):
        return False
    threading.Thread(target=run, args=(name, work, "manual", by), name=f"task-{name}", daemon=True).start()
    return True


def _run_out(r: TaskRun) -> dict:
    took = (r.finished_at - r.started_at).total_seconds() if r.finished_at and r.started_at else None
    return {"id": r.id, "trigger": r.trigger, "status": r.status,
            "started_at": r.started_at.isoformat(timespec="seconds") if r.started_at else None,
            "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
            "seconds": took, "summary": r.summary, "detail": r.detail}


def status(s: Session) -> list[dict]:
    """Every job with its switch and its latest runs, newest first."""
    out = []
    for name, info in TASKS.items():
        row = s.get(SystemTask, name)
        runs = list(s.scalars(select(TaskRun).where(TaskRun.name == name)
                              .order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(HISTORY)))
        out.append({"name": name, **info, "enabled": True if row is None else bool(row.enabled),
                    "updated_at": row.updated_at.isoformat(timespec="seconds") if row and row.updated_at else None,
                    "updated_by": row.updated_by if row else "", "running": is_running(name),
                    "runs": [_run_out(r) for r in runs]})
    return out


# --- the jobs -------------------------------------------------------------------------

def screen_check(config: dict):
    from server.services import screen_track
    from tradingagents import screener

    with db.session() as s:
        n = screen_track.check(s, screener.price_loader(config))
        waiting = s.query(screen_track.ScreenPick).filter(screen_track.ScreenPick.ret_20.is_(None)).count()
    head = f"补上了 {n} 只入选股票的涨跌" if n else "没有到期的入选股票"
    return f"{head}，还有 {waiting} 只没满 20 个交易日。", {"filled": n, "waiting": waiting}


def _pending(config: dict) -> list[dict]:
    from tradingagents.agents.rating import RATING_REVIEW
    from tradingagents.memory import TradingMemoryLog

    log = TradingMemoryLog({"memory_log_path": str(config["memory_log_path"])})
    return [e for e in log.get_pending_entries() if e.get("rating") != RATING_REVIEW]


def system_owner(s: Session):
    """The account the service's own model calls are charged to: the first active admin."""
    from server.models import User

    return s.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)).order_by(User.id).limit(1))


def charge(config: dict, stats, label: str) -> dict | None:
    """Record the model calls ``stats`` (a StatsCallbackHandler) counted as one cost entry of
    the system owner; what was recorded, or None when there were no calls (or no owner)."""
    from server.services import costs
    from tradingagents.usage import summarize

    if stats is None or not stats.llm_calls:
        return None
    usage = summarize(stats.usage, config)
    total = usage["total"]
    with db.session() as s:
        owner = system_owner(s)
        if owner is None:
            return None
        costs.record(s, owner.id, run_time=datetime.now(), ticker="", trade_date=datetime.now().date().isoformat(),
                     rating=label, llm_calls=stats.llm_calls, input_tokens=int(total.get("input_tokens") or 0),
                     output_tokens=int(total.get("output_tokens") or 0), cost=usage["cost"],
                     currency=usage["currency"])
        return {"calls": stats.llm_calls, "input_tokens": int(total.get("input_tokens") or 0),
                "output_tokens": int(total.get("output_tokens") or 0), "cost": usage["cost"],
                "currency": usage["currency"], "charged_to": owner.username}


def _money(spent: dict) -> str:
    if spent.get("cost") is None:
        return f"模型调用 {spent['calls']} 次"
    sign = "¥" if spent.get("currency") in ("", "CNY", "¥", "RMB") else spent["currency"] + " "
    return f"模型花费 {sign}{spent['cost']:.2f}（{spent['calls']} 次调用）"


def settle(config: dict, graph=None, stats=None):
    """Settle the due decisions and revise the beliefs, charging the model calls to the system
    owner; (summary, detail)."""
    before = _pending(config)
    if not before:
        return "没有待结算的决策。", {"settled": [], "failed": [], "pending": 0, "beliefs_revised": False}
    if graph is None:
        from cli.stats_handler import StatsCallbackHandler
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        stats = StatsCallbackHandler()
        graph = TradingAgentsGraph(config=config, callbacks=[stats])
    try:
        outcome = graph.settle_all_pending()
        revised = graph.update_beliefs() if outcome.settled else None
    finally:
        spent = charge(config, stats, "决策结算")   # what was spent is recorded even if a step failed
    left = len(_pending(config))
    settled = [{"ticker": t, "date": d} for t, d in outcome.settled]
    failed = [{"ticker": t, "date": d, "reason": why[:200]} for t, d, why in outcome.failed]
    parts = [f"结算 {len(settled)} 条" if settled else "没有到期的决策"]
    if failed:
        parts.append(f"{len(failed)} 条没结算成（下次再试）")
    parts.append(f"仍待结算 {left} 条")
    if revised:
        parts.append(f"已按 {revised.get('based_on', len(settled))} 条结算结果重写学到的规则")
    if spent:
        parts.append(_money(spent))
    return "，".join(parts) + "。", {"settled": settled, "failed": failed, "pending": left,
                                    "beliefs_revised": bool(revised), "spent": spent}


WORK = {"screen-check": screen_check, "settle": settle}
