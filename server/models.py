"""Tables. Everything a user owns carries user_id; shared data is not stored here."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.db import Base


def _now() -> datetime:
    return datetime.now()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")          # admin | user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Spending cap on model calls per calendar month, in the price currency; None = no cap.
    monthly_quota: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WatchlistItem(Base):
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "ticker"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(String(200), default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")      # manual | screen | import
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PortfolioCash(Base):
    """The user's real (not paper) cash, in one currency."""
    __tablename__ = "portfolio_cash"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    cash: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("user_id", "ticker"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[float] = mapped_column(Float)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Job(Base):
    """A long task (an analysis, a daily batch, a screen) and what became of it."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))                           # analysis | daily | screen
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued -> running -> done | failed | cancelled; "cancelling" while a running job is stopped
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)


class CostEntry(Base):
    """One analysis run's model usage and cost (the CLI's cost ledger, per user)."""
    __tablename__ = "costs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    run_time: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    ticker: Mapped[str] = mapped_column(String(32), default="")
    trade_date: Mapped[str] = mapped_column(String(10), default="")
    rating: Mapped[str] = mapped_column(String(16), default="")
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="")
    report_dir: Mapped[str] = mapped_column(String(500), default="")


class Schedule(Base):
    """When the user's daily analysis runs (the server's scheduler reads this)."""
    __tablename__ = "schedules"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    time_of_day: Mapped[str] = mapped_column(String(5), default="18:30")
    days: Mapped[str] = mapped_column(String(32), default="mon-fri")
    # Runs per ticker; 0 follows TRADINGAGENTS_VOTE_RUNS (the default for a new account).
    runs: Mapped[int] = mapped_column(Integer, default=0)


class AuthSession(Base):
    """A signed-in browser or device. Only a hash of the token is stored."""
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)
    user_agent: Mapped[str] = mapped_column(String(200), default="")


class ScreenPick(Base):
    """A stock a screen ranked near the top, kept to check later how it did (screen_track.py).

    One row per trading day, strategy and stock, whoever ran the screen and however often:
    a stock picked twice the same day by the same strategy counts once."""
    __tablename__ = "screen_picks"
    __table_args__ = (UniqueConstraint("as_of", "strategy", "ticker"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    as_of: Mapped[str] = mapped_column(String(10), index=True)          # the quotes' trading day
    strategy: Mapped[str] = mapped_column(String(32), index=True)        # a preset key, or "custom"
    label: Mapped[str] = mapped_column(String(120), default="")
    rank: Mapped[int] = mapped_column(Integer)
    ticker: Mapped[str] = mapped_column(String(32))
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ret_5: Mapped[float | None] = mapped_column(Float, nullable=True)     # close-to-close, 5 sessions on
    bench_5: Mapped[float | None] = mapped_column(Float, nullable=True)
    ret_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    bench_20: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
