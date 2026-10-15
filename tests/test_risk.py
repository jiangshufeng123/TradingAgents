"""Position size and risk figures computed in code (tradingagents/risk.py)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradingagents import risk
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.portfolio import PortfolioContext

CONFIG = {"risk_per_trade": 0.01, "atr_stop_mult": 2.0, "max_position_weight": 0.2,
          "max_portfolio_var": 0.03, "lot_sizes": "6616.HK=500"}


def _frame(close: float, day_range: float, days: int = 300, seed: int = 0, drift: float = 0.0):
    """Bars whose high-low range is ``day_range`` (so ATR -> day_range) around a noisy close."""
    rng = np.random.default_rng(seed)
    closes = close * np.exp(np.cumsum(rng.normal(drift, 0.01, days)))
    closes = closes / closes[-1] * close
    dates = pd.bdate_range(end="2026-10-14", periods=days)
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes + day_range / 2,
                         "Low": closes - day_range / 2, "Close": closes, "Volume": 1e6})


@pytest.fixture
def prices(monkeypatch):
    frames = {
        "NVDA": _frame(100.0, 4.0, seed=1),          # ATR ~4, USD
        "603980.SS": _frame(20.0, 0.6, seed=2),      # ATR ~0.6, CNY
        "6616.HK": _frame(5.0, 0.2, seed=3),
        "USDCNY=X": _frame(7.0, 0.0, seed=4),
        "HKDCNY=X": _frame(0.9, 0.0, seed=5),
    }
    for name in ("USDCNY=X", "HKDCNY=X"):
        frames[name]["Close"] = frames[name]["Close"].iloc[-1]

    def load(ticker, as_of):
        ticker = normalize_symbol(ticker)       # as load_ohlcv does
        if ticker not in frames:
            raise ValueError(f"no data for {ticker}")
        return frames[ticker]

    monkeypatch.setattr(risk, "_load", load)
    risk._FX.clear()
    return frames


@pytest.mark.unit
def test_atr_matches_a_constant_range():
    assert risk.atr(_frame(50.0, 2.0)) == pytest.approx(2.0, rel=0.05)


@pytest.mark.unit
def test_currencies_boards_and_lots():
    assert risk.currency_of("603980.SH") == "CNY" and risk.currency_of("0700.HK") == "HKD"
    assert risk.currency_of("NVDA") == "USD"
    assert risk.a_share_board("688981.SS") == ("科创板", 0.20)
    assert risk.a_share_board("300750.SZ") == ("创业板", 0.20)
    assert risk.a_share_board("600519.SS") == ("主板", 0.10)
    assert risk.a_share_board("NVDA") is None
    assert risk.lot_size("600519.SS", {}) == (100, True)
    assert risk.lot_size("6616.HK", CONFIG) == (500, True)


@pytest.mark.unit
def test_without_a_portfolio_a_buy_is_a_risk_sized_weight(prices):
    s = risk.size_position("NVDA", "Buy", "2026-10-14", None, CONFIG)
    # 1% risk / (2 x ATR 4 on a 100 close = 8%) = 12.5% of equity, under the 20% cap
    assert s.full_weight == pytest.approx(0.125, rel=0.05)
    assert s.target_weight == pytest.approx(s.full_weight)
    assert s.stop == pytest.approx(100 - 2 * s.atr)
    assert risk.size_position("NVDA", "Overweight", "2026-10-14", None, CONFIG).target_weight == \
        pytest.approx(s.full_weight / 2)
    assert risk.size_position("NVDA", "Sell", "2026-10-14", None, CONFIG).action == "exit / do not open"
    assert any("No portfolio" in w for w in s.warnings)


@pytest.mark.unit
def test_a_buy_with_a_book_gives_shares_in_board_lots(prices):
    book = PortfolioContext.model_validate({"currency": "CNY", "cash": 100_000, "positions": []})
    s = risk.size_position("603980.SH", "Buy", "2026-10-14", book, CONFIG)
    # full weight = 1% x 20 / (2 x 0.6) = 16.7%; of 100,000 CNY at 20 CNY = 833 shares -> 800
    assert s.trade_qty == 800 and s.target_qty == 800
    assert s.action == "BUY 800"
    assert s.cash_after == pytest.approx(100_000 - 800 * 20.0)
    assert s.risk_at_stop == pytest.approx(800 * 2 * s.atr / 100_000)
    assert any("T+1" in w for w in s.warnings)


@pytest.mark.unit
def test_foreign_listings_are_valued_in_the_base_currency(prices):
    book = PortfolioContext.model_validate({"currency": "CNY", "cash": 70_000,
                                            "positions": [{"ticker": "NVDA", "quantity": 100}]})
    # equity = 70,000 + 100 x 100 USD x 7 = 140,000 CNY; NVDA is half of it
    s = risk.size_position("NVDA", "Hold", "2026-10-14", book, CONFIG)
    assert s.equity == pytest.approx(140_000)
    assert s.current_weight == pytest.approx(0.5)
    # Above the 20% cap (40 shares = 0.2 x 140,000 / 700): flagged, not sold
    assert s.trade_qty == 0 and s.target_qty == 100
    assert any("selling 60" in w for w in s.warnings)
    buy = risk.size_position("NVDA", "Buy", "2026-10-14", book, CONFIG)
    assert buy.trade_qty == 0 and buy.action == "no trade"


@pytest.mark.unit
def test_bullish_calls_never_sell_and_bearish_ones_cut(prices):
    book = PortfolioContext.model_validate({"currency": "CNY", "cash": 1_000_000,
                                            "positions": [{"ticker": "603980.SS", "quantity": 5_000}]})
    over = risk.size_position("603980.SS", "Overweight", "2026-10-14", book, CONFIG)
    assert over.trade_qty == 0                       # already above half the full size
    under = risk.size_position("603980.SS", "Underweight", "2026-10-14", book, CONFIG)
    assert under.trade_qty == -2_500
    sell = risk.size_position("603980.SS", "Sell", "2026-10-14", book, CONFIG)
    assert sell.trade_qty == -5_000 and sell.target_qty == 0


@pytest.mark.unit
def test_cash_limits_a_buy(prices):
    book = PortfolioContext.model_validate({"currency": "CNY", "cash": 5_000,
                                            "positions": [{"ticker": "NVDA", "quantity": 100}]})
    s = risk.size_position("603980.SS", "Buy", "2026-10-14", book, CONFIG)
    assert s.trade_qty == 200                       # 5,000 / 20 = 250 -> 200 in lots
    assert any("cash" in w for w in s.warnings)


@pytest.mark.unit
def test_hk_lots_from_the_settings(prices):
    book = PortfolioContext.model_validate({"currency": "CNY", "cash": 100_000, "positions": []})
    s = risk.size_position("6616.HK", "Buy", "2026-10-14", book, CONFIG)
    assert s.trade_qty % 500 == 0 and s.trade_qty > 0


@pytest.mark.unit
def test_book_risk_and_correlation(prices):
    book = risk.build_book(PortfolioContext.model_validate(
        {"currency": "CNY", "cash": 50_000, "positions": [{"ticker": "NVDA", "quantity": 100},
                                                          {"ticker": "603980.SS", "quantity": 1_000}]}),
        "2026-10-14")
    r = risk.book_risk(book, 2.0)
    assert r.equity == pytest.approx(50_000 + 70_000 + 20_000)
    assert r.var_95 > 0 and r.cvar_95 >= r.var_95 and r.vol > 0
    assert math.isclose(sum(r.weights.values()), 90_000 / 140_000, rel_tol=1e-6)
    corr = risk.correlations(risk.instrument_metrics("NVDA", "2026-10-14"), book)
    assert set(corr) == {"603980.SS"} and -1 <= corr["603980.SS"] <= 1


@pytest.mark.unit
def test_the_context_block_carries_the_figures(prices):
    book = PortfolioContext.model_validate({"currency": "CNY", "cash": 100_000,
                                            "positions": [{"ticker": "NVDA", "quantity": 10}]})
    text = risk.context_block("603980.SH", "2026-10-14", book, CONFIG)
    assert "ATR(14)" in text and "Volatility stop" in text and "主板" in text
    assert "Book: equity" in text and "VaR" in text and "correlation" in text
    assert risk.context_block("UNKNOWN", "2026-10-14", None, CONFIG) == ""


@pytest.mark.unit
def test_the_report_section_and_summary(prices):
    book = PortfolioContext.model_validate({"currency": "CNY", "cash": 100_000, "positions": []})
    s = risk.size_position("603980.SS", "Buy", "2026-10-14", book, CONFIG)
    md = risk.render_markdown(s)
    assert "**BUY 800**" in md and "Loss at stop" in md and "Warnings" in md
    assert risk.summary_line(s).startswith("603980.SS: Buy → BUY 800")


@pytest.mark.unit
def test_the_agents_see_the_risk_block_after_the_portfolio():
    from tradingagents.agents.context import get_portfolio_context_from_state

    text = get_portfolio_context_from_state({"portfolio_context": "", "risk_context": "Computed risk figures"})
    assert "not provided" in text and text.endswith("Computed risk figures")


@pytest.mark.unit
def test_portfolio_matches_exchange_spellings():
    book = PortfolioContext.model_validate({"positions": [{"ticker": "603980.SH", "quantity": 100}]})
    assert book.position_in("603980.SS").quantity == 100


@pytest.mark.unit
def test_the_report_gets_a_sizing_section(tmp_path):
    from tradingagents.reporting import write_report_tree

    out = write_report_tree({"final_trade_decision": "**Rating**: Buy"}, "NVDA", tmp_path, html=False,
                            sizing_markdown="| Action | **BUY 10** |")
    assert (tmp_path / "7_sizing" / "sizing.md").exists()
    assert "VII. Position Sizing (computed)" in out.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_risk_command_prints_figures_without_a_model(prices, monkeypatch):
    from typer.testing import CliRunner

    from cli.main import app

    result = CliRunner().invoke(app, ["risk", "603980.SS", "--rating", "buy", "--date", "2026-10-14"])
    assert result.exit_code == 0, result.output
    assert "ATR(14)" in result.output and "If the rating were Buy" in result.output


@pytest.mark.unit
def test_hk_board_lots_are_looked_up_once_and_cached(tmp_path, monkeypatch):
    from tradingagents.dataflows.vendors import akshare_cn

    calls = []

    def lookup(ticker):
        calls.append(ticker)
        return (500, "East Money") if ticker == "0700.HK" else None

    monkeypatch.setattr(akshare_cn, "hk_board_lot", lookup)
    config = {"data_cache_dir": str(tmp_path)}
    assert risk.lot_size("0700.HK", config) == (500, True)
    assert risk.lot_size("700.HK", config) == (500, True)          # cached, same listing
    assert len(calls) == 1
    assert (tmp_path / "hk_board_lots.json").exists()
    assert risk.lot_size("9999.HK", config) == (1, False)           # nobody knows it
    assert risk.lot_size("0700.HK", {**config, "lot_sizes": "0700.HK=100"}) == (100, True)


@pytest.mark.unit
def test_hk_board_lot_falls_back_to_the_hkex_list(monkeypatch):
    from tradingagents.dataflows.vendors import akshare_cn

    def down(*a, **k):
        raise RuntimeError("East Money down")

    monkeypatch.setattr(akshare_cn, "call", down)
    monkeypatch.setattr(akshare_cn, "_hkex_board_lots", lambda: {"06616": 1000})
    assert akshare_cn.hk_board_lot("6616.HK") == (1000, "HKEX")
    assert akshare_cn.hk_board_lot("0005.HK") is None


@pytest.mark.unit
def test_east_money_board_lot_is_read_first(monkeypatch):
    from tradingagents.dataflows.vendors import akshare_cn

    seen = []

    def fake_call(func, **kw):
        seen.append((func, kw))
        return pd.DataFrame({"证券代码": ["06616.HK"], "每手股数": [2000]})

    monkeypatch.setattr(akshare_cn, "call", fake_call)
    assert akshare_cn.hk_board_lot("6616.HK") == (2000, "East Money")
    assert seen == [("stock_hk_security_profile_em", {"symbol": "06616"})]


@pytest.mark.unit
def test_the_hkex_list_is_parsed_below_its_title_rows(monkeypatch):
    import io

    from tradingagents.dataflows.vendors import akshare_cn

    rows = [["List of Securities", None, None], ["Updated as at 14/10/2026", None, None],
            ["Stock Code", "Name of Securities", "Board Lot"],
            ["00700", "TENCENT", "100"], ["06616", "SOME CO", "1,000"], ["bad", "x", "y"]]
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, header=False, index=False)

    class Resp:
        content = buf.getvalue()

        def raise_for_status(self):
            pass

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    assert akshare_cn._hkex_board_lots() == {"00700": 100, "06616": 1000}
