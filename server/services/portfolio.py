"""The user's real holdings and cash, as the core's PortfolioContext for sizing."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import Holding, PortfolioCash
from tradingagents.portfolio import PortfolioContext
from server.tickers import canonical


def holdings(s: Session, user_id: int) -> list[Holding]:
    return list(s.scalars(select(Holding).where(Holding.user_id == user_id).order_by(Holding.ticker)))


def set_holding(s: Session, user_id: int, ticker: str, quantity: float, average_price: float | None = None) -> None:
    ticker = canonical(ticker)
    row = next((h for h in s.scalars(select(Holding).where(Holding.user_id == user_id))
                if canonical(h.ticker) == ticker), None)
    if quantity == 0:
        if row:
            s.delete(row)
        return
    if row is None:
        s.add(Holding(user_id=user_id, ticker=ticker, quantity=quantity, average_price=average_price))
    else:
        row.quantity, row.average_price = quantity, average_price


def set_cash(s: Session, user_id: int, cash: float | None, currency: str = "CNY") -> None:
    row = s.get(PortfolioCash, user_id)
    if row is None:
        s.add(PortfolioCash(user_id=user_id, cash=cash, currency=currency.upper()))
    else:
        row.cash, row.currency = cash, currency.upper()


def context(s: Session, user_id: int) -> PortfolioContext | None:
    """None when the user has entered neither cash nor holdings (sizing then gives weights)."""
    rows = holdings(s, user_id)
    cash = s.get(PortfolioCash, user_id)
    if not rows and (cash is None or cash.cash is None):
        return None
    return PortfolioContext.model_validate({
        "cash": cash.cash if cash else None,
        "currency": cash.currency if cash else "CNY",
        "positions": [{"ticker": h.ticker, "quantity": h.quantity, "average_price": h.average_price} for h in rows],
    })
