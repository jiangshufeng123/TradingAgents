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


def day_spend(s: Session, user_id: int, now: datetime | None = None) -> float:
    now = now or datetime.now()
    start = datetime(now.year, now.month, now.day)
    total = s.scalar(select(func.sum(CostEntry.cost)).where(CostEntry.user_id == user_id,
                                                             CostEntry.run_time >= start))
    return float(total or 0.0)


def check_budget(s: Session, user: User, estimate: float, config: dict) -> tuple[bool, float | None, str]:
    """(allowed, remaining, why not) for spending ``estimate`` more: the account's monthly cap,
    then the daily cap every account shares (``daily_budget``; 0 means none). ``remaining`` is
    the smaller of what is left under each cap that is set."""
    left: list[float] = []
    ok, month_left = within_quota(s, user, estimate)
    if month_left is not None:
        left.append(month_left)
    if not ok:
        return False, min(left), f"本月额度只剩 ¥{max(month_left, 0):.2f}，预计要 ¥{estimate:.2f}"
    try:
        daily = float(config.get("daily_budget") or 0)
    except (TypeError, ValueError):
        daily = 0.0
    if daily > 0:
        today_left = daily - day_spend(s, user.id)
        left.append(today_left)
        if estimate > today_left:
            return False, min(left), f"今天的额度只剩 ¥{max(today_left, 0):.2f}（每天 ¥{daily:.2f}），预计要 ¥{estimate:.2f}"
    return True, (min(left) if left else None), ""


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
