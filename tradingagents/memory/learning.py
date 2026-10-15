"""Learning from settled decisions: a measured track record and distilled beliefs.

The reflection on each settled decision is a single lesson about a single
trade. Two things turn those into something a later run can act on:

* **Track record** (code): hit rate and alpha of the system's own calls, by
  rating and for the ticker at hand, so the Research Manager and Portfolio
  Manager know how far to trust a bullish or bearish case of the kind they are
  about to make.
* **Beliefs** (one LLM call every few settlements): the reflections of calls
  that worked are contrasted with those that did not, and a short list of
  standing rules is revised, each with the evidence behind it, in the spirit of
  FinCon's conceptual verbal reinforcement. Every revision is kept with the
  date its evidence runs to, so a historical run only reads beliefs that
  existed at its own date.

Both are cut at the run's point in time: only decisions resolved by the
analysis date count for a historical run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from tradingagents.agents.rating import RATING_REVIEW

logger = logging.getLogger(__name__)

DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}
MIN_SAMPLE = 20            # below this, say the record is too short to lean on
MAX_BELIEFS = 8


def _pct(text) -> float | None:
    if text is None:
        return None
    try:
        return float(str(text).strip().rstrip("%")) / 100
    except ValueError:
        return None


def settled_rows(entries: list[dict], as_of: str | None = None) -> list[dict]:
    """Settled, rated decisions with numeric returns, resolved by ``as_of`` when given."""
    rows = []
    for e in entries:
        if e.get("pending") or e.get("rating") in (None, RATING_REVIEW):
            continue
        if as_of is not None and not (e.get("resolved") and e["resolved"] <= as_of):
            continue
        raw, alpha = _pct(e.get("raw")), _pct(e.get("alpha"))
        if raw is None or alpha is None:
            continue
        rows.append({**e, "raw_return": raw, "alpha_return": alpha})
    return rows


def _stats(rows: list[dict]) -> tuple[int, int, int, float]:
    """(count, directional, right, mean alpha)."""
    directional = [r for r in rows if DIRECTION.get(r["rating"], 0)]
    right = sum(1 for r in directional if r["alpha_return"] * DIRECTION[r["rating"]] > 0)
    mean_alpha = sum(r["alpha_return"] for r in rows) / len(rows) if rows else 0.0
    return len(rows), len(directional), right, mean_alpha


def _hit(directional: int, right: int) -> str:
    return f"{right}/{directional} right ({right / directional:.0%})" if directional else "no directional calls"


def track_record(entries: list[dict], ticker: str, as_of: str | None = None) -> str:
    """The system's own record, for the decision agents; '' when nothing has settled."""
    rows = settled_rows(entries, as_of)
    if not rows:
        return ""
    n, directional, right, mean_alpha = _stats(rows)
    lines = [f"Your track record ({n} settled decision(s); each scored on its holding-window alpha "
             f"against the regional benchmark):",
             f"- All calls: {_hit(directional, right)}, mean alpha {mean_alpha:+.2%}"]
    for rating in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
        group = [r for r in rows if r["rating"] == rating]
        if group:
            gn, gd, gr, ga = _stats(group)
            hit = f", {_hit(gd, gr)}" if gd else ""
            lines.append(f"- {rating}: {gn} call(s){hit}, mean alpha {ga:+.2%}")
    same = [r for r in rows if r["ticker"] == ticker]
    if same:
        sn, sd, sr, sa = _stats(same)
        recent = ", ".join(f"{r['date']} {r['rating']} {r['alpha_return']:+.1%}" for r in same[-5:])
        lines.append(f"- On {ticker}: {_hit(sd, sr)}, mean alpha {sa:+.2%}; latest: {recent}")
    bullish = [r for r in rows if DIRECTION.get(r["rating"], 0) > 0]
    _, bd, br, _ = _stats(bullish)
    bearish = [r for r in rows if DIRECTION.get(r["rating"], 0) < 0]
    _, ed, er, _ = _stats(bearish)
    if n < MIN_SAMPLE:
        lines.append(f"- The sample is small (under {MIN_SAMPLE}): treat these rates as noise, not skill.")
    else:
        if bd >= 10 and br / bd < 0.5:
            lines.append("- Your bullish calls have been wrong more often than right: require stronger, "
                         "more specific evidence before a Buy or Overweight.")
        if ed >= 10 and er / ed < 0.5:
            lines.append("- Your bearish calls have been wrong more often than right: require stronger, "
                         "more specific evidence before an Underweight or Sell.")
    return "\n".join(lines)


# --- beliefs -------------------------------------------------------------------------

def beliefs_path(config: dict) -> Path | None:
    log = config.get("memory_log_path")
    return Path(log).expanduser().with_name("beliefs.json") if log else None


def _versions(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def beliefs_as_of(config: dict, as_of: str | None = None) -> dict | None:
    """The latest beliefs whose evidence ran to ``as_of`` or earlier (latest of all when None)."""
    versions = [v for v in _versions(beliefs_path(config))
                if as_of is None or v.get("as_of", "9999") <= as_of]
    return versions[-1] if versions else None


def render_beliefs(version: dict | None) -> str:
    if not version or not version.get("beliefs"):
        return ""
    return (f"Standing beliefs learned from your settled decisions (revised {version['created'][:10]}, "
            f"from {version['based_on']} outcomes; weigh each by its evidence, and set one aside "
            f"when this case differs):\n{version['beliefs'].strip()}")


def _case(e: dict) -> str:
    reflection = " ".join(str(e.get("reflection") or "").split())[:600]
    return (f"- {e['date']} {e['ticker']} {e['rating']}: alpha {e['alpha_return']:+.1%} "
            f"(raw {e['raw_return']:+.1%}). Reflection: {reflection or '(none)'}")


def _prompt(current: str, right: list[dict], wrong: list[dict], flat: list[dict]) -> str:
    sections = []
    for title, rows in (("Calls whose direction was RIGHT", right), ("Calls whose direction was WRONG", wrong),
                        ("Hold calls", flat)):
        if rows:
            sections.append(f"{title}:\n" + "\n".join(_case(r) for r in rows))
    return (
        "You maintain the standing beliefs of a trading research team: short rules learned from its own "
        "settled decisions, read before every new analysis.\n\n"
        f"Current beliefs:\n{current.strip() or '(none yet)'}\n\n"
        + "\n\n".join(sections) + "\n\n"
        "Revise the beliefs. Contrast the right calls with the wrong ones and keep only rules the outcomes "
        "support: what kind of evidence preceded calls that worked, and what was overweighted in calls that "
        "failed. Merge duplicates, drop a rule the new outcomes contradict, and do not invent rules the cases "
        "do not show. Each outcome covers a short holding window, so prefer rules about evidence and timing "
        f"over rules about specific tickers.\n\nReturn at most {MAX_BELIEFS} lines, each exactly in the form\n"
        "- <rule, one sentence> (evidence: <k> right / <m> wrong)\n"
        "and nothing else."
    )


def update_beliefs(memory_log, llm, config: dict, every: int | None = None, window: int = 30) -> dict | None:
    """Revise the beliefs when ``every`` decisions have settled since the last revision.

    Returns the new version, or None when no revision was due or it failed.
    """
    every = int(every or config.get("belief_update_every") or 5)
    path = beliefs_path(config)
    if path is None or llm is None:
        return None
    rows = settled_rows(memory_log.load_entries())
    versions = _versions(path)
    # Keyed by decision, not counted: the log trims its oldest entries.
    seen = set(versions[-1].get("seen", [])) if versions else set()
    fresh = [r for r in rows if f"{r['date']}|{r['ticker']}" not in seen]
    if len(rows) < every or len(fresh) < every:
        return None
    recent = rows[-window:]
    right = [r for r in recent if DIRECTION.get(r["rating"], 0) and r["alpha_return"] * DIRECTION[r["rating"]] > 0]
    wrong = [r for r in recent if DIRECTION.get(r["rating"], 0) and r["alpha_return"] * DIRECTION[r["rating"]] <= 0]
    flat = [r for r in recent if not DIRECTION.get(r["rating"], 0)]
    current = versions[-1]["beliefs"] if versions else ""
    try:
        text = llm.invoke(_prompt(current, right, wrong, flat)).content
    except Exception as exc:
        logger.warning("Revising beliefs failed: %s", exc)
        return None
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip().startswith("-")][:MAX_BELIEFS]
    if not lines:
        logger.warning("Belief revision returned no rules; keeping the current ones")
        return None
    version = {
        "as_of": max(r.get("resolved") or r["date"] for r in rows),
        "created": datetime.now().isoformat(timespec="seconds"),
        "based_on": len(rows),
        "beliefs": "\n".join(lines),
        "seen": sorted(seen | {f"{r['date']}|{r['ticker']}" for r in rows}),
    }
    versions.append(version)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(versions, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return version


def learning_context(memory_log, config: dict, ticker: str, as_of: str | None = None) -> str:
    """Track record plus beliefs, for the Research Manager and Portfolio Manager; '' when empty."""
    parts = [track_record(memory_log.load_entries(), ticker, as_of),
             render_beliefs(beliefs_as_of(config, as_of))]
    return "\n\n".join(p for p in parts if p)
