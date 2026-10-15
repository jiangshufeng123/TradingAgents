"""One spelling per listing.

What a user types (``603980.SH``, ``603980``, ``SH603980``, ``700.HK``, ``00700``) becomes the
symbol the analysis records (``603980.SS``, ``0700.HK``): jobs, the watchlist, holdings and the
results they are compared with then all use the same spelling.
"""
from __future__ import annotations

import re

from tradingagents.dataflows.symbols import normalize_symbol

_PREFIXED = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_SUFFIXED = re.compile(r"^(\d{6})(SH|SZ|BJ|SS)$")
_EXCHANGE = {"SH": "SS", "SS": "SS", "SZ": "SZ", "BJ": "BJ"}


def a_share_exchange(code: str) -> str | None:
    """The Yahoo suffix of a bare 6-digit A-share code (stocks, funds, Beijing listings)."""
    if not re.fullmatch(r"\d{6}", code):
        return None
    if code.startswith("92"):                 # Beijing's new 920 codes
        return "BJ"
    if code[0] in "659":                      # 600/601/603/605/688 stocks, 5xxxxx funds, 900 B shares
        return "SS"
    if code[0] in "0123":                     # 000/001/002/003/300/301 stocks, 1xxxxx funds, 200 B shares
        return "SZ"
    if code[0] in "48":                       # 43/83/87/88 Beijing listings
        return "BJ"
    return None


def canonical(ticker) -> str:
    """The analysis's spelling of ``ticker``; an unrecognised one comes back upper-cased."""
    raw = str(ticker or "").strip().upper()
    if not raw:
        return raw
    if m := _PREFIXED.match(raw):
        raw = f"{m.group(2)}.{_EXCHANGE[m.group(1)]}"
    elif m := _SUFFIXED.match(raw):
        raw = f"{m.group(1)}.{_EXCHANGE[m.group(2)]}"
    elif suffix := a_share_exchange(raw):
        raw = f"{raw}.{suffix}"
    elif re.fullmatch(r"\d{1,5}", raw):       # a bare Hong Kong code: 700, 00700
        raw = f"{raw}.HK"
    try:
        return str(normalize_symbol(raw)).upper()
    except Exception:
        return raw


def canonicalize_stored(s) -> int:
    """Respell watchlist entries and holdings saved before tickers were made canonical
    (603980.SH -> 603980.SS). A respelling that would duplicate an entry the user already has
    under the canonical spelling is left as it is. Returns the number of rows changed."""
    from sqlalchemy import select

    from server.models import Holding, WatchlistItem

    changed = 0
    for model in (WatchlistItem, Holding):
        rows = list(s.scalars(select(model)))
        taken = {(r.user_id, r.ticker) for r in rows}
        for r in rows:
            new = canonical(r.ticker)
            if new and new != r.ticker and (r.user_id, new) not in taken:
                taken.discard((r.user_id, r.ticker))
                taken.add((r.user_id, new))
                r.ticker = new
                changed += 1
    s.flush()
    return changed
