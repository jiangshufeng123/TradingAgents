"""Refreshing the price cache: the last weeks only, unless the history was re-adjusted; and
when Yahoo cannot be reached, the cached prices (extended from the domestic sources for
A-shares and Hong Kong) rather than no prices."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.dataflows.vendors import akshare_cn
from tradingagents.dataflows.vendors.yahoo import ohlcv

NOW = pd.Timestamp("2026-10-16 18:00")


def _bars(start, end, scale=1.0):
    days = pd.bdate_range(start, end)
    close = [round(10 + i * 0.1, 4) * scale for i in range(len(days))]
    return pd.DataFrame({"Date": days, "Open": close, "High": close, "Low": close, "Close": close,
                         "Volume": 1000.0})


def _setup(tmp_path, monkeypatch, symbol="600519.SS"):
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: NOW))
    cache = tmp_path / f"{symbol}-YFin-data.csv"
    full = _bars("2026-01-02", "2026-10-16")
    full[full["Date"] <= "2026-10-14"].to_csv(cache, index=False)     # cached two days ago
    t = (NOW - pd.Timedelta(days=2)).to_pydatetime().timestamp()
    os.utime(cache, (t, t))
    return cache, full


def _yahoo(monkeypatch, frame_for):
    calls = []

    def history(start=None, end=None, **k):
        calls.append(start)
        return frame_for(start).set_index("Date")
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: SimpleNamespace(history=history, history_metadata={}))
    return calls


@pytest.mark.unit
def test_a_refresh_downloads_the_last_weeks_and_keeps_the_years(tmp_path, monkeypatch):
    cache, full = _setup(tmp_path, monkeypatch)
    calls = _yahoo(monkeypatch, lambda start: full[full["Date"] >= start])
    out = ohlcv.load_ohlcv("600519.SS", "2026-10-16")
    assert len(calls) == 1 and calls[0] == "2026-09-23"           # 21 days before the last cached bar
    assert out["Date"].iloc[0] == pd.Timestamp("2026-01-02") and out["Date"].iloc[-1] == pd.Timestamp("2026-10-16")
    assert len(out) == len(full) and out["Date"].is_unique
    assert len(pd.read_csv(cache)) == len(full)                       # the joined history is what is cached


@pytest.mark.unit
def test_a_re_adjusted_history_is_downloaded_whole(tmp_path, monkeypatch):
    """After a dividend every adjusted price before it changes: joining would mix two bases."""
    cache, full = _setup(tmp_path, monkeypatch)
    adjusted = _bars("2026-01-02", "2026-10-16", scale=0.97)
    calls = _yahoo(monkeypatch, lambda start: adjusted[adjusted["Date"] >= start])
    out = ohlcv.load_ohlcv("600519.SS", "2026-10-16")
    assert len(calls) == 2 and calls[1] < "2022-01-01"                # the recent weeks, then 5 years
    assert out["Close"].iloc[0] == pytest.approx(10 * 0.97)


@pytest.mark.unit
def test_yahoo_unreachable_uses_the_cache_and_the_domestic_sessions_since(tmp_path, monkeypatch):
    cache, full = _setup(tmp_path, monkeypatch)

    def down(*a, **k):
        raise ConnectionError("blocked")
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: SimpleNamespace(history=down, history_metadata={}))
    asked = []

    def domestic(ticker, start, end):
        asked.append((ticker, pd.Timestamp(start).date()))
        return full[(full["Date"] >= start) & (full["Date"] <= end)], "Sina"
    monkeypatch.setattr(akshare_cn, "daily_bars", domestic)
    out = ohlcv.load_ohlcv("600519.SS", "2026-10-16")
    assert asked == [("600519.SS", pd.Timestamp("2026-10-15").date())]
    assert out["Date"].iloc[-1] == pd.Timestamp("2026-10-16")
    assert len(pd.read_csv(cache)) < len(full)                        # nothing written to the cache


@pytest.mark.unit
def test_yahoo_unreachable_and_nothing_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: NOW))

    def down(*a, **k):
        raise ConnectionError("blocked")
    monkeypatch.setattr(ohlcv.yf, "Ticker", lambda s: SimpleNamespace(history=down, history_metadata={}))
    full = _bars("2025-01-02", "2026-10-16")
    monkeypatch.setattr(akshare_cn, "daily_bars", lambda t, s, e: (full, "Tushare"))
    assert ohlcv.load_ohlcv("000960.SZ", "2026-10-16")["Close"].iloc[-1] == full["Close"].iloc[-1]
    from tradingagents.dataflows.errors import VendorUnavailableError

    with pytest.raises(VendorUnavailableError):                       # no domestic source for a US listing
        ohlcv.load_ohlcv("NVDA", "2026-10-16")


@pytest.mark.unit
def test_prefetch_fetches_every_ticker_and_reports_the_failures(monkeypatch):
    from typer.testing import CliRunner

    from cli import main as cli_main
    from tradingagents import names
    from tradingagents.dataflows.vendors.yahoo import ohlcv as yahoo_ohlcv

    seen = []

    def load(ticker, as_of):
        seen.append((ticker, as_of))
        if ticker == "BAD.SS":
            raise RuntimeError("no source")
        return _bars("2026-10-01", "2026-10-15")
    monkeypatch.setattr(yahoo_ohlcv, "load_ohlcv", load)
    monkeypatch.setattr(names, "names_for", lambda t: {})
    failed = cli_main._prefetch(["600519.SS", "BAD.SS", "0700.HK"], "2026-10-16")
    assert sorted(seen) == [("0700.HK", "2026-10-16"), ("600519.SS", "2026-10-16"), ("BAD.SS", "2026-10-16")]
    assert [t for t, _ in failed] == ["BAD.SS"]

    r = CliRunner().invoke(cli_main.app, ["datasource", "prefetch", "600519.SS,BAD.SS", "--date", "2026-10-16"],
                           env={"COLUMNS": "200"})
    assert r.exit_code == 1 and "1/2 只股票的价格已就绪" in r.output and "BAD.SS 取价格失败" in r.output

    monkeypatch.setenv("TRADINGAGENTS_SELFCHECK", "1")                # the self-check stays offline
    seen.clear()
    assert cli_main._prefetch(["600519.SS"], "2026-10-16") == [] and seen == []
