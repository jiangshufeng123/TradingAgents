"""How the live decisions have done: a forward scoreboard from the memory log.

Every analysis records its rating in the memory log, and a later run (or
``tradingagents scoreboard``) settles it with the raw and the alpha return over
the holding window (``holding_period_days``, default 5 trading days) against the
listing's regional benchmark. This reads those settled decisions and answers:

* by rating: how often a directional call got the direction right, and its
  average alpha;
* by ticker: the same, per instrument;
* versus doing nothing clever: the average raw return of following the calls
  (long on Buy/Overweight, out otherwise) against simply holding every name.

Only decisions made *after* the model's training cutoff say anything about the
system: earlier ones can be recalled rather than reasoned (see "Profit Mirage",
arXiv 2510.07920). Forward runs made from today on are clean by construction.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from tradingagents.agents.rating import RATING_REVIEW, RATINGS_5_TIER
from tradingagents.memory import TradingMemoryLog

DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
# Position taken when "following" a call: long on a bullish call, out otherwise
# (an individual can rarely short, and never on an A-share).
FOLLOW = {"Buy": 1.0, "Overweight": 1.0, "Hold": 0.0, "Underweight": 0.0, "Sell": 0.0}

CSV_FIELDS = ("date", "ticker", "rating", "raw_return", "alpha_return", "holding", "resolved",
              "direction_right")


def _pct(text) -> float | None:
    """'+3.2%' -> 0.032; None when not a number."""
    if text is None:
        return None
    value = str(text).strip().rstrip("%")
    try:
        return float(value) / 100
    except ValueError:
        return None


@dataclass
class Group:
    count: int
    directional: int
    right: int
    mean_raw: float
    mean_alpha: float

    @property
    def hit_rate(self) -> float | None:
        return self.right / self.directional if self.directional else None


def _group(rows: list[dict]) -> Group:
    directional = [r for r in rows if DIRECTION.get(r["rating"], 0)]
    right = sum(1 for r in directional if r["alpha_return"] * DIRECTION[r["rating"]] > 0)
    return Group(
        count=len(rows),
        directional=len(directional),
        right=right,
        mean_raw=sum(r["raw_return"] for r in rows) / len(rows),
        mean_alpha=sum(r["alpha_return"] for r in rows) / len(rows),
    )


def load_rows(log_path) -> tuple[list[dict], int, int]:
    """Settled decisions as rows, plus counts of pending and unscored (no rating) ones."""
    entries = TradingMemoryLog({"memory_log_path": str(log_path)}).load_entries()
    rows, pending, unscored = [], 0, 0
    for e in entries:
        if e["rating"] == RATING_REVIEW:
            unscored += 1
            continue
        if e["pending"]:
            pending += 1
            continue
        raw, alpha = _pct(e.get("raw")), _pct(e.get("alpha"))
        if raw is None or alpha is None:
            pending += 1
            continue
        direction = DIRECTION.get(e["rating"], 0)
        rows.append({
            "date": e["date"], "ticker": e["ticker"], "rating": e["rating"],
            "raw_return": raw, "alpha_return": alpha,
            "holding": e.get("holding") or "", "resolved": e.get("resolved") or "",
            "direction_right": "" if not direction else ("yes" if alpha * direction > 0 else "no"),
        })
    rows.sort(key=lambda r: (r["date"], r["ticker"]))
    return rows, pending, unscored


@dataclass
class Scoreboard:
    rows: list[dict]
    pending: int
    unscored: int

    def by_rating(self) -> dict[str, Group]:
        return {r: _group([x for x in self.rows if x["rating"] == r])
                for r in RATINGS_5_TIER if any(x["rating"] == r for x in self.rows)}

    def by_ticker(self) -> dict[str, Group]:
        tickers = sorted({x["ticker"] for x in self.rows})
        return {t: _group([x for x in self.rows if x["ticker"] == t]) for t in tickers}

    def overall(self) -> Group | None:
        return _group(self.rows) if self.rows else None

    def follow_vs_hold(self) -> tuple[float, float] | None:
        """Mean raw return per decision when following the calls, and when always holding."""
        if not self.rows:
            return None
        follow = sum(FOLLOW.get(r["rating"], 0.0) * r["raw_return"] for r in self.rows) / len(self.rows)
        hold = sum(r["raw_return"] for r in self.rows) / len(self.rows)
        return follow, hold


def build(log_path) -> Scoreboard:
    rows, pending, unscored = load_rows(log_path)
    return Scoreboard(rows=rows, pending=pending, unscored=unscored)


def _fmt_pct(value) -> str:
    return "—" if value is None else f"{value:+.2%}"


def _hit(group: Group) -> str:
    return "—" if group.hit_rate is None else f"{group.hit_rate:.0%} ({group.right}/{group.directional})"


def render_markdown(board: Scoreboard, title_date: str = "") -> str:
    holding = sorted({r["holding"] for r in board.rows if r["holding"]})
    lines = [f"# TradingAgents scoreboard{(' — ' + title_date) if title_date else ''}", ""]
    overall = board.overall()
    if overall is None:
        lines += [f"No settled decisions yet ({board.pending} pending). A decision settles once its "
                  "holding window has traded; run `tradingagents scoreboard` again later.", ""]
        return "\n".join(lines)
    follow, hold = board.follow_vs_hold()
    lines += [
        f"- Settled decisions: **{overall.count}** · pending: {board.pending}"
        + (f" · without a rating: {board.unscored}" if board.unscored else ""),
        f"- Holding window: {', '.join(h.rstrip('d') + ' trading days' for h in holding) or 'configured window'}; "
        "alpha is against the listing's regional benchmark (e.g. SPY, SSE Composite, Hang Seng).",
        f"- Direction right on directional calls: **{_hit(overall)}**",
        f"- Average per decision — following the calls (long on Buy/Overweight, out otherwise): "
        f"**{_fmt_pct(follow)}** vs holding every name: **{_fmt_pct(hold)}**",
        "",
        "## By rating",
        "| Rating | Decisions | Direction right | Avg raw | Avg alpha |",
        "|---|---:|---:|---:|---:|",
    ]
    for rating, g in board.by_rating().items():
        lines.append(f"| {rating} | {g.count} | {_hit(g)} | {_fmt_pct(g.mean_raw)} | {_fmt_pct(g.mean_alpha)} |")
    lines += ["", "## By ticker", "| Ticker | Decisions | Direction right | Avg raw | Avg alpha |",
              "|---|---:|---:|---:|---:|"]
    for ticker, g in board.by_ticker().items():
        lines.append(f"| {ticker} | {g.count} | {_hit(g)} | {_fmt_pct(g.mean_raw)} | {_fmt_pct(g.mean_alpha)} |")
    lines += ["", "## Decisions", "| Date | Ticker | Rating | Raw | Alpha | Right? |", "|---|---|---|---:|---:|---|"]
    for r in reversed(board.rows):
        lines.append(f"| {r['date']} | {r['ticker']} | {r['rating']} | {_fmt_pct(r['raw_return'])} | "
                     f"{_fmt_pct(r['alpha_return'])} | {r['direction_right'] or '—'} |")
    lines += ["", "Small samples say little: read hit rates only once there are dozens of settled "
              "decisions, and compare across market regimes. Decisions dated before the model's "
              "training cutoff can be recalled rather than reasoned, so judge on forward runs."]
    return "\n".join(lines)


def write(board: Scoreboard, out_dir, title_date: str = "") -> tuple[Path, Path]:
    """scoreboard.md and scoreboard.csv (UTF-8 with BOM, for Excel) in ``out_dir``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "scoreboard.md"
    md.write_text(render_markdown(board, title_date), encoding="utf-8")
    path = out_dir / "scoreboard.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in board.rows:
            writer.writerow({**r, "raw_return": round(r["raw_return"], 6),
                             "alpha_return": round(r["alpha_return"], 6)})
    return md, path
