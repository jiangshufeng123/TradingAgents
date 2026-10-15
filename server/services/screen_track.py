"""Do the screens pick stocks that then do well? The forward check.

Each finished screen keeps its top ``KEEP`` stocks (``record``). Every trading evening the
scheduler (and `tradingagents server screen-check`) looks at the ones old enough
(``check``): the close ``HORIZONS`` sessions after the screen's trading day against that
day's close, for the stock and for the benchmark (CSI 300). ``summary`` turns them into
the scoreboard's "选股策略" section: per strategy, the average return over the benchmark
and how often a pick beat it.

The price is the close on the screen's trading day, so a screen run during a session is
measured from that day's close, not from the price seen on screen.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

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
    """Keep the top picks of a finished screen; returns how many new ones were kept."""
    strategy, name = strategy_of(spec)
    have = {(p.ticker) for p in s.scalars(select(ScreenPick).where(ScreenPick.as_of == as_of,
                                                                   ScreenPick.strategy == strategy))}
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


def summary(s: Session) -> dict:
    """Per strategy: picks kept, and for each horizon the checked count, average excess
    return over the benchmark and the share that beat it; plus the latest checked picks."""
    from tradingagents.scoring import PRESETS

    picks = list(s.scalars(select(ScreenPick)))
    order = list(PRESETS) + ["custom"]
    out = []
    for key in sorted({p.strategy for p in picks}, key=lambda k: order.index(k) if k in order else 99):
        mine = [p for p in picks if p.strategy == key]
        row = {"strategy": key, "name": PRESETS[key].name if key in PRESETS else "自定义",
               "picks": len(mine), "screens": len({(p.as_of, p.job_id) for p in mine}),
               "first": min(p.as_of for p in mine), "last": max(p.as_of for p in mine)}
        for n in HORIZONS:
            done = [p for p in mine if getattr(p, f"ret_{n}") is not None]
            excess = [getattr(p, f"ret_{n}") - getattr(p, f"bench_{n}") for p in done]
            row[f"checked_{n}"] = len(done)
            row[f"excess_{n}"] = sum(excess) / len(excess) if excess else None
            row[f"beat_{n}"] = sum(1 for x in excess if x > 0) / len(excess) if excess else None
            row[f"ret_{n}"] = sum(getattr(p, f"ret_{n}") for p in done) / len(done) if done else None
        out.append(row)
    recent = sorted((p for p in picks if p.ret_5 is not None), key=lambda p: (p.as_of, p.strategy, p.rank),
                    reverse=True)[:30]
    pending = sum(1 for p in picks if p.ret_5 is None)
    return {"strategies": out, "pending": pending, "horizons": list(HORIZONS), "benchmark": BENCHMARK,
            "recent": [{"as_of": p.as_of, "strategy": p.strategy, "label": p.label, "rank": p.rank,
                        "ticker": p.ticker, "score": p.score, "ret_5": p.ret_5, "bench_5": p.bench_5,
                        "ret_20": p.ret_20, "bench_20": p.bench_20} for p in recent]}
