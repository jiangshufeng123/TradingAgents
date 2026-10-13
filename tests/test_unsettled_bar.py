"""A daily bar whose trading session is still open is not used as a finished day."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import ohlcv, session

# Tuesday 2026-10-13, NYSE regular session 09:30-16:00 EDT = 13:30-20:00 UTC.
OPEN = pd.Timestamp("2026-10-13 13:30", tz="UTC").timestamp()
CLOSE = pd.Timestamp("2026-10-13 20:00", tz="UTC").timestamp()
MIDDAY = pd.Timestamp("2026-10-13 17:10", tz="UTC").timestamp()       # 01:10 Beijing next day
METADATA = {
    "exchangeTimezoneName": "America/New_York",
    "currentTradingPeriod": {"regular": {
        "start": pd.Timestamp(OPEN, unit="s", tz="UTC").tz_convert("America/New_York"),
        "end": pd.Timestamp(CLOSE, unit="s", tz="UTC").tz_convert("America/New_York"),
    }},
}


def _frame():
    return pd.DataFrame({
        "Date": pd.to_datetime(["2026-10-09", "2026-10-12", "2026-10-13"]),
        "Close": [229.28, 230.10, 226.81],
        "Volume": [84_600_000, 70_100_000, 42_232_787],
    })


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(session, "get_config", lambda: {"drop_unsettled_bar": True})


@pytest.mark.unit
def test_the_session_is_read_from_yahoo_metadata():
    assert session.session_from_metadata(METADATA) == {"date": "2026-10-13", "end": CLOSE}
    raw = {"currentTradingPeriod": {"regular": {"start": int(OPEN), "end": int(CLOSE)}},
           "exchangeTimezoneName": "America/New_York"}
    assert session.session_from_metadata(raw) == {"date": "2026-10-13", "end": CLOSE}
    assert session.session_from_metadata({}) is None


@pytest.mark.unit
def test_a_bar_still_trading_is_dropped_and_a_settled_one_kept():
    s = session.session_from_metadata(METADATA)
    mid = session.drop_unsettled_bar(_frame(), s, "NVDA", now=MIDDAY)
    assert mid["Date"].iloc[-1] == pd.Timestamp("2026-10-12")
    after = session.drop_unsettled_bar(_frame(), s, "NVDA", now=CLOSE + 60)
    assert after["Date"].iloc[-1] == pd.Timestamp("2026-10-13")


@pytest.mark.unit
def test_only_the_open_sessions_own_bar_is_dropped():
    # Before the open, the newest bar is yesterday's finished one.
    s = session.session_from_metadata(METADATA)
    frame = _frame().iloc[:-1]
    assert len(session.drop_unsettled_bar(frame, s, "NVDA", now=OPEN - 3600)) == 2


@pytest.mark.unit
def test_the_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(session, "get_config", lambda: {"drop_unsettled_bar": False})
    s = session.session_from_metadata(METADATA)
    assert len(session.drop_unsettled_bar(_frame(), s, "NVDA", now=MIDDAY)) == 3


@pytest.mark.unit
def test_an_index_dated_frame_is_handled_too():
    s = session.session_from_metadata(METADATA)
    frame = _frame().set_index("Date")
    frame.index = frame.index.tz_localize("America/New_York")
    assert len(session.drop_unsettled_bar(frame, s, "NVDA", date_column=None, now=MIDDAY)) == 2


@pytest.mark.unit
def test_load_ohlcv_drops_the_partial_bar_and_keeps_the_session_with_its_cache(tmp_path, monkeypatch):
    downloads = []

    class _Ticker:
        history_metadata = METADATA

        def __init__(self, symbol):
            pass

        def history(self, **k):
            downloads.append(1)
            return _frame().set_index("Date")

    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2026-10-14 01:10")))
    monkeypatch.setattr(ohlcv.yf, "Ticker", _Ticker)
    monkeypatch.setattr(session.time, "time", lambda: MIDDAY)

    data = ohlcv.load_ohlcv("NVDA", "2026-10-14", fill_gaps=False)
    assert data["Date"].iloc[-1] == pd.Timestamp("2026-10-12")
    assert (tmp_path / "NVDA-YFin-data-session.json").exists()

    # Served from the cache, the partial bar is still dropped.
    os.utime(tmp_path / "NVDA-YFin-data.csv", (pd.Timestamp("2026-10-14 01:10").timestamp(),) * 2)
    again = ohlcv.load_ohlcv("NVDA", "2026-10-14", fill_gaps=False)
    assert again["Date"].iloc[-1] == pd.Timestamp("2026-10-12") and len(downloads) == 1


@pytest.mark.unit
def test_a_cache_written_mid_session_is_refetched_once_the_session_closes(tmp_path):
    s = {"date": "2026-10-13", "end": CLOSE}
    assert session.session_closed_since(s, written_at=MIDDAY, now=CLOSE + 60)
    assert not session.session_closed_since(s, written_at=MIDDAY, now=CLOSE - 60)
    assert not session.session_closed_since(s, written_at=CLOSE + 10, now=CLOSE + 60)
    assert not session.session_closed_since(None, written_at=MIDDAY, now=CLOSE + 60)


@pytest.mark.unit
def test_a_ticker_without_metadata_keeps_every_bar(monkeypatch):
    assert session.fetch_session(SimpleNamespace()) is None
    assert len(session.drop_unsettled_bar(_frame(), None, "NVDA", now=MIDDAY)) == 3
