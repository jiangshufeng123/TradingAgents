"""One spelling per listing, and analyses that cannot outlive the service."""
import subprocess
import sys
import time

import pytest

from server import procs
from server.tickers import canonical


@pytest.mark.unit
@pytest.mark.parametrize("raw, want", [
    ("603980.SH", "603980.SS"), ("603980.ss", "603980.SS"), ("603980", "603980.SS"),
    ("SH603980", "603980.SS"), ("603980SH", "603980.SS"), ("688981", "688981.SS"),
    ("510300", "510300.SS"), ("000960", "000960.SZ"), ("300750", "300750.SZ"),
    ("SZ000960", "000960.SZ"), ("000960.SZ", "000960.SZ"), ("830799", "830799.BJ"),
    ("920118", "920118.BJ"), ("700.HK", "0700.HK"), ("00700", "0700.HK"), ("700", "0700.HK"),
    ("nvda", "NVDA"), ("BRK.B", "BRK.B"), ("^GSPC", "^GSPC"), ("", ""),
])
def test_canonical(raw, want):
    assert canonical(raw) == want


@pytest.mark.unit
def test_api_and_storage_use_the_canonical_spelling(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{tmp_path / 'ta.db'}")
    from server import db
    from server.models import Holding, User, WatchlistItem
    from server.services import portfolio, watchlist
    from server.tickers import canonicalize_stored

    db.init_db()
    with db.session() as s:
        s.add(User(id=1, username="ian", password_hash="x"))
        s.flush()
        s.add_all([WatchlistItem(user_id=1, ticker="603980.SH"), WatchlistItem(user_id=1, ticker="000960"),
                   Holding(user_id=1, ticker="600519.SH", quantity=100)])
    with db.session() as s:
        assert canonicalize_stored(s) == 3
    with db.session() as s:
        assert watchlist.tickers(s, 1) == ["603980.SS", "000960.SZ"]
        assert watchlist.add(s, 1, ["603980.SH"]) == []                 # the same listing, once
        portfolio.set_holding(s, 1, "600519.SH", 200)                    # updates, not a second row
        assert [(h.ticker, h.quantity) for h in portfolio.holdings(s, 1)] == [("600519.SS", 200)]
        assert canonicalize_stored(s) == 0


def _sleeper(*argv):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process table")
def test_leftover_analysis_is_stopped_but_nothing_else(tmp_path):
    work = tmp_path / "7"
    work.mkdir()
    analysis = _sleeper("-m", "cli.main", "daily", "NVDA")       # looks like an analysis
    other = _sleeper("unrelated")
    try:
        time.sleep(0.2)
        procs.remember(work, analysis.pid)
        assert procs.stop_leftover(work) is True
        assert analysis.wait(timeout=10) is not None
        assert not procs.pid_file(work).exists()

        procs.remember(work, other.pid)                          # a reused process id
        assert procs.stop_leftover(work) is False
        assert other.poll() is None
        assert procs.stop_leftover(tmp_path / "none") is False
    finally:
        for p in (analysis, other):
            p.kill()
            p.wait()


@pytest.mark.unit
def test_contain_is_a_no_op_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows has the job object")
    proc = _sleeper()
    try:
        assert procs.contain(proc) is None
        procs.release(None)
    finally:
        proc.kill()
        proc.wait()
