from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import WatchlistItem
from server.tickers import canonical


def _key(ticker: str) -> str:
    return canonical(ticker)


def items(s: Session, user_id: int) -> list[WatchlistItem]:
    return list(s.scalars(select(WatchlistItem).where(WatchlistItem.user_id == user_id)
                          .order_by(WatchlistItem.created_at, WatchlistItem.id)))


def tickers(s: Session, user_id: int) -> list[str]:
    return [i.ticker for i in items(s, user_id)]


def add(s: Session, user_id: int, tickers_: list[str], note: str = "", source: str = "manual") -> list[str]:
    """Add tickers the user does not watch yet (spellings of one listing count once)."""
    have = {_key(i.ticker) for i in items(s, user_id)}
    added = []
    for t in tickers_:
        t = t.strip().upper()
        if t and _key(t) not in have:
            have.add(_key(t))
            s.add(WatchlistItem(user_id=user_id, ticker=t, note=note[:200], source=source))
            added.append(t)
    s.flush()
    return added


def remove(s: Session, user_id: int, tickers_: list[str]) -> list[str]:
    keys = {_key(t) for t in tickers_}
    gone = []
    for i in items(s, user_id):
        if _key(i.ticker) in keys:
            gone.append(i.ticker)
            s.delete(i)
    s.flush()
    return gone
