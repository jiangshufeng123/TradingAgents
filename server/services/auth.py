"""Sign-in sessions and protection against password guessing.

A session token is 32 random bytes handed to the browser once (in an HttpOnly
cookie, or to an API client in the login answer); the database keeps only its
SHA-256, so a copy of the database does not let anyone sign in.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from server.models import AuthSession, User

SESSION_DAYS = 14


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(s: Session, user: User, user_agent: str = "", days: int = SESSION_DAYS) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    s.add(AuthSession(token_hash=_hash(token), user_id=user.id, created_at=now, last_seen=now,
                      expires_at=now + timedelta(days=days), user_agent=user_agent[:200]))
    s.flush()
    return token


def resolve(s: Session, token: str | None) -> User | None:
    """The signed-in, active user for ``token``; None when unknown, expired or disabled."""
    if not token:
        return None
    row = s.get(AuthSession, _hash(token))
    now = datetime.now()
    if row is None or row.expires_at <= now:
        return None
    user = s.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    if (now - row.last_seen).total_seconds() > 300:
        row.last_seen = now
    return user


def revoke(s: Session, token: str | None) -> None:
    if token:
        s.execute(delete(AuthSession).where(AuthSession.token_hash == _hash(token)))


def revoke_user(s: Session, user_id: int) -> None:
    """Sign the user out everywhere (after a password change or disabling)."""
    s.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


def purge_expired(s: Session) -> None:
    s.execute(delete(AuthSession).where(AuthSession.expires_at <= datetime.now()))


def sessions(s: Session, user_id: int) -> list[AuthSession]:
    return list(s.scalars(select(AuthSession).where(AuthSession.user_id == user_id)))


class Throttle:
    """After ``limit`` failed sign-ins for one name or one address within ``window``
    seconds, further attempts are refused until the window passes."""

    def __init__(self, limit: int = 5, window: float = 600):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        stamps = [t for t in self._fails.get(key, []) if now - t < self.window]
        self._fails[key] = stamps
        return stamps

    def blocked(self, *keys: str) -> float:
        """Seconds to wait (0 when allowed)."""
        now = time.time()
        with self._lock:
            waits = [self.window - (now - stamps[0]) for k in keys
                     if len(stamps := self._recent(k, now)) >= self.limit]
        return max(waits, default=0.0)

    def failed(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                self._recent(k, now).append(now)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)
