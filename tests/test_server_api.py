"""The web API, the job worker and the scheduler (no model is called)."""

from __future__ import annotations

import csv
import time
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from server import db, runner  # noqa: E402
from server.models import CostEntry, Job, Schedule, User  # noqa: E402
from server.services import jobs, users, watchlist  # noqa: E402

H = {"X-TA-Client": "test"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    db.init_db()
    with db.session() as s:
        users.create_user(s, "ian", "admin-pass-1", role="admin")
        users.create_user(s, "amy", "user-pass-1")
    from tradingagents.default_config import DEFAULT_CONFIG

    config = {**DEFAULT_CONFIG, "reports_dir": str(tmp_path / "reports"), "vote_runs": 2,
              "paper_dir": str(tmp_path / "paper"), "price_currency": "CNY"}
    return config


@pytest.fixture
def app(env):
    from server.app import create_app

    return create_app(env, start_worker=False, start_scheduler=False)


def _client(app, username="ian", password="admin-pass-1") -> TestClient:
    c = TestClient(app)
    c.__enter__()
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


@pytest.mark.unit
def test_sign_in_cookie_bearer_and_csrf_header(app):
    c = TestClient(app)
    with c:
        assert c.get("/api/health").json()["ok"]
        assert c.get("/api/me").status_code == 401
        assert c.post("/api/auth/login", json={"username": "ian", "password": "nope-nope"}).status_code == 401
        r = c.post("/api/auth/login", json={"username": "ian", "password": "admin-pass-1"})
        token = r.json()["token"]
        assert "ta_session" in r.cookies and r.json()["user"]["role"] == "admin"
        assert c.get("/api/me").json()["username"] == "ian"
        # a change sent with only the cookie needs the client header (no cross-site forms)
        assert c.post("/api/watchlist", json={"tickers": ["NVDA"]}).status_code == 403
        assert c.post("/api/watchlist", json={"tickers": ["NVDA"]}, headers=H).status_code == 200
        bare = TestClient(app)
        r = bare.post("/api/watchlist", json={"tickers": ["AAPL"]}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        c.post("/api/auth/logout", headers=H)
        assert bare.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.unit
def test_repeated_wrong_passwords_are_throttled(app):
    with TestClient(app) as c:
        for _ in range(5):
            assert c.post("/api/auth/login", json={"username": "amy", "password": "wrong-pass"}).status_code == 401
        r = c.post("/api/auth/login", json={"username": "amy", "password": "user-pass-1"})
        assert r.status_code == 429


@pytest.mark.unit
def test_watchlist_and_book_are_per_user(app):
    ian, amy = _client(app), _client(app, "amy", "user-pass-1")
    assert ian.post("/api/watchlist", json={"tickers": ["600519.ss, 0700.HK"]}, headers=H).json()["added"] == \
        ["600519.SS", "0700.HK"]
    assert ian.post("/api/watchlist", json={"tickers": ["bad ticker!"]}, headers=H).status_code == 422
    assert [w["ticker"] for w in ian.get("/api/watchlist").json()] == ["600519.SS", "0700.HK"]
    assert amy.get("/api/watchlist").json() == []
    assert ian.delete("/api/watchlist/0700.HK", headers=H).json()["removed"] == ["0700.HK"]
    ian.put("/api/portfolio/cash", json={"cash": 100000, "currency": "cny"}, headers=H)
    ian.put("/api/portfolio/holdings/600519.SS", json={"quantity": 100, "average_price": 1500}, headers=H)
    book = ian.get("/api/portfolio").json()
    assert book["cash"] == 100000 and book["currency"] == "CNY" and book["holdings"][0]["quantity"] == 100
    assert amy.get("/api/portfolio").json()["holdings"] == []


def _ledger(path, rows):
    from tradingagents.usage import LEDGER_FIELDS

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


@pytest.mark.unit
def test_analysis_job_estimate_quota_and_isolation(app, env):
    ian = _client(app)
    ian.post("/api/watchlist", json={"tickers": ["600519.SS", "NVDA"]}, headers=H)
    est = ian.post("/api/analyses/estimate", json={}, headers=H).json()
    assert est["tickers"] == ["600519.SS", "NVDA"] and est["runs"] == 2 and est["estimate"] is None
    _ledger(runner.reports_base(env) / "cost_ledger.csv",
            [{"run_time": "2026-10-14 18:40:00", "ticker": "X", "cost": "1.2", "vote_runs": "2"},
             {"run_time": "2026-10-14 18:50:00", "ticker": "Y", "cost": "0.8", "vote_runs": "1"}])
    est = ian.post("/api/analyses/estimate", json={"tickers": ["NVDA"], "runs": 3}, headers=H).json()
    assert est["estimate"] == pytest.approx(0.7 * 3)        # per run (0.6 + 0.8) / 2, three runs
    job = ian.post("/api/analyses", json={"tickers": ["NVDA"], "runs": 1}, headers=H).json()
    assert job["status"] == "queued" and job["params"]["tickers"] == ["NVDA"]
    with db.session() as s:
        s.get(User, 2).monthly_quota = 0.5                    # amy
    amy = _client(app, "amy", "user-pass-1")
    assert amy.get(f"/api/jobs/{job['id']}").status_code == 404
    r = amy.post("/api/analyses", json={"tickers": ["NVDA"], "runs": 1}, headers=H)
    assert r.status_code == 402 and "quota" in r.json()["detail"]
    assert ian.post(f"/api/jobs/{job['id']}/cancel", headers=H).json()["status"] == "cancelled"
    assert ian.post(f"/api/jobs/{job['id']}/cancel", headers=H).status_code == 409


@pytest.mark.unit
def test_screen_results_join_the_watchlist_only_when_picked(app):
    ian = _client(app)
    job = ian.post("/api/screens", json={"request": "低估值银行股"}, headers=H).json()
    assert ian.post(f"/api/screens/{job['id']}/pick", json={"rows": [1]}, headers=H).status_code == 409
    rows = [{"ticker": "600000.SS", "name": "浦发银行"}, {"ticker": "601398.SS", "name": "工商银行"},
            {"ticker": None, "name": "北交所某股", "code": "430047"}]
    with db.session() as s:
        jobs.finish(s, s.get(Job, job["id"]), {"rows": rows})
    assert ian.get("/api/watchlist").json() == []             # finished, still nothing added
    r = ian.post(f"/api/screens/{job['id']}/pick", json={"rows": [2, 3]}, headers=H).json()
    assert r["added"] == ["601398.SS"] and r["no_symbol"] == ["北交所某股"]
    assert [w["source"] for w in ian.get("/api/watchlist").json()] == ["screen"]
    assert ian.post(f"/api/screens/{job['id']}/pick", json={"rows": [9]}, headers=H).status_code == 422
    assert ian.post("/api/screens", json={"request": " "}, headers=H).status_code == 422
    bad = ian.post("/api/screens", json={"spec": {"limit": "x"}}, headers=H)
    assert bad.status_code == 422


@pytest.mark.unit
def test_worker_runs_jobs_and_records_costs(env):
    from server.worker import Worker

    seen = []

    def fake_analysis(job, user, config, should_stop):
        seen.append((job.params["tickers"], user.username))
        return {"runs": [{"ticker": "NVDA", "rating": "Buy", "cost": 0.4, "currency": "CNY", "trade_date": "2026-10-15",
                          "llm_calls": 30, "input_tokens": 1000, "output_tokens": 200, "report_dir": "x"}]}

    def broken(job, user, config, should_stop):
        raise RuntimeError("model down")

    def stopping(job, user, config, should_stop):
        with db.session() as s:
            jobs.cancel(s, s.get(Job, job.id))
        assert should_stop()
        raise runner.Cancelled()

    with db.session() as s:
        a = jobs.create(s, 1, "analysis", {"tickers": ["NVDA"]}).id
        b = jobs.create(s, 1, "screen", {"request": "x"}).id
        c = jobs.create(s, 1, "stop", {}).id
    w = Worker(env, runners={"analysis": fake_analysis, "screen": broken, "stop": stopping})
    assert w.run_next() and w.run_next() and w.run_next() and not w.run_next()
    with db.session() as s:
        ja, jb, jc = s.get(Job, a), s.get(Job, b), s.get(Job, c)
        assert ja.status == "done" and ja.cost == pytest.approx(0.4) and ja.result["runs"][0]["rating"] == "Buy"
        assert jb.status == "failed" and jb.error.startswith("RuntimeError: model down")
        assert jc.status == "cancelled"
        entry = s.query(CostEntry).one()
        assert entry.user_id == 1 and entry.job_id == a and entry.ticker == "NVDA"
    assert seen == [(["NVDA"], "ian")]


@pytest.mark.unit
def test_jobs_left_running_are_failed_at_start(env):
    from server.worker import Worker

    with db.session() as s:
        j = jobs.create(s, 1, "analysis", {"tickers": ["NVDA"]})
        jobs.claim(s, j.id)
        jid = j.id
    w = Worker(env, runners={}, poll=0.05)
    w.start()
    w.stop()
    with db.session() as s:
        assert s.get(Job, jid).status == "failed"


@pytest.mark.unit
def test_analysis_runner_pieces(env, tmp_path):
    cmd = runner.analysis_command({"tickers": ["NVDA", "600519.SS"], "runs": 2, "date": "2026-10-15"}, None)
    assert cmd[-6:] == ["NVDA,600519.SS", "--runs", "2", "--date", "2026-10-15", "--no-portfolio"]
    assert cmd[2:5] == ["cli.main", "daily", "NVDA,600519.SS"] and cmd[-1] == "--no-portfolio"
    assert "--portfolio" in runner.analysis_command({"tickers": ["NVDA"]}, tmp_path / "p.json")
    with db.session() as s:
        user = s.get(User, 2)
    e = runner.analysis_env(env, user)
    assert e["TRADINGAGENTS_REPORTS_DIR"].replace("\\", "/").endswith("reports/users/amy")
    assert e["TRADINGAGENTS_PAPER_DIR"].replace("\\", "/").endswith("paper/users/2")
    log = "─ 1/3 NVDA 2026-10-15 ─\n...\n─ 2/3 600519.SS 2026-10-15 ─\n"
    p = runner.progress(log)
    assert (p["index"], p["total"], p["ticker"]) == (2, 3, "600519.SS") and p["fraction"] == pytest.approx(1 / 3, abs=1e-3)
    path = tmp_path / "ledger.csv"
    _ledger(path, [{"run_time": "2026-10-15 09:00:00", "ticker": "A"}, {"run_time": "2026-10-15 10:00:00", "ticker": "B"}])
    assert [r["ticker"] for r in runner.ledger_rows(path, since=datetime(2026, 10, 15, 9, 30))] == ["B"]


@pytest.mark.unit
def test_analysis_runner_end_to_end_with_a_stand_in_batch(env, monkeypatch, tmp_path):
    """The child process is replaced by a script that writes the ledger as the CLI would."""
    import sys

    with db.session() as s:
        user = s.get(User, 1)
        job = jobs.create(s, 1, "analysis", {"tickers": ["NVDA", "AAPL"], "runs": 1})
    ledger = runner.user_reports_dir(env, user) / "cost_ledger.csv"
    script = tmp_path / "fake_daily.py"
    script.write_text(
        "import csv, datetime, sys\n"
        "from tradingagents.usage import LEDGER_FIELDS\n"
        f"path = r'{ledger}'\n"
        "import os; os.makedirs(os.path.dirname(path), exist_ok=True)\n"
        "print('─ 1/2 NVDA 2026-10-15 ─', flush=True)\n"
        "with open(path, 'a', encoding='utf-8', newline='') as f:\n"
        "    w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, extrasaction='ignore'); w.writeheader()\n"
        "    w.writerow({'run_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'ticker': 'NVDA',\n"
        "                'rating': 'Hold', 'cost': '0.3', 'currency': 'CNY', 'llm_calls': '20'})\n"
        "sys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(runner, "analysis_command", lambda params, pf: [sys.executable, str(script)])
    result = runner.run_analysis(job, user, env, lambda: False, poll=0.05)
    assert [r["ticker"] for r in result["runs"]] == ["NVDA"] and result["missing"] == ["AAPL"]
    assert result["exit_code"] == 1 and "1/2 NVDA" in runner.job_log(job.id).read_text(encoding="utf-8")

    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(runner, "analysis_command", lambda params, pf: [sys.executable, str(slow)])
    started = time.time()
    with pytest.raises(runner.Cancelled):
        runner.run_analysis(job, user, env, lambda: time.time() - started > 0.3, poll=0.05)
    assert time.time() - started < 15


@pytest.mark.unit
def test_job_log_and_report_routes(app, env, tmp_path):
    ian = _client(app)
    with db.session() as s:
        job = jobs.create(s, 1, "analysis", {"tickers": ["NVDA"]})
        log = runner.job_log(job.id)
        log.write_text("hello\nworld\n", encoding="utf-8")
        job.log_path = str(log)
        report = runner.reports_base(env) / "users" / "ian" / "NVDA" / "2026-10-15_1840"
        report.mkdir(parents=True)
        (report / "complete_report.html").write_text("<h1>NVDA</h1>", encoding="utf-8")
        jobs.finish(s, job, {"runs": [{"ticker": "NVDA", "report_dir": str(report)},
                                      {"ticker": "X", "report_dir": str(tmp_path)}]})
        jid = job.id
    r = ian.get(f"/api/jobs/{jid}/log?offset=6")
    assert r.text == "world\n" and r.headers["X-Log-Offset"] == "12"
    assert "NVDA" in ian.get(f"/api/jobs/{jid}/report/0").text
    assert ian.get(f"/api/jobs/{jid}/report/1").status_code == 404     # outside the reports folder
    assert ian.get(f"/api/jobs/{jid}/report/5").status_code == 404


@pytest.mark.unit
def test_schedule_and_daily_queue(app, env):
    from server import scheduler

    ian = _client(app)
    assert ian.put("/api/schedule", json={"enabled": True, "time_of_day": "25:00"}, headers=H).status_code == 422
    out = ian.put("/api/schedule", json={"enabled": True, "time_of_day": "18:30", "days": "mon-fri", "runs": 2},
                  headers=H).json()
    assert out["enabled"] and out["time_of_day"] == "18:30"
    assert scheduler.queue_daily(1) is None                   # empty watchlist: nothing queued
    with db.session() as s:
        watchlist.add(s, 1, ["NVDA"])
    woke = []
    jid = scheduler.queue_daily(1, on_queued=lambda: woke.append(1))
    assert jid and woke == [1]
    assert scheduler.queue_daily(1) is None                   # the previous one is still queued
    with db.session() as s:
        assert s.get(Job, jid).params == {"tickers": ["NVDA"], "runs": 2, "scheduled": True}
    sch = scheduler.DailyScheduler()
    sch.start()
    try:
        nxt = sch.next_run(1)
        assert nxt is not None and (nxt.hour, nxt.minute) == (18, 30) and nxt.weekday() < 5
        assert sch.next_run(2) is None
    finally:
        sch.stop()


@pytest.mark.unit
def test_admin_routes(app):
    ian, amy = _client(app), _client(app, "amy", "user-pass-1")
    assert amy.get("/api/admin/users").status_code == 403
    assert len(ian.get("/api/admin/users").json()) == 2
    new = ian.post("/api/admin/users", json={"username": "bob", "password": "bob-pass-1", "monthly_quota": 50},
                   headers=H).json()
    assert new["monthly_quota"] == 50
    assert ian.patch("/api/admin/users/1", json={"is_active": False}, headers=H).status_code == 409
    assert ian.patch("/api/admin/users/2", json={"is_active": False}, headers=H).json()["is_active"] is False
    assert amy.get("/api/me").status_code == 401             # signed out when disabled
    r = ian.post("/api/auth/password", json={"old_password": "admin-pass-1", "new_password": "new-admin-pass"},
                 headers=H)
    assert r.status_code == 200 and ian.get("/api/me").status_code == 401


@pytest.mark.unit
def test_overview_and_datasources(app):
    ian = _client(app)
    o = ian.get("/api/overview").json()
    assert o["user"]["username"] == "ian" and o["watchlist_count"] == 0 and o["schedule"]["enabled"] is False
    rows = ian.get("/api/datasources").json()["sources"]
    assert {r["capability"] for r in rows} == {"daily_bars", "cn_spot", "lhb", "margin"}
    assert _client(app, "amy", "user-pass-1").post("/api/datasources/reset", headers=H).status_code == 403
    with db.session() as s:
        assert s.get(Schedule, 1) is not None


@pytest.mark.unit
def test_pages_are_served_with_the_api(app):
    with TestClient(app) as c:
        page = c.get("/")
        assert page.status_code == 200 and "TradingAgents 投研台" in page.text and "app.js" in page.text
        assert c.get("/app.js").status_code == 200 and c.get("/vendor/vue-3.5.13.global.prod.js").status_code == 200
        assert c.get("/api/health").json()["ok"]                 # the API still wins over the pages
        assert c.get("/api/me").status_code == 401


@pytest.mark.unit
def test_overview_decisions_one_per_listing_newest_first(app, env):
    ian, amy = _client(app), _client(app, "amy", "user-pass-1")
    with db.session() as s:
        ian_user, amy_user = s.get(User, 1), s.get(User, 2)
    report = runner.user_reports_dir(env, ian_user) / "NVDA" / "2026-10-15_1640"
    report.mkdir(parents=True)
    (report / "complete_report.html").write_text("<h1>NVDA</h1>", encoding="utf-8")
    # the pre-web ledger (imported history) and this user's own ledger
    _ledger(runner.reports_base(env) / "cost_ledger.csv", [
        {"run_time": "2026-10-09 18:40:00", "ticker": "NVDA", "rating": "Buy"},
        {"run_time": "2026-10-14 18:41:00", "ticker": "603980.SS", "rating": "Underweight", "action": "cut the position by half"},
        {"run_time": "2026-10-14 18:50:00", "ticker": "NVDA", "rating": "Overweight"}])
    _ledger(runner.user_reports_dir(env, ian_user) / "cost_ledger.csv", [
        {"run_time": "2026-10-15 16:40:00", "ticker": "NVDA", "rating": "Underweight", "action": "SELL 100",
         "target_weight": "0.03", "stop": "171.2", "report_dir": str(report)},
        {"run_time": "2026-10-15 16:45:00", "ticker": "603980.SH", "rating": "Hold"}])
    with db.session() as s:
        j = jobs.create(s, 1, "analysis", {"tickers": ["NVDA"]})
        jobs.finish(s, j, {"runs": [{"ticker": "NVDA", "run_time": "2026-10-15 16:40:00", "report_dir": str(report)}]})
        jid = j.id
    d = ian.get("/api/overview").json()["decisions"]
    assert [x["ticker"] for x in d] == ["603980.SH", "NVDA"]               # one per listing, newest first
    nvda = d[1]
    assert nvda["rating"] == "Underweight" and nvda["action"] == "SELL 100" and nvda["target_weight"] == 0.03
    assert (nvda["job_id"], nvda["index"]) == (jid, 0) and d[0]["job_id"] is None
    assert "NVDA" in ian.get("/api/report", params={"dir": str(report)}).text
    assert ian.get("/api/report", params={"dir": "/etc"}).status_code == 404
    assert amy.get("/api/overview").json()["decisions"] == []               # not the administrator's history
    assert amy.get("/api/report", params={"dir": str(report)}).status_code == 404
    assert ian.get("/api/paper").json()["exists"] is False


@pytest.mark.unit
def test_paper_account_opened_and_switched_from_the_page(app, env):
    ian = _client(app)
    assert ian.get("/api/paper").json() == {"exists": False, "orders_on": False, "benchmark": env.get("paper_benchmark") or "",
                                            "currency": env.get("paper_currency") or "CNY"}
    with db.session() as s:
        user = s.get(User, 1)
    assert runner.analysis_env(env, user)["TRADINGAGENTS_PAPER_TRADING"] == "false"     # no account: no orders
    assert ian.put("/api/paper/switch", json={"enabled": True}, headers=H).status_code == 409
    r = ian.post("/api/paper/open", json={"cash": 500000, "currency": "HKD"}, headers=H).json()
    assert r["exists"] and r["orders_on"] and r["currency"] == "HKD" and r["start_cash"] == 500000
    assert runner.analysis_env(env, user)["TRADINGAGENTS_PAPER_TRADING"] == "true"
    assert ian.post("/api/paper/open", json={"cash": 1}, headers=H).status_code == 409          # already open
    assert ian.put("/api/paper/switch", json={"enabled": False}, headers=H).json() == {"orders_on": False}
    assert runner.analysis_env(env, user)["TRADINGAGENTS_PAPER_TRADING"] == "false"
    r = ian.post("/api/paper/open", json={"cash": 2000000, "currency": "CNY", "restart": True}, headers=H).json()
    assert r["start_cash"] == 2000000 and r["orders_on"] is True                                  # a fresh start is on
    assert ian.post("/api/paper/open", json={"cash": -5}, headers=H).status_code == 422
    amy = _client(app, "amy", "user-pass-1")
    assert amy.get("/api/paper").json()["exists"] is False                                       # per user
