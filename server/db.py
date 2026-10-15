"""Database engine and sessions.

``TRADINGAGENTS_DATABASE_URL`` picks the database; the default is a SQLite file
under ~/.tradingagents. Moving to PostgreSQL later is a change of URL
(``postgresql+psycopg://user:pass@host/db``) plus a data copy; the models use
only portable column types.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime
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
    upgrade(eng)
    return eng


# Columns added after a table first shipped: create_all makes new tables but never alters an
# existing one, so a database from an earlier version gets them here (portable SQL only).
_ADDED_COLUMNS = [("jobs", "serial", "VARCHAR(12)", "CREATE UNIQUE INDEX IF NOT EXISTS ix_jobs_serial ON jobs (serial)")]


def upgrade(eng) -> None:
    """Bring a database made by an earlier version up to the current models; safe to repeat."""
    from sqlalchemy import inspect, text

    insp = inspect(eng)
    with eng.begin() as conn:
        for table, column, sql_type, index in _ADDED_COLUMNS:
            if column not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
                if index:
                    conn.execute(text(index))
    rebuilt = _rebuild_screen_picks(eng)
    from server.services import jobs

    with _session_for(eng) as s:
        jobs.backfill_serials(s)
    if rebuilt:
        from server.services import screen_track

        with _session_for(eng) as s:
            screen_track.backfill(s)


def _rebuild_screen_picks(eng) -> bool:
    """Screen picks used to be one per day, strategy and stock; now one per screen job and
    stock. A unique constraint cannot be changed in place in SQLite, so the table is rebuilt.

    Done so that no step can lose the rows, even where the database commits each table change
    at once (SQLite's Python driver does): the old table is first renamed to screen_picks_old,
    the new one made from the model, the rows copied, and only then the old table dropped. A
    run cut short anywhere is finished by the next start. Returns whether rows were moved."""
    from sqlalchemy import inspect, text

    insp = inspect(eng)
    names = set(insp.get_table_names())
    if "screen_picks" in names and "screen_picks_old" not in names:
        old = [sorted(u["column_names"]) for u in insp.get_unique_constraints("screen_picks")]
        if sorted(["as_of", "strategy", "ticker"]) not in old:
            return False
        indexes = [i["name"] for i in insp.get_indexes("screen_picks") if i.get("name")]
        with eng.begin() as conn:
            conn.execute(text("ALTER TABLE screen_picks RENAME TO screen_picks_old"))
            for name in indexes:                       # the new table's indexes take these names
                conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
    elif "screen_picks_old" not in names:
        return False

    table = Base.metadata.tables["screen_picks"]
    table.create(eng, checkfirst=True)
    with eng.begin() as conn:
        cols = [c.name for c in table.columns]
        old = text(f"SELECT {', '.join(cols)} FROM screen_picks_old ORDER BY id").columns(
            *[table.c[name] for name in cols])           # read with the model's types: dates come back as dates
        rows = [dict(r._mapping) for r in conn.execute(old)]
        have = {(r.job_id, r.ticker) for r in conn.execute(text("SELECT job_id, ticker FROM screen_picks"))}
        ids = {r[0] for r in conn.execute(text("SELECT id FROM screen_picks"))}
        keep = []
        for r in rows:
            key = (r.get("job_id"), r.get("ticker"))
            if r.get("id") in ids or (key[0] is not None and key in have):
                continue                                # copied already, or a second row of that job and stock
            have.add(key)
            r["created_at"] = r.get("created_at") or datetime.now()
            keep.append({c.name: r.get(c.name) for c in table.columns})
        if keep:
            conn.execute(table.insert(), keep)
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE screen_picks_old"))
    return True


@contextmanager
def _session_for(eng):
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


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
