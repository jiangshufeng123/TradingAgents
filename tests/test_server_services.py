"""The system service's data layer: accounts, per-user data, jobs, costs, import."""

from __future__ import annotations

import csv
import json
from datetime import datetime

import pytest

pytest.importorskip("sqlalchemy")

from server import db  # noqa: E402
from server.security import hash_password, verify_password  # noqa: E402
from server.services import costs, jobs, paper, portfolio, users, watchlist  # noqa: E402


@pytest.fixture
def url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    db.init_db(url)
    return url


@pytest.mark.unit
def test_passwords_are_salted_and_checked():
    h1, h2 = hash_password("correct horse"), hash_password("correct horse")
    assert h1 != h2 and verify_password("correct horse", h1) and not verify_password("wrong", h1)
    with pytest.raises(ValueError):
        hash_password("short")
    assert not verify_password("x", "garbage")


@pytest.mark.unit
def test_users_and_login(url):
    with db.session(url) as s:
        admin = users.create_user(s, "ian", "s3cret-pass", role="admin")
        assert admin.id and admin.role == "admin"
        with pytest.raises(ValueError):
            users.create_user(s, "ian", "another-pass")
    with db.session(url) as s:
        assert users.authenticate(s, "ian", "s3cret-pass").username == "ian"
        assert users.authenticate(s, "ian", "nope") is None
        users.by_name(s, "ian").is_active = False
    with db.session(url) as s:
        assert users.authenticate(s, "ian", "s3cret-pass") is None


@pytest.mark.unit
def test_each_user_has_an_own_watchlist(url):
    with db.session(url) as s:
        a = users.create_user(s, "a", "password-a")
        b = users.create_user(s, "b", "password-b")
        assert watchlist.add(s, a.id, ["NVDA", "603980.SH", "603980.SS"], "screen", "screen") == ["NVDA", "603980.SH"]
        watchlist.add(s, b.id, ["0700.HK"])
    with db.session(url) as s:
        assert watchlist.tickers(s, a.id) == ["NVDA", "603980.SH"]
        assert watchlist.tickers(s, b.id) == ["0700.HK"]
        assert watchlist.remove(s, a.id, ["603980.ss"]) == ["603980.SH"]
        assert watchlist.tickers(s, a.id) == ["NVDA"]


@pytest.mark.unit
def test_portfolio_becomes_a_sizing_context(url):
    with db.session(url) as s:
        u = users.create_user(s, "u", "password-u")
        assert portfolio.context(s, u.id) is None
        portfolio.set_cash(s, u.id, 200_000, "cny")
        portfolio.set_holding(s, u.id, "nvda", 50, 120.0)
        portfolio.set_holding(s, u.id, "600519.SS", 100)
        portfolio.set_holding(s, u.id, "600519.SS", 0)          # zero removes it
    with db.session(url) as s:
        book = portfolio.context(s, u.id)
        assert book.cash == 200_000 and book.currency == "CNY"
        assert [(p.ticker, p.quantity) for p in book.positions] == [("NVDA", 50)]


@pytest.mark.unit
def test_jobs_lifecycle_and_queue_order(url):
    with db.session(url) as s:
        u = users.create_user(s, "u", "password-u")
        j1 = jobs.create(s, u.id, "analysis", {"ticker": "NVDA", "runs": 2})
        j2 = jobs.create(s, u.id, "analysis", {"ticker": "000960.SZ"})
    with db.session(url) as s:
        job = jobs.next_queued(s)
        assert job.id == j1.id
        jobs.start(s, job, "/tmp/log")
    with db.session(url) as s:
        assert jobs.next_queued(s).id == j2.id
        assert [j.status for j in jobs.active(s, u.id)] == ["running", "queued"]
        running = s.get(type(j1), j1.id)
        assert jobs.cancel(s, running) is True and running.status == "cancelling"   # the worker stops it
        running.status = "running"
        job = s.get(type(j1), j1.id)
        jobs.finish(s, job, {"rating": "Hold"}, cost=2.5)
        jobs.cancel(s, s.get(type(j2), j2.id))
    with db.session(url) as s:
        assert [(j.status, j.cost) for j in jobs.recent(s, u.id)] == [("cancelled", None), ("done", 2.5)]


@pytest.mark.unit
def test_monthly_spend_and_quota(url):
    with db.session(url) as s:
        u = users.create_user(s, "u", "password-u")
        u.monthly_quota = 10.0
        costs.record(s, u.id, run_time=datetime(2026, 10, 3), ticker="NVDA", cost=4.0, currency="¥")
        costs.record(s, u.id, run_time=datetime(2026, 9, 30), ticker="NVDA", cost=100.0)
        assert costs.month_spend(s, u.id, datetime(2026, 10, 15)) == 4.0


@pytest.mark.unit
def test_each_user_has_an_own_paper_account(tmp_path):
    config = {"paper_dir": str(tmp_path / "paper"), "paper_cash": 50_000}
    a, b = paper.account(config, 1), paper.account(config, 2)
    a.save()
    b.state["cash"] = 1.0
    b.save()
    assert paper.account(config, 1).state["cash"] == 50_000 and paper.account(config, 2).state["cash"] == 1.0
    assert a.dir != b.dir


@pytest.mark.unit
def test_import_brings_the_single_user_files_in_once(url, tmp_path, monkeypatch):
    from server.importer import import_all

    reports = tmp_path / "reports"
    (reports / "paper").mkdir(parents=True)
    (reports / "paper" / "account.json").write_text(json.dumps({"cash": 123.0}), encoding="utf-8")
    with open(reports / "cost_ledger.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["run_time", "ticker", "trade_date", "rating", "llm_calls",
                                          "input_tokens", "output_tokens", "cost", "currency", "report_dir"])
        w.writeheader()
        w.writerow({"run_time": "2026-10-15 01:41:14", "ticker": "6616.HK", "trade_date": "2026-10-15",
                    "rating": "Overweight", "llm_calls": 37, "input_tokens": 371107, "output_tokens": 151845,
                    "cost": 2.4696, "currency": "¥", "report_dir": "D:/r"})
    (tmp_path / "portfolio.json").write_text(json.dumps(
        {"cash": 200000, "currency": "CNY", "positions": [{"ticker": "NVDA", "quantity": 50}]}), encoding="utf-8")
    (tmp_path / "watchlist.txt").write_text("600519.SS  # screen\n", encoding="utf-8")
    config = {"reports_dir": str(reports), "watchlist": "NVDA,000960.SZ",
              "watchlist_file": str(tmp_path / "watchlist.txt"), "portfolio_path": str(tmp_path / "portfolio.json")}
    with db.session(url) as s:
        u = users.create_user(s, "ian", "password-1", role="admin")
        first = import_all(s, u.id, config)
        second = import_all(s, u.id, config)
    assert first["watchlist"] == ["NVDA", "000960.SZ", "600519.SS"] and first["holdings"] == 1
    assert first["costs"] == 1 and first["paper"] is True
    assert second["watchlist"] == [] and second["costs"] == 0 and second["paper"] is False
    with db.session(url) as s:
        assert costs.month_spend(s, u.id, datetime(2026, 10, 20)) == pytest.approx(2.4696)
        assert portfolio.context(s, u.id).position_in("NVDA").quantity == 50
    assert json.loads((paper.user_dir(config, u.id) / "account.json").read_text(encoding="utf-8"))["cash"] == 123.0


@pytest.mark.unit
def test_server_cli_init_and_users(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as cli_main

    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "watchlist", "NVDA")
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "watchlist_file", str(tmp_path / "none.txt"))
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "reports_dir", str(tmp_path / "reports"))
    runner = CliRunner()
    result = runner.invoke(cli_main.app, ["server", "init", "--admin", "ian"], input="password-1\npassword-1\n")
    assert result.exit_code == 0, result.output
    assert "Imported: watchlist 1" in result.output
    result = runner.invoke(cli_main.app, ["server", "users"])
    assert "ian" in result.output and "admin" in result.output
