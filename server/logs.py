"""Keep the service's log readable and bounded.

* The web page asks for the job list every few seconds, so successful GETs would fill
  the log (about 600 KB a day) and bury what matters. ``QuietAccess`` drops them from
  uvicorn's access log; every other request, and every error or refusal, is still logged.
* scripts/run_server.ps1 writes logs/server_YYYY-MM-DD.log. ``prune`` deletes those older
  than ``KEEP_DAYS`` days; the nightly 03:00 job runs it.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
KEEP_DAYS = 14


class QuietAccess(logging.Filter):
    """Pass an access line unless it is a GET that succeeded."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 5:
            method, status = str(args[1]).upper(), args[4]
            try:
                if method in ("GET", "HEAD") and int(status) < 400:
                    return False
            except (TypeError, ValueError):
                pass
        return True


def quiet_access_log() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietAccess) for f in access.filters):
        access.addFilter(QuietAccess())


def prune(folder: Path | None = None, keep_days: int = KEEP_DAYS, now: float | None = None) -> list[Path]:
    """Delete server_*.log files last written more than ``keep_days`` days ago."""
    folder = Path(folder or PROJECT_DIR / "logs")
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    gone = []
    for path in folder.glob("server_*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                gone.append(path)
        except OSError:            # in use or already gone: next night
            pass
    return gone
