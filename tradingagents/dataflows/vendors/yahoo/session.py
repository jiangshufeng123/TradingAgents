"""The trading session behind a symbol's newest daily bar: is it still open?

During market hours Yahoo serves today's daily bar with the price so far as its
``Close`` and the volume so far as its ``Volume``. Read as a finished day, that
bar turns half a session's volume into "a sharp drop in volume" and an intraday
print into a closing level. Yahoo's history metadata names the regular session
it is in (``currentTradingPeriod.regular``); while that session has not ended,
its bar is dropped so a run sees only settled days.

The session comes from the same request as the prices, so it costs no extra
call. A cached download keeps its session beside it in a small JSON file.
"""

from __future__ import annotations

import json
import logging
import os
import time

import pandas as pd

from tradingagents.dataflows.config import get_config

logger = logging.getLogger(__name__)


def session_from_metadata(metadata) -> dict | None:
    """``{"date": "YYYY-MM-DD", "end": epoch_seconds}`` for the regular session, or None."""
    try:
        regular = metadata["currentTradingPeriod"]["regular"]
        start, end = regular["start"], regular["end"]
    except (KeyError, TypeError, IndexError):
        return None
    try:
        tz = metadata.get("exchangeTimezoneName") if hasattr(metadata, "get") else None
        start = _timestamp(start, tz)
        end = _timestamp(end, tz)
    except (ValueError, TypeError):
        return None
    return {"date": start.strftime("%Y-%m-%d"), "end": float(end.timestamp())}


def _timestamp(value, tz) -> pd.Timestamp:
    """A session bound as a Timestamp in the exchange's own time zone."""
    if isinstance(value, (int, float)):
        value = pd.Timestamp(value, unit="s", tz="UTC")
    value = pd.Timestamp(value)
    if value.tzinfo is None:
        value = value.tz_localize("UTC")
    return value.tz_convert(tz) if tz else value


def fetch_session(ticker) -> dict | None:
    """The session of a ``yf.Ticker`` whose ``history()`` has just been read."""
    try:
        return session_from_metadata(ticker.history_metadata)
    except Exception as exc:   # metadata is a convenience; prices never wait on it
        logger.debug("No trading-session metadata: %s", exc)
        return None


def session_file(data_file: str) -> str:
    return f"{os.path.splitext(data_file)[0]}-session.json"


def save_session(data_file: str, session: dict | None) -> None:
    path = session_file(data_file)
    try:
        if session is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f)
    except OSError as exc:
        logger.debug("Could not write %s: %s", path, exc)


def load_session(data_file: str) -> dict | None:
    try:
        with open(session_file(data_file), encoding="utf-8") as f:
            session = json.load(f)
        return session if {"date", "end"} <= set(session) else None
    except (OSError, ValueError, TypeError):
        return None


def session_closed_since(session: dict | None, written_at: float, now: float | None = None) -> bool:
    """Whether a session that was open when a cache was written has ended since."""
    now = time.time() if now is None else now
    return bool(session) and written_at < session["end"] <= now


def enabled() -> bool:
    return bool(get_config().get("drop_unsettled_bar", True))


def is_open(session: dict | None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return bool(session) and now < session["end"]


def drop_unsettled_bar(data: pd.DataFrame, session: dict | None, symbol: str,
                       date_column: str | None = "Date", now: float | None = None) -> pd.DataFrame:
    """``data`` without its newest row when that row is a session still trading.

    ``date_column`` names the column holding bar dates; ``None`` reads the index.
    """
    if data is None or data.empty or not enabled() or not is_open(session, now):
        return data
    dates = data.index if date_column is None else data[date_column]
    last = pd.Timestamp(dates[-1] if date_column is None else dates.iloc[-1])
    if last.strftime("%Y-%m-%d") != session["date"]:
        return data
    logger.info("%s: dropping the %s bar, its session is still trading (closes %s UTC)",
                symbol, session["date"],
                pd.Timestamp(session["end"], unit="s").strftime("%Y-%m-%d %H:%M"))
    return data.iloc[:-1]
