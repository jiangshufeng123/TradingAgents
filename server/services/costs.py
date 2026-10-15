"""Model spending per user, for quotas and the dashboard."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server.models import CostEntry, User


def month_spend(s: Session, user_id: int, now: datetime | None = None) -> float:
    now = now or datetime.now()
    start = datetime(now.year, now.month, 1)
    total = s.scalar(select(func.sum(CostEntry.cost)).where(CostEntry.user_id == user_id,
                                                             CostEntry.run_time >= start))
    return float(total or 0.0)


def within_quota(s: Session, user: User, estimate: float = 0.0) -> tuple[bool, float | None]:
    """(allowed, remaining) for spending ``estimate`` more this month."""
    if user.monthly_quota is None:
        return True, None
    remaining = user.monthly_quota - month_spend(s, user.id)
    return estimate <= remaining, remaining


def record(s: Session, user_id: int, **fields) -> CostEntry:
    entry = CostEntry(user_id=user_id, **{k: v for k, v in fields.items() if hasattr(CostEntry, k)})
    s.add(entry)
    s.flush()
    return entry
