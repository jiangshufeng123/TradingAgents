from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import Schedule, User
from server.security import hash_password, verify_password


def create_user(s: Session, username: str, password: str, role: str = "user", display_name: str = "") -> User:
    username = username.strip()
    if not username:
        raise ValueError("username is empty")
    if s.scalar(select(User).where(User.username == username)):
        raise ValueError(f"user {username!r} already exists")
    user = User(username=username, password_hash=hash_password(password), role=role,
                display_name=display_name or username)
    s.add(user)
    s.flush()
    s.add(Schedule(user_id=user.id, runs=0))                # runs: follow TRADINGAGENTS_VOTE_RUNS
    return user


def authenticate(s: Session, username: str, password: str) -> User | None:
    user = s.scalar(select(User).where(User.username == username.strip()))
    if user and user.is_active and verify_password(password, user.password_hash):
        return user
    return None


def set_password(s: Session, user: User, password: str) -> None:
    user.password_hash = hash_password(password)


def get(s: Session, user_id: int) -> User | None:
    return s.get(User, user_id)


def by_name(s: Session, username: str) -> User | None:
    return s.scalar(select(User).where(User.username == username.strip()))


def admins(s: Session) -> list[User]:
    return list(s.scalars(select(User).where(User.role == "admin")))
