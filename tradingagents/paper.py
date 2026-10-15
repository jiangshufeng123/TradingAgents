"""Paper trading: the system's calls traded in a simulated account, by the market's rules.

A forward scoreboard says whether ratings point the right way; a paper account
says what following them would have earned once sizing, cash, board lots, fees,
T+1 and price limits get their say. Each analysis's final rating is sized
against the paper book (tradingagents/risk.py) and becomes an order; the order
fills at the open of the next session, or is rejected by the rules:

* A-shares: buys in lots of 100; T+1 (a lot bought on a day cannot be sold that
  day); a buy cannot fill at a limit-up open nor a sell at a limit-down open.
* Fees per market (commission with a minimum, stamp duty, exchange fees),
  charged in the instrument's currency and booked in the account's.
* A held position carries the volatility stop from its last sizing; a session
  whose low reaches it sells the position at the stop, or at the open when the
  session gaps below it.

The account lives in ``paper_dir`` (default: <reports_dir>/paper): account.json
for state, trades.csv for every fill or rejection, equity.csv for the daily
value. ``tradingagents paper status`` updates and shows it.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

# Fees as fractions of trade value, per market; minimums in the instrument's currency.
# Typical retail schedules; adjust here if your broker differs.
FEES = {
    "CN": {"commission": 0.00025, "min_commission": 5.0, "stamp_sell": 0.0005, "stamp_buy": 0.0,
           "other": 0.00001},                                  # transfer fee
    "HK": {"commission": 0.0003, "min_commission": 3.0, "stamp_sell": 0.001, "stamp_buy": 0.001,
           "other": 0.0000565 + 0.000027 + 0.0000015},         # trading fee, SFC and AFRC levies
    "US": {"commission": 0.0, "min_commission": 0.0, "stamp_sell": 0.0000278, "stamp_buy": 0.0,
           "other": 0.0},                                      # SEC fee on sells
}

TRADE_FIELDS = ("date", "ticker", "side", "qty", "price", "value", "fees", "currency", "cash_after",
                "reason", "status", "decision_date", "rating")
EQUITY_FIELDS = ("date", "cash", "positions_value", "equity", "return", "benchmark_return")


def market_of(ticker: str) -> str:
    symbol = normalize_symbol(ticker).upper()
    if symbol.endswith((".SS", ".SZ", ".BJ")):
        return "CN"
    if symbol.endswith(".HK"):
        return "HK"
    return "US"


def fees_for(ticker: str, side: str, value: float) -> float:
    f = FEES[market_of(ticker)]
    commission = max(value * f["commission"], f["min_commission"]) if value else 0.0
    stamp = value * (f["stamp_sell"] if side == "SELL" else f["stamp_buy"])
    return round(commission + stamp + value * f["other"], 2)


def paper_dir(config: dict) -> Path:
    if config.get("paper_dir"):
        return Path(config["paper_dir"]).expanduser()
    base = config.get("reports_dir") or os.path.join(config.get("results_dir", "."), "reports")
    return Path(base).expanduser() / "paper"


@dataclass
class Fill:
    date: str
    ticker: str
    side: str
    qty: float
    price: float
    fees: float
    reason: str
    status: str = "filled"
    decision_date: str = ""
    rating: str = ""


class PaperAccount:
    """The simulated account: state on disk, orders, fills and the daily value."""

    def __init__(self, config: dict):
        self.config = config
        self.dir = paper_dir(config)
        self.path = self.dir / "account.json"
        self.state = self._load()

    # --- state ------------------------------------------------------------------

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return self._fresh(float(self.config.get("paper_cash") or 1_000_000),
                           str(self.config.get("paper_currency") or "CNY"))

    @staticmethod
    def _fresh(cash: float, currency: str) -> dict:
        today = date.today().isoformat()
        return {"base": currency.upper(), "start_cash": cash, "cash": cash, "start_date": today,
                "positions": {}, "pending": [], "last_update": None}

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def reset(self, cash: float, currency: str = "CNY") -> None:
        self.state = self._fresh(cash, currency)
        for name in ("trades.csv", "equity.csv"):
            if (self.dir / name).exists():
                (self.dir / name).rename(self.dir / f"{name[:-4]}_{date.today():%Y%m%d}_old.csv")
        self.save()

    @property
    def base(self) -> str:
        return self.state["base"]

    def quantity(self, ticker: str) -> float:
        pos = self.state["positions"].get(normalize_symbol(ticker))
        return sum(lot["qty"] for lot in pos["lots"]) if pos else 0.0

    def portfolio(self):
        """The book as a PortfolioContext, for sizing against it."""
        from tradingagents.portfolio import PortfolioContext

        return PortfolioContext.model_validate({
            "cash": self.state["cash"], "currency": self.base,
            "positions": [{"ticker": t, "quantity": self.quantity(t),
                           "average_price": self._avg_cost(t)} for t in self.state["positions"]],
        })

    def _avg_cost(self, ticker: str) -> float | None:
        lots = self.state["positions"].get(normalize_symbol(ticker), {}).get("lots", [])
        qty = sum(lot["qty"] for lot in lots)
        return sum(lot["qty"] * lot["price"] for lot in lots) / qty if qty else None

    # --- orders -------------------------------------------------------------------

    def submit(self, sizing, decision_date: str) -> dict | None:
        """Queue the trade a sizing calls for; keep the stop of a held position current."""
        ticker = normalize_symbol(sizing.ticker)
        pos = self.state["positions"].get(ticker)
        if pos is not None and sizing.rating in ("Buy", "Overweight", "Hold"):
            # A stop only ever moves up while the call stays bullish or neutral.
            pos["stop"] = max(pos.get("stop") or 0.0, sizing.stop)
        # One order per ticker: a newer decision replaces an unfilled older one.
        self.state["pending"] = [o for o in self.state["pending"] if o["ticker"] != ticker]
        order = None
        if sizing.trade_qty:
            order = {"ticker": ticker, "side": "BUY" if sizing.trade_qty > 0 else "SELL",
                     "qty": abs(sizing.trade_qty), "decision_date": decision_date, "rating": sizing.rating,
                     "stop": sizing.stop, "lot": sizing.lot}
            self.state["pending"].append(order)
        self.save()
        return order

    # --- the market ---------------------------------------------------------------

    def _bars(self, ticker: str, as_of: str) -> pd.DataFrame:
        from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

        data = load_ohlcv(ticker, as_of).copy()
        data["Date"] = pd.to_datetime(data["Date"]).dt.normalize()
        return data.sort_values("Date").reset_index(drop=True)

    def _fx(self, ticker: str, as_of: str) -> float:
        from tradingagents.risk import currency_of, fx_rate

        return fx_rate(currency_of(ticker), self.base, as_of)

    @staticmethod
    def _limit_open(ticker: str, bars: pd.DataFrame, i: int) -> int:
        """+1 when the session opened at its limit-up, -1 at limit-down, else 0 (A-shares only)."""
        from tradingagents.risk import a_share_board

        board = a_share_board(ticker)
        if board is None or i == 0:
            return 0
        prev, opened = float(bars["Close"].iloc[i - 1]), float(bars["Open"].iloc[i])
        if opened >= round(prev * (1 + board[1]), 2) - 0.005:
            return 1
        if opened <= round(prev * (1 - board[1]), 2) + 0.005:
            return -1
        return 0

    def update(self, as_of: str | None = None) -> list[Fill]:
        """Fill or reject queued orders and run the stops, through ``as_of`` (default today)."""
        as_of = as_of or date.today().isoformat()
        fills: list[Fill] = []
        still = []
        for order in self.state["pending"]:
            try:
                fill = self._execute(order, as_of)
            except Exception as exc:   # no prices yet: try again next update
                logger.info("Paper order %s %s waits: %s", order["side"], order["ticker"], exc)
                fill = None
            if fill is None:
                still.append(order)
            else:
                fills.append(fill)
        self.state["pending"] = still
        for ticker in list(self.state["positions"]):
            try:
                fill = self._check_stop(ticker, as_of)
            except Exception as exc:
                logger.info("Paper stop check for %s skipped: %s", ticker, exc)
                fill = None
            if fill:
                fills.append(fill)
        self.state["last_update"] = as_of
        self._log(fills)
        self._mark(as_of)
        self.save()
        return fills

    def _execute(self, order: dict, as_of: str) -> Fill | None:
        ticker = order["ticker"]
        bars = self._bars(ticker, as_of)
        after = bars.index[bars["Date"] > pd.Timestamp(order["decision_date"])]
        if len(after) == 0:
            return None                      # the next session has not traded yet
        i = int(after[0])
        day = bars["Date"].iloc[i].strftime("%Y-%m-%d")
        price = float(bars["Open"].iloc[i])
        limit = self._limit_open(ticker, bars, i)
        reject = None
        if order["side"] == "BUY" and limit > 0:
            reject = "opened at limit-up: no seller"
        if order["side"] == "SELL" and limit < 0:
            reject = "opened at limit-down: no buyer"
        if reject:
            return self._reject(order, day, price, reject)
        fx = self._fx(ticker, day)
        if order["side"] == "BUY":
            qty = order["qty"]
            lot = int(order.get("lot") or 1)
            while qty > 0 and qty * price * fx + fees_for(ticker, "BUY", qty * price) * fx > self.state["cash"]:
                qty -= lot
            if qty <= 0:
                return self._reject(order, day, price, "not enough cash")
            return self._tag(self._buy(ticker, qty, price, fx, day, order, "rating " + order["rating"]), order)
        sellable = self._sellable(ticker, day)
        qty = min(order["qty"], sellable)
        if qty <= 0:
            return self._reject(order, day, price, "nothing sellable (T+1 or no position)")
        return self._tag(self._sell(ticker, qty, price, fx, day, order, "rating " + order["rating"]), order)

    @staticmethod
    def _tag(fill: Fill, order: dict) -> Fill:
        fill.decision_date, fill.rating = order.get("decision_date", ""), order.get("rating", "")
        return fill

    def _sellable(self, ticker: str, day: str) -> float:
        pos = self.state["positions"].get(ticker)
        if not pos:
            return 0.0
        t_plus_1 = market_of(ticker) == "CN"
        return sum(lot["qty"] for lot in pos["lots"] if not t_plus_1 or lot["date"] < day)

    def _buy(self, ticker, qty, price, fx, day, order, reason) -> Fill:
        value = qty * price
        fees = fees_for(ticker, "BUY", value)
        self.state["cash"] -= (value + fees) * fx
        pos = self.state["positions"].setdefault(ticker, {"lots": [], "stop": None})
        pos["lots"].append({"qty": qty, "price": price + fees / qty, "date": day})
        if order.get("stop"):
            pos["stop"] = max(pos.get("stop") or 0.0, order["stop"])
        return Fill(day, ticker, "BUY", qty, price, fees, reason)

    def _sell(self, ticker, qty, price, fx, day, order, reason) -> Fill:
        value = qty * price
        fees = fees_for(ticker, "SELL", value)
        self.state["cash"] += (value - fees) * fx
        pos = self.state["positions"][ticker]
        left = qty
        t_plus_1 = market_of(ticker) == "CN"
        for lot in pos["lots"]:                 # first in, first out, sellable lots only
            if left <= 0:
                break
            if t_plus_1 and lot["date"] >= day:
                continue
            take = min(lot["qty"], left)
            lot["qty"] -= take
            left -= take
        pos["lots"] = [lot for lot in pos["lots"] if lot["qty"] > 0]
        if not pos["lots"]:
            del self.state["positions"][ticker]
        return Fill(day, ticker, "SELL", qty, price, fees, reason)

    def _reject(self, order, day, price, reason) -> Fill:
        return self._tag(Fill(day, order["ticker"], order["side"], order["qty"], price, 0.0, reason,
                              status="rejected"), order)

    def _check_stop(self, ticker: str, as_of: str) -> Fill | None:
        pos = self.state["positions"].get(ticker)
        stop = pos.get("stop") if pos else None
        if not stop:
            return None
        bars = self._bars(ticker, as_of)
        since = pos.get("stop_checked") or min(lot["date"] for lot in pos["lots"])
        for i in bars.index[bars["Date"] > pd.Timestamp(since)]:
            day = bars["Date"].iloc[i].strftime("%Y-%m-%d")
            pos["stop_checked"] = day
            if float(bars["Low"].iloc[i]) > stop:
                continue
            if self._limit_open(ticker, bars, i) < 0 and float(bars["High"].iloc[i]) <= float(bars["Open"].iloc[i]):
                continue                     # locked at limit-down all session: no fill
            qty = self._sellable(ticker, day)
            if qty <= 0:
                continue
            price = min(float(bars["Open"].iloc[i]), stop)
            return self._sell(ticker, qty, price, self._fx(ticker, day), day, {}, f"stop {stop:,.4g} hit")
        return None

    # --- records ------------------------------------------------------------------

    def _log(self, fills: list[Fill]) -> None:
        if not fills:
            return
        path = self.dir / "trades.csv"
        new = not path.exists()
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new:
                writer.writeheader()
            for fill in fills:
                writer.writerow({
                    "date": fill.date, "ticker": fill.ticker, "side": fill.side, "qty": fill.qty,
                    "price": round(fill.price, 4), "value": round(fill.qty * fill.price, 2), "fees": fill.fees,
                    "currency": _ccy(fill.ticker), "cash_after": round(self.state["cash"], 2),
                    "reason": fill.reason, "status": fill.status, "decision_date": fill.decision_date,
                    "rating": fill.rating,
                })

    def valuation(self, as_of: str) -> dict:
        """Positions at their last close on or before ``as_of``, in the base currency."""
        rows, total = [], 0.0
        for ticker, pos in self.state["positions"].items():
            qty = sum(lot["qty"] for lot in pos["lots"])
            try:
                bars = self._bars(ticker, as_of)
                close = float(bars["Close"].iloc[-1])
                fx = self._fx(ticker, as_of)
            except Exception:
                close, fx = None, None
            value = qty * close * fx if close is not None else None
            total += value or 0.0
            avg = self._avg_cost(ticker)
            rows.append({"ticker": ticker, "qty": qty, "avg_cost": avg, "close": close, "value": value,
                         "pnl": (close / avg - 1) if close and avg else None, "stop": pos.get("stop")})
        equity = self.state["cash"] + total
        for row in rows:
            row["weight"] = (row["value"] or 0.0) / equity if equity else None
        return {"cash": self.state["cash"], "positions_value": total, "equity": equity, "rows": rows,
                "return": equity / self.state["start_cash"] - 1}

    def _benchmark_return(self, as_of: str) -> float | None:
        bench = self.config.get("paper_benchmark") or "000300.SS"
        try:
            bars = self._bars(bench, as_of)
            start = bars[bars["Date"] <= pd.Timestamp(self.state["start_date"])]
            if start.empty:
                return None
            return float(bars["Close"].iloc[-1]) / float(start["Close"].iloc[-1]) - 1
        except Exception:
            return None

    def _mark(self, as_of: str) -> None:
        v = self.valuation(as_of)
        path = self.dir / "equity.csv"
        rows = []
        if path.exists():
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = [r for r in csv.DictReader(f) if r["date"] != as_of]
        rows.append({"date": as_of, "cash": round(v["cash"], 2), "positions_value": round(v["positions_value"], 2),
                     "equity": round(v["equity"], 2), "return": round(v["return"], 6),
                     "benchmark_return": _round(self._benchmark_return(as_of))})
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=EQUITY_FIELDS)
            writer.writeheader()
            writer.writerows(rows)


def _ccy(ticker: str) -> str:
    from tradingagents.risk import currency_of

    return currency_of(ticker)


def _round(x, digits=6):
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, digits)


def render_status(account: PaperAccount, as_of: str) -> str:
    """The account as markdown: value, positions, pending orders, recent trades."""
    v = account.valuation(as_of)
    bench = account._benchmark_return(as_of)
    base = account.base
    lines = [f"## Paper account ({base}), started {account.state['start_date']} with "
             f"{account.state['start_cash']:,.0f}",
             f"- Equity {v['equity']:,.2f} ({v['return']:+.2%}); cash {v['cash']:,.2f}; "
             f"positions {v['positions_value']:,.2f}"
             + (f"; benchmark {account.config.get('paper_benchmark') or '000300.SS'} {bench:+.2%}"
                if bench is not None else "")]
    if v["rows"]:
        lines += ["", "| Ticker | Shares | Avg cost | Close | P&L | Weight | Stop |", "|---|---:|---:|---:|---:|---:|---:|"]
        for r in v["rows"]:
            lines.append(f"| {r['ticker']} | {r['qty']:,.0f} | {_n(r['avg_cost'])} | {_n(r['close'])} | "
                         f"{_p(r['pnl'])} | {_p(r['weight'])} | {_n(r['stop'])} |")
    if account.state["pending"]:
        lines += ["", "Orders for the next open:"]
        lines += [f"- {o['side']} {o['qty']:,.0f} {o['ticker']} ({o['rating']} on {o['decision_date']})"
                  for o in account.state["pending"]]
    path = account.dir / "trades.csv"
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            recent = list(csv.DictReader(f))[-8:]
        if recent:
            lines += ["", "Recent trades:"]
            lines += [f"- {t['date']} {t['side']} {float(t['qty']):,.0f} {t['ticker']} @ {t['price']} "
                      f"({t['status']}: {t['reason']})" for t in recent]
    return "\n".join(lines)


def _n(x):
    return "—" if x is None else f"{x:,.4g}"


def _p(x):
    return "—" if x is None else f"{x:+.1%}" if abs(x) < 10 else f"{x:.0%}"
