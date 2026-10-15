"""Risk numbers and position size, computed in code rather than estimated by an LLM.

The agents decide direction; how much to hold follows from arithmetic the model
should not be trusted with: the instrument's volatility (ATR), a stop at a
multiple of it, a fixed fraction of equity at risk per position, a cap on any
one position's weight, the cash that is actually free, board lots, and what the
rest of the book already holds.

Two uses:

* ``context_block`` runs before the agents and gives the Trader, the risk
  analysts and the Portfolio Manager the measured figures (and, with a
  portfolio, the book's exposure, correlation and value at risk), so their
  sizing talk starts from the same numbers.
* ``size_position`` runs after the final rating and turns it into a target
  position: shares to buy or sell, the stop, the money at risk, and warnings
  when a limit binds. This is the number to act on; the agents' prose sizing is
  commentary.

Every price comes through the same point-in-time loader the analysts use, so a
historical run sizes on what was known at its date.

Settings (``.env``): TRADINGAGENTS_RISK_PER_TRADE (fraction of equity lost if
the stop is hit, default 0.01), TRADINGAGENTS_ATR_STOP_MULT (stop distance in
ATRs, default 2), TRADINGAGENTS_MAX_POSITION_WEIGHT (default 0.2),
TRADINGAGENTS_MAX_PORTFOLIO_VAR (1-day 95% VaR limit as a fraction of equity,
default 0.03), TRADINGAGENTS_LOT_SIZES (e.g. "6616.HK=500,0700.HK=100").
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
ATR_PERIOD = 14

# How far toward the full risk-sized position each rating goes.
RATING_TARGET = {"Buy": 1.0, "Overweight": 0.5}


# --- instruments ----------------------------------------------------------------

def currency_of(ticker: str) -> str:
    """The quote currency implied by a Yahoo symbol's suffix."""
    symbol = normalize_symbol(ticker).upper()
    if symbol.endswith((".SS", ".SZ", ".BJ")):
        return "CNY"
    if symbol.endswith(".HK"):
        return "HKD"
    if symbol.endswith(".T"):
        return "JPY"
    return "USD"


def a_share_board(ticker: str) -> tuple[str, float] | None:
    """(board, daily price limit) for an A-share, else None."""
    symbol = normalize_symbol(ticker).upper()
    if not symbol.endswith((".SS", ".SZ", ".BJ")):
        return None
    code = symbol.split(".")[0]
    if symbol.endswith(".BJ") or code.startswith(("8", "4", "92")):
        return "北交所", 0.30
    if code.startswith("688"):
        return "科创板", 0.20
    if code.startswith(("300", "301")):
        return "创业板", 0.20
    return "主板", 0.10


LOT_CACHE_DAYS = 30


def _hk_lot_lookup(symbol: str, config: dict) -> int | None:
    """A Hong Kong board lot, looked up once and kept in the data cache for a month."""
    import json
    import os
    from datetime import date

    cache_dir = config.get("data_cache_dir")
    if not cache_dir:
        from tradingagents.dataflows.config import get_config

        cache_dir = get_config().get("data_cache_dir")
    path = os.path.join(cache_dir, "hk_board_lots.json") if cache_dir else None
    cached = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            cached = {}
    hit = cached.get(symbol)
    today = date.today()
    if hit and (today - date.fromisoformat(hit["date"])).days < LOT_CACHE_DAYS:
        return int(hit["lot"])
    try:
        from tradingagents.dataflows.vendors.akshare_cn import hk_board_lot

        found = hk_board_lot(symbol)
    except Exception as exc:
        logger.info("No board lot for %s: %s", symbol, exc)
        found = None
    if not found:
        return int(hit["lot"]) if hit else None      # a stale answer beats none
    lot, source = found
    cached[symbol] = {"lot": lot, "date": today.isoformat(), "source": source}
    if path:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cached, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
    return lot


def lot_size(ticker: str, config: dict) -> tuple[int, bool]:
    """(shares per board lot, whether it is known).

    TRADINGAGENTS_LOT_SIZES wins; A-shares trade in 100s; a Hong Kong lot is
    looked up (East Money, then HKEX) and cached for a month.
    """
    symbol = normalize_symbol(ticker).upper()
    for item in str(config.get("lot_sizes") or "").replace(";", ",").split(","):
        if "=" in item:
            name, _, size = item.partition("=")
            if normalize_symbol(name.strip()).upper() == symbol:
                try:
                    return max(int(float(size)), 1), True
                except ValueError:
                    break
    if a_share_board(symbol):
        return 100, True
    if symbol.endswith(".HK"):
        lot = _hk_lot_lookup(symbol, config)
        return (lot, True) if lot else (1, False)
    return 1, True


def _load(ticker: str, as_of: str) -> pd.DataFrame:
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    return load_ohlcv(ticker, as_of)


@dataclass
class Metrics:
    ticker: str
    as_of: str
    last_date: str
    close: float
    atr: float
    vol_20: float | None
    vol_60: float | None
    max_drawdown_1y: float | None
    currency: str
    returns: pd.Series = field(repr=False)

    @property
    def atr_pct(self) -> float:
        return self.atr / self.close if self.close else float("nan")


def atr(data: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """Wilder's average true range of the frame's latest bar."""
    high, low, close = data["High"], data["Low"], data["Close"]
    prev = close.shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return float(true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1])


def _vol(returns: pd.Series, days: int) -> float | None:
    tail = returns.tail(days)
    return float(tail.std() * math.sqrt(TRADING_DAYS)) if len(tail) >= max(10, days // 2) else None


def instrument_metrics(ticker: str, as_of: str) -> Metrics:
    data = _load(ticker, as_of)
    if data is None or len(data) < ATR_PERIOD + 2:
        raise ValueError(f"not enough price history for {ticker} to measure its risk")
    data = data.sort_values("Date")
    closes = data["Close"].astype(float)
    returns = pd.Series(np.log(closes).diff().to_numpy(), index=pd.to_datetime(data["Date"])).dropna()
    year = closes.tail(TRADING_DAYS)
    drawdown = float((year / year.cummax() - 1).min()) if len(year) > 20 else None
    return Metrics(
        ticker=normalize_symbol(ticker), as_of=as_of,
        last_date=pd.Timestamp(data["Date"].iloc[-1]).strftime("%Y-%m-%d"),
        close=float(closes.iloc[-1]), atr=atr(data),
        vol_20=_vol(returns, 20), vol_60=_vol(returns, 60), max_drawdown_1y=drawdown,
        currency=currency_of(ticker), returns=returns,
    )


_FX: dict = {}


def fx_rate(from_ccy: str, to_ccy: str, as_of: str) -> float:
    """Units of ``to_ccy`` per one ``from_ccy`` at ``as_of``."""
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    if from_ccy == to_ccy:
        return 1.0
    key = (from_ccy, to_ccy, as_of)
    if key not in _FX:
        try:
            _FX[key] = float(_load(f"{from_ccy}{to_ccy}=X", as_of)["Close"].iloc[-1])
        except Exception:
            _FX[key] = 1.0 / float(_load(f"{to_ccy}{from_ccy}=X", as_of)["Close"].iloc[-1])
    return _FX[key]


# --- the book ---------------------------------------------------------------------

@dataclass
class Holding:
    ticker: str
    quantity: float
    metrics: Metrics | None
    fx: float
    value: float | None          # in the base currency

    @property
    def weight_basis(self) -> float:
        return self.value or 0.0


@dataclass
class Book:
    base: str
    cash: float | None
    holdings: list[Holding]
    problems: list[str]

    @property
    def equity(self) -> float | None:
        if self.cash is None:
            return None
        return self.cash + sum(h.weight_basis for h in self.holdings)

    def holding(self, ticker: str) -> Holding | None:
        symbol = normalize_symbol(ticker).upper()
        return next((h for h in self.holdings if normalize_symbol(h.ticker).upper() == symbol), None)


def build_book(portfolio, as_of: str, base: str | None = None) -> Book:
    """Value every position in the base currency (the portfolio's cash currency)."""
    base = (base or getattr(portfolio, "currency", None) or "CNY").upper()
    holdings, problems = [], []
    for p in portfolio.positions:
        try:
            m = instrument_metrics(p.ticker, as_of)
            fx = fx_rate(m.currency, base, as_of)
            holdings.append(Holding(p.ticker, p.quantity, m, fx, p.quantity * m.close * fx))
        except Exception as exc:
            problems.append(f"{p.ticker}: no price ({type(exc).__name__}); left out of the book's value")
            holdings.append(Holding(p.ticker, p.quantity, None, 1.0, None))
    return Book(base=base, cash=portfolio.cash, holdings=holdings, problems=problems)


def _returns_frame(series: dict[str, pd.Series], days: int) -> pd.DataFrame:
    frame = pd.DataFrame(series).sort_index()
    # Markets keep different holidays: a day one market was shut is a zero return there.
    return frame.fillna(0.0).tail(days)


@dataclass
class BookRisk:
    equity: float
    weights: dict[str, float]
    vol: float | None
    var_95: float | None
    cvar_95: float | None
    open_risk: float          # loss at every position's ATR stop, fraction of equity


def book_risk(book: Book, atr_mult: float, extra: tuple[str, float, Metrics, float] | None = None,
              days: int = TRADING_DAYS) -> BookRisk | None:
    """Volatility and historical 1-day VaR/CVaR of the book at today's weights.

    ``extra`` = (ticker, quantity, metrics, fx) replaces or adds one position,
    to see the book after a proposed trade.
    """
    equity = book.equity
    if not equity or equity <= 0:
        return None
    positions = {normalize_symbol(h.ticker).upper(): (h.quantity, h.metrics, h.fx)
                 for h in book.holdings if h.metrics is not None}
    if extra is not None:
        ticker, qty, metrics, fx = extra
        positions[normalize_symbol(ticker).upper()] = (qty, metrics, fx)
    weights = {t: q * m.close * fx / equity for t, (q, m, fx) in positions.items() if q}
    open_risk = sum(abs(q) * atr_mult * m.atr * fx for q, m, fx in positions.values()) / equity
    if not weights:
        return BookRisk(equity, {}, 0.0, 0.0, 0.0, 0.0)
    frame = _returns_frame({t: positions[t][1].returns for t in weights}, days)
    if len(frame) < 30:
        return BookRisk(equity, weights, None, None, None, open_risk)
    pnl = frame.mul(pd.Series(weights)).sum(axis=1)
    cut = float(pnl.quantile(0.05))
    return BookRisk(
        equity=equity, weights=weights,
        vol=float(pnl.std() * math.sqrt(TRADING_DAYS)),
        var_95=-cut, cvar_95=-float(pnl[pnl <= cut].mean()), open_risk=open_risk,
    )


def correlations(metrics: Metrics, book: Book, days: int = 120) -> dict[str, float]:
    out = {}
    for h in book.holdings:
        if h.metrics is None or normalize_symbol(h.ticker).upper() == metrics.ticker.upper():
            continue
        frame = _returns_frame({"a": metrics.returns, "b": h.metrics.returns}, days)
        if len(frame) >= 30 and frame["a"].std() > 0 and frame["b"].std() > 0:
            out[normalize_symbol(h.ticker)] = float(frame["a"].corr(frame["b"]))
    return out


# --- settings ---------------------------------------------------------------------

@dataclass
class Limits:
    risk_per_trade: float = 0.01
    atr_mult: float = 2.0
    max_weight: float = 0.20
    max_var: float = 0.03

    @classmethod
    def from_config(cls, config: dict) -> "Limits":
        def num(key, default):
            value = config.get(key)
            try:
                return float(value) if value not in (None, "") else default
            except (TypeError, ValueError):
                return default
        return cls(num("risk_per_trade", 0.01), num("atr_stop_mult", 2.0),
                   num("max_position_weight", 0.20), num("max_portfolio_var", 0.03))


def _pct(x: float | None, digits: int = 1) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{digits}f}%"


def _money(x: float | None, ccy: str = "") -> str:
    return "—" if x is None else f"{x:,.2f}{(' ' + ccy) if ccy else ''}"


# --- before the agents --------------------------------------------------------------

def context_block(ticker: str, as_of: str, portfolio, config: dict) -> str:
    """Measured risk figures for the decision agents; '' when prices are unavailable."""
    limits = Limits.from_config(config)
    try:
        m = instrument_metrics(ticker, as_of)
    except Exception as exc:
        logger.info("No risk metrics for %s: %s", ticker, exc)
        return ""
    stop = m.close - limits.atr_mult * m.atr
    risk_weight = min(limits.risk_per_trade * m.close / (limits.atr_mult * m.atr), limits.max_weight)
    lines = [
        f"Computed risk figures for {m.ticker} (code, not estimates; quote these rather than your own):",
        f"- Last close {m.close:,.4g} {m.currency} on {m.last_date}; ATR({ATR_PERIOD}) {m.atr:,.4g} "
        f"({_pct(m.atr_pct)} of price)",
        f"- Annualized volatility: 20-day {_pct(m.vol_20)}, 60-day {_pct(m.vol_60)}; "
        f"1-year max drawdown {_pct(m.max_drawdown_1y)}",
        f"- Volatility stop at {limits.atr_mult:g}×ATR: {stop:,.4g} ({_pct(-limits.atr_mult * m.atr_pct)})",
        f"- Risk budget: {_pct(limits.risk_per_trade)} of equity lost if that stop is hit gives a full "
        f"position of {_pct(risk_weight)} of equity (cap {_pct(limits.max_weight, 0)} per position). "
        f"Buy sizes to the full position, Overweight to half; the final share count is computed "
        f"after your decision.",
    ]
    board = a_share_board(ticker)
    if board:
        lines.append(f"- A-share {board[0]}: daily price limit ±{board[1] * 100:.0f}%, T+1 (shares bought "
                     f"today cannot be sold until the next session), board lot 100 shares.")
    elif m.ticker.upper().endswith(".HK"):
        lot, known = lot_size(ticker, config)
        if known:
            lines.append(f"- Hong Kong board lot: {lot:,} shares.")
    if portfolio is not None:
        try:
            lines += _book_lines(m, portfolio, as_of, limits)
        except Exception as exc:
            logger.info("No book risk for %s: %s", ticker, exc)
    return "\n".join(lines)


def _book_lines(m: Metrics, portfolio, as_of: str, limits: Limits) -> list[str]:
    book = build_book(portfolio, as_of)
    lines = []
    risk = book_risk(book, limits.atr_mult)
    if risk is not None:
        held = book.holding(m.ticker)
        weight = risk.weights.get(normalize_symbol(m.ticker).upper(), 0.0) if held else 0.0
        lines.append(f"- Book: equity {_money(risk.equity, book.base)}; {m.ticker} is {_pct(weight)} of it; "
                     f"cash {_pct((book.cash or 0) / risk.equity)}")
        lines.append(f"- Book risk now: annualized volatility {_pct(risk.vol)}, 1-day 95% VaR {_pct(risk.var_95, 2)}"
                     f" (limit {_pct(limits.max_var, 1)}), CVaR {_pct(risk.cvar_95, 2)}, loss if every position "
                     f"hit its {limits.atr_mult:g}×ATR stop {_pct(risk.open_risk)}")
    corr = correlations(m, book)
    if corr:
        top = sorted(corr.items(), key=lambda kv: -abs(kv[1]))[:5]
        lines.append("- 120-day return correlation with holdings: "
                     + ", ".join(f"{t} {c:+.2f}" for t, c in top))
    lines += [f"- Note: {p}" for p in book.problems]
    return lines


# --- after the decision --------------------------------------------------------------

@dataclass
class Sizing:
    ticker: str
    rating: str
    close: float
    currency: str
    atr: float
    stop: float
    lot: int
    lot_known: bool
    base: str | None = None
    equity: float | None = None
    current_qty: float | None = None
    target_qty: float | None = None
    trade_qty: float | None = None
    target_weight: float | None = None
    current_weight: float | None = None
    risk_at_stop: float | None = None        # fraction of equity
    cash_after: float | None = None
    var_before: float | None = None
    var_after: float | None = None
    full_weight: float | None = None          # risk-sized full position, fraction of equity
    warnings: list[str] = field(default_factory=list)

    @property
    def action(self) -> str:
        if self.trade_qty is None:
            return {"Buy": "open/add up to the full position", "Overweight": "open/add up to half the full position",
                    "Hold": "keep what you hold", "Underweight": "cut the position by half",
                    "Sell": "exit / do not open"}.get(self.rating, "review")
        if self.trade_qty > 0:
            return f"BUY {self.trade_qty:,.0f}"
        if self.trade_qty < 0:
            return f"SELL {-self.trade_qty:,.0f}"
        return "no trade"


def _round_lot(qty: float, lot: int) -> float:
    return math.floor(qty / lot) * lot if qty > 0 else 0.0


def size_position(ticker: str, rating: str, as_of: str, portfolio, config: dict) -> Sizing:
    """Turn the final rating into a target position, in shares when the book is known."""
    limits = Limits.from_config(config)
    m = instrument_metrics(ticker, as_of)
    lot, lot_known = lot_size(ticker, config)
    stop_dist = limits.atr_mult * m.atr
    full_weight = min(limits.risk_per_trade * m.close / stop_dist, limits.max_weight) if stop_dist else 0.0
    s = Sizing(ticker=m.ticker, rating=rating, close=m.close, currency=m.currency, atr=m.atr,
               stop=m.close - stop_dist, lot=lot, lot_known=lot_known, full_weight=full_weight)
    if not lot_known:
        s.warnings.append(f"Board lot of {m.ticker} could not be looked up: shares are not rounded "
                          f"to lots; set TRADINGAGENTS_LOT_SIZES (e.g. {m.ticker}=500).")
    board = a_share_board(ticker)
    if board and stop_dist / m.close > board[1]:
        s.warnings.append(f"The stop is {stop_dist / m.close:.0%} away, beyond the ±{board[1]:.0%} daily limit: "
                          f"a limit-down day can hold you past it.")
    if board and rating in RATING_TARGET:
        s.warnings.append("T+1: shares bought today can only be sold from the next session.")
    if rating not in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
        s.warnings.append("No usable rating: no size computed.")
        return s
    if portfolio is None or portfolio.cash is None:
        if rating in RATING_TARGET:
            s.target_weight = full_weight * RATING_TARGET[rating]
        s.warnings.append("No portfolio given (TRADINGAGENTS_PORTFOLIO): sizes are weights of equity, "
                          "not share counts.")
        return s

    book = build_book(portfolio, as_of)
    s.warnings += book.problems
    equity = book.equity
    s.base, s.equity = book.base, equity
    if not equity or equity <= 0:
        s.warnings.append("The book's equity is not positive: no size computed.")
        return s
    fx = fx_rate(m.currency, book.base, as_of)
    held = book.holding(ticker)
    q = held.quantity if held else 0.0
    s.current_qty, s.current_weight = q, q * m.close * fx / equity
    if q < 0:
        s.warnings.append("The book is short this instrument; sizing covers long positions only.")
        return s
    full = full_weight * equity / (m.close * fx)
    cap = limits.max_weight * equity / (m.close * fx)
    # A bullish or neutral call never sells: a position above the cap is
    # flagged with the trim that would restore it, and left to the caller.
    if rating in RATING_TARGET:
        target = q if q >= cap else min(max(q, full * RATING_TARGET[rating]), cap)
    elif rating == "Hold":
        target = q
    elif rating == "Underweight":
        target = q / 2
    else:
        target = 0.0
    if q > cap and target >= q:
        excess = math.ceil((q - cap) / lot) * lot
        s.warnings.append(f"Held at {s.current_weight:.1%}, above the {limits.max_weight:.0%} weight cap: "
                          f"selling {excess:,.0f} would bring it back under; no shares added.")

    trade = target - q
    if trade > 0:
        if book.cash is not None and trade * m.close * fx > book.cash:
            trade = book.cash / (m.close * fx)
            s.warnings.append("Not enough cash for the full size: limited to the cash available.")
        trade = _round_lot(trade, lot)
        if trade == 0:
            s.warnings.append(f"The increase is smaller than one board lot ({lot}): no trade.")
    elif trade < 0:
        if target == 0:
            trade = -q                                  # odd lots may be sold to close
        else:
            trade = -_round_lot(-trade, lot)
    s.trade_qty = trade
    s.target_qty = q + trade
    s.target_weight = s.target_qty * m.close * fx / equity
    s.risk_at_stop = s.target_qty * stop_dist * fx / equity
    s.cash_after = (book.cash or 0.0) - trade * m.close * fx

    before = book_risk(book, limits.atr_mult)
    after = book_risk(book, limits.atr_mult, extra=(ticker, s.target_qty, m, fx))
    s.var_before = before.var_95 if before else None
    s.var_after = after.var_95 if after else None
    if s.var_after is not None and s.var_after > limits.max_var:
        s.warnings.append(f"Book 1-day 95% VaR after the trade {_pct(s.var_after, 2)} exceeds the "
                          f"{_pct(limits.max_var, 1)} limit: consider a smaller size or cutting other risk.")
    for name, c in correlations(m, book).items():
        if c >= 0.7 and trade > 0:
            s.warnings.append(f"Adding to a position that moves with {name} (correlation {c:+.2f}): "
                              f"the two act as one bet.")
    return s


def render_markdown(s: Sizing) -> str:
    """The sizing section for the report."""
    rows = [
        ("Rating", s.rating),
        ("Action", f"**{s.action}**"),
        ("Last close", f"{s.close:,.4g} {s.currency}"),
        (f"ATR({ATR_PERIOD})", f"{s.atr:,.4g} ({_pct(s.atr / s.close if s.close else None)})"),
        ("Stop (volatility)", f"{s.stop:,.4g} {s.currency}"),
        ("Full risk-sized position", _pct(s.full_weight)),
        ("Target weight", _pct(s.target_weight)),
    ]
    if s.equity is not None:
        rows += [
            ("Equity", _money(s.equity, s.base)),
            ("Current / target shares",
             f"{s.current_qty:,.0f} → {s.target_qty:,.0f}" if s.target_qty is not None else "—"),
            ("Current weight", _pct(s.current_weight)),
            ("Loss at stop (of equity)", _pct(s.risk_at_stop, 2)),
            ("Cash after", _money(s.cash_after, s.base)),
            ("Book 1-day 95% VaR before → after", f"{_pct(s.var_before, 2)} → {_pct(s.var_after, 2)}"),
        ]
    rows.append(("Board lot", f"{s.lot}" + ("" if s.lot_known else " (unknown)")))
    lines = ["Computed in code from the final rating, ATR and the book; the agents' own sizing text is "
             "commentary.", "", "| | |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    if s.warnings:
        lines += ["", "**Warnings**", ""] + [f"- {w}" for w in s.warnings]
    return "\n".join(lines)


def summary_line(s: Sizing) -> str:
    """One line for the console and the daily summary."""
    parts = [f"{s.ticker}: {s.rating} → {s.action}"]
    if s.target_weight is not None:
        parts.append(f"target {_pct(s.target_weight)}")
    parts.append(f"stop {s.stop:,.4g} {s.currency}")
    return ", ".join(parts)
