"""The watchlist that `tradingagents daily` analyzes: .env plus a file you curate.

``TRADINGAGENTS_WATCHLIST`` in .env holds the names you set by hand. Names you
pick from a screen go to a plain text file (``watchlist_file``, default
``watchlist.txt`` in the project folder), one ticker per line with a note on
where it came from. Nothing is added to it except by your choice:
`tradingagents screen` asks which results to add, and
`tradingagents watchlist add/remove` edits it directly.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tradingagents.dataflows.symbols import normalize_symbol


def path(config: dict) -> Path:
    if config.get("watchlist_file"):
        return Path(config["watchlist_file"]).expanduser()
    return Path(__file__).resolve().parents[1] / "watchlist.txt"


def _key(ticker: str) -> str:
    try:
        return normalize_symbol(ticker.strip()).upper()
    except Exception:
        return ticker.strip().upper()


def from_env(config: dict) -> list[str]:
    text = config.get("watchlist") or ""
    return list(dict.fromkeys(t.strip().upper() for t in str(text).replace(";", ",").split(",") if t.strip()))


def entries(config: dict) -> list[tuple[str, str]]:
    """(ticker, note) for every line of the file."""
    p = path(config)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ticker, _, note = line.partition("#")
        if ticker.strip():
            out.append((ticker.strip().upper(), note.strip()))
    return out


def tickers(config: dict) -> list[str]:
    """.env names first, then the file's, each listing once."""
    seen, out = set(), []
    for t in from_env(config) + [t for t, _ in entries(config)]:
        if _key(t) not in seen:
            seen.add(_key(t))
            out.append(t)
    return out


def _write(config: dict, rows: list[tuple[str, str]]) -> None:
    p = path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Tickers `tradingagents daily` analyzes in addition to TRADINGAGENTS_WATCHLIST.",
             "# One per line; text after # is a note. Edit freely."]
    lines += [f"{t}  # {note}" if note else t for t, note in rows]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add(config: dict, new: list[str], note: str = "") -> list[str]:
    """Add tickers not already watched (in .env or the file); return those added."""
    rows = entries(config)
    watched = {_key(t) for t in from_env(config)} | {_key(t) for t, _ in rows}
    added = []
    stamp = f"{note} ({date.today():%Y-%m-%d})" if note else f"added {date.today():%Y-%m-%d}"
    for t in new:
        if t and _key(t) not in watched:
            watched.add(_key(t))
            rows.append((t.strip().upper(), stamp))
            added.append(t.strip().upper())
    if added:
        _write(config, rows)
    return added


def remove(config: dict, gone: list[str]) -> list[str]:
    """Remove tickers from the file (not from .env); return those removed."""
    keys = {_key(t) for t in gone}
    rows = entries(config)
    kept = [(t, n) for t, n in rows if _key(t) not in keys]
    removed = [t for t, _ in rows if _key(t) in keys]
    if removed:
        _write(config, kept)
    return removed


def parse_picks(text: str, count: int) -> list[int]:
    """'1,3, 5-7' / 'all' / '' -> zero-based row indexes; out-of-range numbers raise."""
    text = (text or "").strip().lower().replace("，", ",").replace("、", ",")
    if not text or text in ("n", "no", "none", "0"):
        return []
    if text in ("all", "a", "全部"):
        return list(range(count))
    picks = []
    for part in [p.strip() for p in text.replace(" ", ",").split(",") if p.strip()]:
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            nums = range(min(lo, hi), max(lo, hi) + 1)
        else:
            nums = [int(part)]
        for n in nums:
            if not 1 <= n <= count:
                raise ValueError(f"{n} is not a row number (1-{count})")
            if n - 1 not in picks:
                picks.append(n - 1)
    return picks
