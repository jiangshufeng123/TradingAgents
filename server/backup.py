"""Copies of the service database, made while it runs (SQLite's online backup).

``backup()`` writes <home>/backups/server_YYYYMMDD_HHMM.db and keeps the newest
``keep`` copies. The scheduler runs it every night; ``tradingagents server
backup`` runs it now. A PostgreSQL database is backed up with pg_dump instead.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from server import db

KEEP = 14


def sqlite_path(url: str | None = None) -> Path | None:
    url = url or db.default_url()
    if not url.startswith("sqlite:///"):
        return None
    return Path(url[len("sqlite:///"):])


def backup(url: str | None = None, keep: int = KEEP, folder: Path | None = None) -> Path:
    source = sqlite_path(url)
    if source is None:
        raise RuntimeError("only a SQLite database is copied here; back up PostgreSQL with pg_dump")
    if not source.exists():
        raise FileNotFoundError(f"no database at {source}")
    folder = folder or db.home() / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"server_{datetime.now():%Y%m%d_%H%M%S}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    copies = sorted(folder.glob("server_*.db"))
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return target
