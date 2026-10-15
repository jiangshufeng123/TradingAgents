"""A-share trading signals for the sentiment analyst: limit moves, 龙虎榜, margin, popularity."""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.dataflows.vendors import akshare_cn as cn
from tradingagents.dataflows.vendors.yahoo import ohlcv


def _prices(changes, start=10.0):
    closes = [start]
    for c in changes:
        closes.append(round(closes[-1] * (1 + c), 2))
    dates = pd.bdate_range(end="2026-10-14", periods=len(closes))
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": 1e6})


@pytest.fixture
def prices(monkeypatch):
    frame = _prices([0.01] * 15 + [-0.02, 0.10, 0.10, 0.0, 0.10, 0.10])
    monkeypatch.setattr(ohlcv, "load_ohlcv", lambda ticker, as_of, fill_gaps=True: frame)
    return frame


@pytest.mark.unit
def test_limit_moves_are_counted_from_prices(prices):
    lines = cn.limit_move_lines("603980.SS", "2026-10-14")
    text = "\n".join(lines)
    assert "主板" in text and "Limit-up closes: 4 (" in text and "limit-down closes: 0" in text
    assert "2 consecutive limit-up" in text
    assert cn.limit_move_lines("0700.HK", "2026-10-14") == []


@pytest.mark.unit
def test_lhb_keeps_the_listing_and_drops_later_rows_and_after_returns(prices, monkeypatch):
    df = pd.DataFrame({
        "代码": ["603980", "603980", "600519"], "上榜日": ["2026-10-09", "2026-10-20", "2026-10-09"],
        "上榜原因": ["日涨幅偏离值达7%", "x", "y"], "涨跌幅": [10.0, 5.0, 1.0],
        "龙虎榜净买额": [1.2e8, 9e9, 3e8], "净买额占总成交比": [8.5, 1, 1], "换手率": [12.0, 1, 1],
        "上榜后5日": [25.0, 1, 1],
    })
    asked = {}

    def fake_call(func, **kw):
        asked.update(kw)
        return df

    monkeypatch.setattr(cn, "call", fake_call)
    text = "\n".join(cn.lhb_lines("603980.SH", "2026-10-14"))
    sessions = sorted(pd.to_datetime(prices["Date"]))
    assert asked["start_date"] == f"{sessions[-20]:%Y%m%d}"          # same span as the limit moves
    assert "2026-10-09" in text and "日涨幅偏离值达7%" in text and "1.20" in text
    assert "2026-10-20" not in text and "25.0" not in text      # after the date, and look-ahead returns
    monkeypatch.setattr(cn, "call", lambda func, **kw: df.iloc[0:0])
    assert "not listed" in "\n".join(cn.lhb_lines("603980.SS", "2026-10-14"))


@pytest.mark.unit
def test_margin_uses_the_day_before_the_analysis_and_compares_back(prices, monkeypatch):
    days = sorted(pd.to_datetime(prices["Date"]))
    seen = []

    def fake_call(func, **kw):
        seen.append((func, kw["date"]))
        balance = 2.2e8 if kw["date"] == f"{days[-2]:%Y%m%d}" else 2.0e8
        return pd.DataFrame({"标的证券代码": ["603980"], "融资余额": [balance], "融资买入额": [3e7],
                             "融券余量": [1000]})

    monkeypatch.setattr(cn, "call", fake_call)
    text = "\n".join(cn.margin_lines("603980.SS", f"{days[-1]:%Y-%m-%d}"))
    assert seen[0] == ("stock_margin_detail_sse", f"{days[-2]:%Y%m%d}")   # T+1 publication
    assert seen[1][1] == f"{days[-7]:%Y%m%d}"
    assert "2.20" in text and "+10.0%" in text


@pytest.mark.unit
def test_shenzhen_margin_reads_the_szse_table(prices, monkeypatch):
    monkeypatch.setattr(cn, "call", lambda func, **kw: pd.DataFrame(
        {"证券代码": ["000960"], "融资余额": [5e8], "融资买入额": [1e7], "融券余量": [0]})
        if func == "stock_margin_detail_szse" else pd.DataFrame())
    assert "5.00" in "\n".join(cn.margin_lines("000960.SZ", "2026-10-14"))


@pytest.mark.unit
def test_popularity_rank_trend(monkeypatch):
    df = pd.DataFrame({"时间": pd.date_range("2026-10-01", periods=12).strftime("%Y-%m-%d"),
                       "排名": list(range(500, 380, -10)), "新晋粉丝": [0.6] * 12, "铁杆粉丝": [0.4] * 12})
    monkeypatch.setattr(cn, "call", lambda func, **kw: df)
    text = "\n".join(cn.hot_rank_lines("000960.SZ", "2026-10-10"))
    assert "Latest rank 410" in text and "rising attention" in text and "2026-10-10" in text


@pytest.mark.unit
def test_one_failing_source_does_not_hide_the_others(prices, monkeypatch):
    def fake_call(func, **kw):
        raise cn.VendorUnavailableError("down")

    monkeypatch.setattr(cn, "call", fake_call)
    text = cn.a_share_microstructure("603980.SS", "2026-10-14")
    assert "Limit-up closes" in text and "<unavailable" in text
    assert cn.a_share_microstructure("NVDA", "2026-10-14") == ""
