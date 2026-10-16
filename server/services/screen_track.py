"""Do the screens pick stocks that then do well? The forward check.

Each finished screen keeps its top ``KEEP`` stocks (``record``), unless its owner switched
it off for the scoreboard (``set_tracked``: the job's ``params["track"]``, on by default).
Every trading evening the
scheduler (and `tradingagents server screen-check`) looks at the ones old enough
(``check``): the close ``HORIZONS`` sessions after the screen's trading day against that
day's close, for the stock and for the benchmark (CSI 300). ``summary`` turns them into
the scoreboard's "选股策略" section: per strategy, the average return over the benchmark
and how often a pick beat it, then every screen job of that strategy with its own picks.
Two screens of the same strategy on the same day each keep their own list (their extra
conditions may differ); the strategy's figures count a stock picked twice that day once.

The price is the close on the screen's trading day, so a screen run during a session is
measured from that day's close, not from the price seen on screen.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import ScreenPick

logger = logging.getLogger(__name__)

KEEP = 10
HORIZONS = (5, 20)
BENCHMARK = "000300.SS"


def strategy_of(spec) -> tuple[str, str]:
    """(strategy key, label) of a screen's ranking: its preset, or "custom" for a sort or
    weights of the user's or the model's own."""
    from tradingagents.scoring import PRESETS

    if spec.preset and not spec.weights and not spec.sort:
        return spec.preset, PRESETS[spec.preset].name
    return "custom", "自定义"


def record(s: Session, job_id: int | None, user_id: int | None, result: pd.DataFrame, spec, as_of: str,
           label: str = "") -> int:
    """Keep the top picks of a finished screen; returns how many new ones were kept. A job
    keeps each stock once; a screen without a job (none yet) once per day and strategy."""
    strategy, name = strategy_of(spec)
    if job_id is not None:
        where = ScreenPick.job_id == job_id
    else:
        where = (ScreenPick.job_id.is_(None)) & (ScreenPick.as_of == as_of) & (ScreenPick.strategy == strategy)
    have = set(s.scalars(select(ScreenPick.ticker).where(where)))
    added = 0
    for rank, (_, row) in enumerate(result.head(KEEP).iterrows(), 1):
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or not ticker or ticker in have:
            continue
        have.add(ticker)
        score = row.get("score")
        s.add(ScreenPick(job_id=job_id, user_id=user_id, as_of=as_of, strategy=strategy,
                         label=(label or name)[:120], rank=rank, ticker=ticker,
                         score=None if score is None or pd.isna(score) else float(score)))
        added += 1
    s.flush()
    return added


def tracked(job) -> bool:
    """Whether a screen job counts on the scoreboard (its owner's switch; on by default)."""
    return (job.params or {}).get("track", True) is not False


def as_of_of(job) -> str | None:
    """The quotes' trading day of a finished screen: saved with the result, else read from its
    source note ("Tushare (2026-10-16 close)"), else the day the job ran."""
    result = job.result or {}
    if result.get("as_of"):
        return str(result["as_of"])[:10]
    found = re.search(r"\d{4}-\d{2}-\d{2}", str(result.get("source") or ""))
    if found:
        return found.group(0)
    when = job.finished_at or job.created_at
    return when.date().isoformat() if when else None


def record_job(s: Session, job) -> int:
    """Keep the top picks of a finished screen job from its saved result (the switch turned
    back on, or a screen from before every job kept its own list)."""
    from tradingagents import screener

    result = job.result or {}
    rows, as_of = result.get("rows") or [], as_of_of(job)
    if job.kind != "screen" or job.status != "done" or not rows or not as_of:
        return 0
    spec = screener.Spec.from_dict(result.get("spec") or {})
    request = str(result.get("request") or (job.params or {}).get("request") or "")
    return record(s, job.id, job.user_id, pd.DataFrame(rows), spec, as_of,
                  label=request[:60] if request.strip() else "")


def backfill(s: Session) -> int:
    """Give every finished, switched-on screen job its whole list (before each job kept its
    own, a stock another screen of the strategy had picked that day was left out)."""
    from server.models import Job

    added = 0
    for job in s.scalars(select(Job).where(Job.kind == "screen", Job.status == "done").order_by(Job.id)):
        if not tracked(job):
            continue
        try:
            added += record_job(s, job)
        except Exception as exc:                  # one unreadable old result must not stop the rest
            logger.warning("screen %s not added to the forward check: %s", job.id, exc)
    return added


def set_tracked(s: Session, job, on: bool) -> int:
    """Switch a screen job on or off for the scoreboard. Off removes its picks (their returns
    too: turned back on they are checked again); on keeps them again if the job is done.
    Returns how many picks the job now has on the scoreboard."""
    job.params = {**(job.params or {}), "track": bool(on)}
    if on:
        record_job(s, job)
    else:
        s.query(ScreenPick).filter(ScreenPick.job_id == job.id).delete(synchronize_session=False)
    s.flush()
    return s.query(ScreenPick).filter(ScreenPick.job_id == job.id).count()


def _forward(bars: pd.DataFrame, as_of: str, n: int) -> float | None:
    """Return from the close on ``as_of`` (the last one on or before it) to the close ``n``
    sessions later; None while those sessions have not all traded."""
    if bars is None or bars.empty:
        return None
    dates = pd.to_datetime(bars["Date"]).dt.normalize()
    close = pd.to_numeric(bars["Close"], errors="coerce")
    before = (dates <= pd.Timestamp(as_of)).to_numpy().nonzero()[0]
    if not len(before):
        return None
    i = before[-1]
    if i + n >= len(close) or pd.isna(close.iloc[i]) or pd.isna(close.iloc[i + n]) or close.iloc[i] <= 0:
        return None
    return float(close.iloc[i + n] / close.iloc[i] - 1)


def check(s: Session, load, today: date | None = None) -> int:
    """Fill in the returns of picks old enough; returns how many picks gained a figure.
    ``load(ticker, as_of)`` gives daily bars up to ``as_of`` (the cached price loader)."""
    today = today or date.today()
    open_ = list(s.scalars(select(ScreenPick).where((ScreenPick.ret_5.is_(None)) | (ScreenPick.ret_20.is_(None)))))
    # 5 sessions take at least a week of calendar days; nothing younger can be ready.
    due = [p for p in open_ if (today - date.fromisoformat(p.as_of)).days >= 7]
    if not due:
        return 0
    as_of_today = today.isoformat()
    cache: dict[str, pd.DataFrame | None] = {}

    def bars(ticker):
        if ticker not in cache:
            try:
                cache[ticker] = load(ticker, as_of_today)
            except Exception as exc:
                logger.info("screen check: no prices for %s (%s)", ticker, exc)
                cache[ticker] = None
        return cache[ticker]

    filled = 0
    for p in due:
        changed = False
        for n in HORIZONS:
            if getattr(p, f"ret_{n}") is not None:
                continue
            mine, bench = _forward(bars(p.ticker), p.as_of, n), _forward(bars(BENCHMARK), p.as_of, n)
            if mine is not None and bench is not None:
                setattr(p, f"ret_{n}", mine)
                setattr(p, f"bench_{n}", bench)
                changed = True
        if changed:
            p.checked_at = datetime.now()
            filled += 1
    s.flush()
    return filled


DETAIL_SCREENS = 30       # screen jobs listed under a strategy, newest first


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pick_out(p: ScreenPick) -> dict:
    out = {"rank": p.rank, "ticker": p.ticker, "score": p.score}
    for n in HORIZONS:
        ret, bench = getattr(p, f"ret_{n}"), getattr(p, f"bench_{n}")
        out[f"ret_{n}"], out[f"bench_{n}"] = ret, bench
        out[f"excess_{n}"] = ret - bench if ret is not None and bench is not None else None
    return out


def _unique(picks: list[ScreenPick]) -> list[ScreenPick]:
    """One pick per trading day and stock (the earliest screen's), for a strategy's figures:
    two screens of a strategy the same day must not count the same stock twice."""
    seen, out = set(), []
    for p in sorted(picks, key=lambda p: (p.as_of, p.job_id is None, p.job_id or 0, p.rank)):
        if (p.as_of, p.ticker) not in seen:
            seen.add((p.as_of, p.ticker))
            out.append(p)
    return out


def screens(mine: list[ScreenPick], jobs: dict, user_id: int | None = None,
            name: str = "") -> tuple[list[dict], int]:
    """A strategy's picks grouped by screen job, newest first (at most ``DETAIL_SCREENS``),
    and how many older ones were left out. ``jobs``: job id -> its row, for the number
    (``serial``) and the conditions (``spec``) that tell two screens of a strategy apart.
    ``mine`` says whether ``user_id`` ran it (the page links to it; other accounts' screens
    are not theirs to open). Picks with no job (a screen whose job is gone) group by day.

    Another account's screen shows its number, day, strategy (``name``) and results only:
    its description and conditions are that person's own ideas, so ``request`` is empty,
    ``spec`` None and ``label`` the strategy's name."""
    groups: dict = {}
    for p in mine:
        groups.setdefault(p.job_id if p.job_id is not None else ("day", p.as_of), []).append(p)

    def newest(key):
        picks = groups[key]
        return (picks[0].as_of, key if isinstance(key, int) else 0)

    keys = sorted(groups, key=newest, reverse=True)
    out = []
    for key in keys[:DETAIL_SCREENS]:
        picks = sorted(groups[key], key=lambda p: (p.rank, p.ticker))
        job = jobs.get(key) if isinstance(key, int) else None
        result = (job.result or {}) if job is not None else {}
        rows = [_pick_out(p) for p in picks]
        owned = user_id is not None and all(p.user_id == user_id for p in picks)
        item = {"job": key if isinstance(key, int) else None, "serial": job.serial if job is not None else None,
                "mine": owned and job is not None,
                "as_of": picks[0].as_of, "label": (picks[0].label or "") if owned else name,
                "request": str(result.get("request") or "") if owned else "",
                "spec": result.get("spec") if owned else None,
                "picks": rows, "check_from": (date.fromisoformat(picks[0].as_of) + timedelta(days=7)).isoformat()}
        for n in HORIZONS:
            done = [r for r in rows if r[f"excess_{n}"] is not None]
            item[f"checked_{n}"] = len(done)
            item[f"excess_{n}"] = _mean([r[f"excess_{n}"] for r in done])
            item[f"ret_{n}"] = _mean([r[f"ret_{n}"] for r in done])
            item[f"bench_{n}"] = done[0][f"bench_{n}"] if done else None
            item[f"beat_{n}"] = sum(1 for r in done if r[f"excess_{n}"] > 0) / len(done) if done else None
        out.append(item)
    return out, max(0, len(keys) - DETAIL_SCREENS)


def summary(s: Session, user_id: int | None = None, only_mine: bool = False) -> dict:
    """Per strategy: stocks kept (one per day), and for each horizon the checked count,
    average excess return over the benchmark and the share that beat it, with each screen
    job's own picks (``screens``); plus the latest checked picks.

    Every account's screens count by default (more picks, steadier figures); ``only_mine``
    keeps ``user_id``'s alone, every figure worked out from those."""
    from tradingagents.scoring import PRESETS

    from server.models import Job

    picks = list(s.scalars(select(ScreenPick)))
    everyone = len(picks)
    if only_mine:
        picks = [p for p in picks if user_id is not None and p.user_id == user_id]
    ids = {p.job_id for p in picks if p.job_id}
    jobs = {j.id: j for j in s.scalars(select(Job).where(Job.id.in_(ids)))} if ids else {}
    order = list(PRESETS) + ["custom"]
    out = []
    for key in sorted({p.strategy for p in picks}, key=lambda k: order.index(k) if k in order else 99):
        mine = [p for p in picks if p.strategy == key]
        once = _unique(mine)
        row = {"strategy": key, "name": PRESETS[key].name if key in PRESETS else "自定义",
               "picks": len(once), "screens": len({p.job_id if p.job_id else p.as_of for p in mine}),
               "first": min(p.as_of for p in mine), "last": max(p.as_of for p in mine)}
        for n in HORIZONS:
            done = [p for p in once if getattr(p, f"ret_{n}") is not None]
            excess = [getattr(p, f"ret_{n}") - getattr(p, f"bench_{n}") for p in done]
            row[f"checked_{n}"] = len(done)
            row[f"excess_{n}"] = sum(excess) / len(excess) if excess else None
            row[f"beat_{n}"] = sum(1 for x in excess if x > 0) / len(excess) if excess else None
            row[f"ret_{n}"] = sum(getattr(p, f"ret_{n}") for p in done) / len(done) if done else None
        row["jobs"], row["older_jobs"] = screens(mine, jobs, user_id, row["name"])
        out.append(row)
    recent = sorted((p for p in picks if p.ret_5 is not None), key=lambda p: (p.as_of, p.strategy, p.rank),
                    reverse=True)[:30]
    pending = len({(p.strategy, p.as_of, p.ticker) for p in picks if p.ret_5 is None})
    names = {r["strategy"]: r["name"] for r in out}
    return {"strategies": out, "pending": pending, "horizons": list(HORIZONS), "benchmark": BENCHMARK,
            "scope": "mine" if only_mine else "all", "all_picks": everyone,
            "recent": [{"as_of": p.as_of, "strategy": p.strategy, "rank": p.rank,
                        "label": p.label if user_id is not None and p.user_id == user_id else names.get(p.strategy, ""),
                        "job": p.job_id, "serial": jobs[p.job_id].serial if p.job_id in jobs else None,
                        "ticker": p.ticker, "score": p.score, "ret_5": p.ret_5, "bench_5": p.bench_5,
                        "ret_20": p.ret_20, "bench_20": p.bench_20} for p in recent]}
