"""Paper trading: orders from sizings, filled at the next open by the market's rules."""

from __future__ import annotations

import csv

import pandas as pd
import pytest

from tradingagents import paper, risk
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.risk import Sizing


def _bars(rows):
    """rows: (date, open, high, low, close)."""
    return pd.DataFrame([{"Date": pd.Timestamp(d), "Open": o, "High": h, "Low": lo, "Close": c, "Volume": 1e6}
                         for d, o, h, lo, c in rows])


@pytest.fixture
def market(monkeypatch):
    frames = {}

    def bars(self, ticker, as_of):
        frame = frames[normalize_symbol(ticker)]
        return frame[frame["Date"] <= pd.Timestamp(as_of)].reset_index(drop=True)

    monkeypatch.setattr(paper.PaperAccount, "_bars", bars)
    monkeypatch.setattr(paper.PaperAccount, "_benchmark_return", lambda self, as_of: 0.01)
    monkeypatch.setattr(risk, "fx_rate", lambda a, b, as_of: 7.0 if (a, b) == ("USD", "CNY") else 1.0)
    return frames


@pytest.fixture
def account(tmp_path):
    return paper.PaperAccount({"paper_dir": str(tmp_path / "paper"), "paper_cash": 100_000})


def _sizing(ticker, rating, trade, stop=9.0, lot=100):
    return Sizing(ticker=normalize_symbol(ticker), rating=rating, close=10.0, currency="CNY", atr=0.5,
                  stop=stop, lot=lot, lot_known=True, trade_qty=trade)


@pytest.mark.unit
def test_fees_by_market():
    assert paper.fees_for("600519.SS", "BUY", 10_000) == 5.1            # commission minimum 5 + transfer
    assert paper.fees_for("600519.SS", "SELL", 100_000) == pytest.approx(25 + 50 + 1)
    assert paper.fees_for("0700.HK", "BUY", 100_000) > 100              # stamp duty both ways
    assert paper.fees_for("NVDA", "BUY", 10_000) == 0.0


@pytest.mark.unit
def test_a_buy_fills_at_the_next_open_and_t_plus_1_blocks_a_same_day_sale(market, account):
    market["603980.SS"] = _bars([("2026-10-14", 10, 10.2, 9.9, 10.0), ("2026-10-15", 10.1, 10.5, 10.0, 10.4),
                                 ("2026-10-16", 10.4, 10.6, 10.3, 10.5)])
    order = account.submit(_sizing("603980.SH", "Buy", 800), "2026-10-14")
    assert order["side"] == "BUY" and order["qty"] == 800
    assert account.update("2026-10-14") == []                       # next session not traded yet
    fills = account.update("2026-10-15")
    assert [(f.side, f.qty, f.price, f.date) for f in fills] == [("BUY", 800, 10.1, "2026-10-15")]
    assert account.state["cash"] == pytest.approx(100_000 - 8080 - paper.fees_for("603980.SS", "BUY", 8080))
    assert account.quantity("603980.SS") == 800
    # a sell decided on the buy day fills the next session, so T+1 is respected
    assert account._sellable("603980.SS", "2026-10-15") == 0
    assert account._sellable("603980.SS", "2026-10-16") == 800


@pytest.mark.unit
def test_limit_up_open_rejects_a_buy(market, account):
    market["603980.SS"] = _bars([("2026-10-14", 10, 10, 10, 10.0), ("2026-10-15", 11.0, 11.0, 11.0, 11.0)])
    account.submit(_sizing("603980.SS", "Buy", 800), "2026-10-14")
    fills = account.update("2026-10-15")
    assert fills[0].status == "rejected" and "limit-up" in fills[0].reason
    assert account.state["pending"] == [] and account.quantity("603980.SS") == 0


@pytest.mark.unit
def test_cash_shrinks_a_buy_to_whole_lots(market, tmp_path):
    acc = paper.PaperAccount({"paper_dir": str(tmp_path / "p"), "paper_cash": 2_500})
    market["000960.SZ"] = _bars([("2026-10-14", 10, 10, 10, 10.0), ("2026-10-15", 10, 10, 10, 10.0)])
    acc.submit(_sizing("000960.SZ", "Buy", 800), "2026-10-14")
    fill = acc.update("2026-10-15")[0]
    assert fill.status == "filled" and fill.qty == 200


@pytest.mark.unit
def test_a_stop_sells_at_the_stop_or_the_gap_open(market, account):
    market["603980.SS"] = _bars([("2026-10-14", 10, 10, 10, 10.0), ("2026-10-15", 10, 10.2, 9.8, 10.0),
                                 ("2026-10-16", 9.6, 9.7, 9.5, 9.6), ("2026-10-19", 9.5, 9.5, 9.0, 9.2)])
    account.submit(_sizing("603980.SS", "Buy", 1000, stop=9.7), "2026-10-14")
    account.update("2026-10-15")
    fills = account.update("2026-10-19")
    assert len(fills) == 1 and fills[0].side == "SELL" and fills[0].qty == 1000
    assert fills[0].price == 9.6 and fills[0].date == "2026-10-16"      # gapped below the stop
    assert "stop" in fills[0].reason and account.quantity("603980.SS") == 0


@pytest.mark.unit
def test_bullish_calls_ratchet_the_stop_and_sells_reduce(market, account):
    market["NVDA"] = _bars([("2026-10-14", 100, 100, 100, 100), ("2026-10-15", 100, 101, 99, 100),
                            ("2026-10-16", 102, 103, 101, 102)])
    account.submit(Sizing("NVDA", "Buy", 100.0, "USD", 3.0, 94.0, 1, True, trade_qty=50), "2026-10-14")
    account.update("2026-10-15")
    assert account.state["cash"] == pytest.approx(100_000 - 50 * 100 * 7)
    account.submit(Sizing("NVDA", "Hold", 100.0, "USD", 3.0, 96.0, 1, True, trade_qty=0), "2026-10-15")
    assert account.state["positions"]["NVDA"]["stop"] == 96.0
    account.submit(Sizing("NVDA", "Hold", 100.0, "USD", 3.0, 90.0, 1, True, trade_qty=0), "2026-10-15")
    assert account.state["positions"]["NVDA"]["stop"] == 96.0            # never lowered
    account.submit(Sizing("NVDA", "Underweight", 100.0, "USD", 3.0, 96.0, 1, True, trade_qty=-25), "2026-10-15")
    fill = account.update("2026-10-16")[0]
    assert (fill.side, fill.qty, fill.price) == ("SELL", 25, 102)        # US: no T+1


@pytest.mark.unit
def test_records_and_status(market, account):
    market["603980.SS"] = _bars([("2026-10-14", 10, 10, 10, 10.0), ("2026-10-15", 10, 10.5, 10, 10.5)])
    account.submit(_sizing("603980.SS", "Buy", 1000), "2026-10-14")
    account.update("2026-10-15")
    with open(account.dir / "trades.csv", encoding="utf-8-sig") as f:
        trades = list(csv.DictReader(f))
    assert trades[0]["rating"] == "Buy" and trades[0]["decision_date"] == "2026-10-14"
    with open(account.dir / "equity.csv", encoding="utf-8-sig") as f:
        equity = list(csv.DictReader(f))
    assert equity[-1]["date"] == "2026-10-15" and float(equity[-1]["equity"]) == pytest.approx(
        100_000 - paper.fees_for("603980.SS", "BUY", 10_000) + 500)
    text = paper.render_status(account, "2026-10-15")
    assert "603980.SS" in text and "Recent trades" in text and "benchmark" in text
    # the book sizes like any portfolio
    book = account.portfolio()
    assert book.cash == account.state["cash"] and book.position_in("603980.SH").quantity == 1000


@pytest.mark.unit
def test_a_newer_decision_replaces_an_unfilled_order(account):
    account.submit(_sizing("603980.SS", "Buy", 800), "2026-10-14")
    account.submit(_sizing("603980.SS", "Overweight", 400), "2026-10-15")
    assert [(o["qty"], o["rating"]) for o in account.state["pending"]] == [(400, "Overweight")]
    account.submit(_sizing("603980.SS", "Hold", 0), "2026-10-16")
    assert account.state["pending"] == []


@pytest.mark.unit
def test_reset_keeps_old_files(account, market):
    account.save()
    (account.dir / "trades.csv").write_text("x", encoding="utf-8")
    account.reset(50_000, "CNY")
    assert account.state["cash"] == 50_000
    assert list(account.dir.glob("trades_*_old.csv"))


@pytest.mark.unit
def test_paper_cli_reset_and_status(tmp_path, monkeypatch, market):
    from typer.testing import CliRunner

    import cli.main as cli_main

    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "paper_dir", str(tmp_path / "paper"))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["paper", "reset", "--cash", "200000", "--yes"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli_main.app, ["paper", "status"])
    assert result.exit_code == 0, result.output
    assert "200,000" in result.output and "Paper account" in result.output
