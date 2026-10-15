"""Database engine and sessions.

``TRADINGAGENTS_DATABASE_URL`` picks the database; the default is a SQLite file
under ~/.tradingagents. Moving to PostgreSQL later is a change of URL
(``postgresql+psycopg://user:pass@host/db``) plus a data copy; the models use
only portable column types.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def home() -> Path:
    """The service's own folder (database, job logs): TRADINGAGENTS_HOME or ~/.tradingagents."""
    path = Path(os.environ.get("TRADINGAGENTS_HOME") or Path.home() / ".tradingagents").expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_url() -> str:
    url = os.environ.get("TRADINGAGENTS_DATABASE_URL", "").strip()
    if url:
        return url
    return f"sqlite:///{(home() / 'server.db').as_posix()}"


_engines: dict = {}


def engine(url: str | None = None):
    url = url or default_url()
    if url not in _engines:
        kwargs = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        eng = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(eng, "connect")
            def _sqlite_pragmas(conn, _record):   # foreign keys on; readers do not block the writer
                cur = conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        _engines[url] = eng
    return _engines[url]


def init_db(url: str | None = None):
    from server import models  # noqa: F401  (registers the tables)

    eng = engine(url)
    Base.metadata.create_all(eng)
    return eng


@contextmanager
def session(url: str | None = None):
    """A unit of work: committed on success, rolled back on error."""
    s = sessionmaker(bind=engine(url), expire_on_commit=False)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
