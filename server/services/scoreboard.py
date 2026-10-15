"""The scoreboard page: how the system's settled decisions have done.

Decisions and their outcomes live in the shared memory log (every account's analyses
feed it, and every account learns from it), so this page is the same for everyone.
A decision settles once its holding window (``holding_period_days`` trading days) has
traded: its raw return and its return against the listing's benchmark ("alpha") are
written next to it. The numbers here come from ``tradingagents.scoreboard``, the same
module as `tradingagents scoreboard`.
"""
from __future__ import annotations

from collections import defaultdict

from tradingagents import scoreboard as sb
from tradingagents.agents.rating import RATING_REVIEW
from tradingagents.memory import TradingMemoryLog
from tradingagents.memory.learning import _versions, beliefs_path

RECENT = 30


def _group(group: sb.Group | None) -> dict | None:
    if group is None:
        return None
    return {"count": group.count, "directional": group.directional, "right": group.right,
            "hit_rate": group.hit_rate, "mean_raw": group.mean_raw, "mean_alpha": group.mean_alpha}


def payload(config: dict) -> dict:
    log_path = config["memory_log_path"]
    board = sb.build(log_path)
    entries = TradingMemoryLog({"memory_log_path": str(log_path)}).load_entries()
    pending = sorted(({"date": e["date"], "ticker": e["ticker"], "rating": e["rating"]}
                      for e in entries if e.get("pending") and e["rating"] != RATING_REVIEW),
                     key=lambda r: (r["date"], r["ticker"]))
    review = sorted(({"date": e["date"], "ticker": e["ticker"]} for e in entries if e["rating"] == RATING_REVIEW),
                    key=lambda r: r["date"], reverse=True)

    months: dict[str, list[dict]] = defaultdict(list)
    for row in board.rows:
        months[row["date"][:7]].append(row)
    follow = board.follow_vs_hold()
    versions = _versions(beliefs_path(config))
    return {
        "holding_days": int(config.get("holding_period_days") or 5),
        "total": len(entries),
        "overall": _group(board.overall()),
        "follow": None if follow is None else {"follow": follow[0], "hold": follow[1]},
        "by_rating": [{"rating": r, **_group(g)} for r, g in board.by_rating().items()],
        "by_ticker": sorted(({"ticker": t, **_group(g)} for t, g in board.by_ticker().items()),
                            key=lambda r: (-r["count"], r["ticker"])),
        "by_month": [{"month": m, **_group(sb._group(rows))} for m, rows in sorted(months.items())],
        "recent": [{k: r[k] for k in ("date", "ticker", "rating", "raw_return", "alpha_return",
                                      "resolved", "direction_right")}
                   for r in sorted(board.rows, key=lambda r: (r["date"], r["ticker"]), reverse=True)[:RECENT]],
        "pending": pending,
        "review": review[:RECENT],
        "review_count": len(review),
        "beliefs": [{"created": v.get("created"), "as_of": v.get("as_of"), "based_on": v.get("based_on"),
                     "beliefs": v.get("beliefs") or ""} for v in reversed(versions)][:10],
    }
