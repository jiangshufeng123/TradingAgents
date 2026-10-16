"""The service's scheduled scoreboard jobs: switch, run records, settlement summary, API."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from server import db  # noqa: E402
from server.models import TaskRun  # noqa: E402
from server.services import system_tasks, users  # noqa: E402
from tradingagents.memory.settlement import Settlement  # noqa: E402

H = {"X-TA-Client": "test"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{(tmp_path / 'tasks.db').as_posix()}")
    db.init_db()
    with db.session() as s:
        users.create_user(s, "ian", "admin-pass-1", role="admin")
        users.create_user(s, "amy", "user-pass-1")
    from tradingagents.default_config import DEFAULT_CONFIG

    return {**DEFAULT_CONFIG, "memory_log_path": str(tmp_path / "memory.md")}


class FakeGraph:
    def __init__(self, settled, failed, beliefs=None):
        self.outcome, self.beliefs, self.calls = Settlement(settled, failed), beliefs, []

    def settle_all_pending(self):
        self.calls.append("settle")
        return self.outcome

    def update_beliefs(self):
        self.calls.append("beliefs")
        return self.beliefs


@pytest.mark.unit
def test_settling_reports_what_it_did(env, monkeypatch):
    left = iter([[{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}], [{"ticker": "C"}]])
    monkeypatch.setattr(system_tasks, "_pending", lambda config: next(left))
    graph = FakeGraph([("A.SS", "2026-10-09")], [("B.SS", "2026-10-09", "prices unavailable: timeout")],
                      beliefs={"based_on": 10, "beliefs": "..."})
    summary, detail = system_tasks.settle(env, graph=graph)
    assert summary == "结算 1 条，1 条没结算成（下次再试），仍待结算 1 条，已按 10 条结算结果重写学到的规则。"
    assert detail["settled"] == [{"ticker": "A.SS", "date": "2026-10-09"}] and detail["pending"] == 1
    assert detail["failed"][0]["reason"].startswith("prices") and detail["beliefs_revised"]

    monkeypatch.setattr(system_tasks, "_pending", lambda config: [])          # nothing pending: no model
    assert system_tasks.settle(env, graph=None)[0] == "没有待结算的决策。"
    idle = FakeGraph([], [])
    monkeypatch.setattr(system_tasks, "_pending", lambda config: [{"ticker": "C"}])
    assert system_tasks.settle(env, graph=idle)[0] == "没有到期的决策，仍待结算 1 条。"
    assert idle.calls == ["settle"]                                           # no beliefs without settling


@pytest.mark.unit
def test_settlement_model_calls_are_charged_to_the_system_owner(env, monkeypatch):
    from cli.stats_handler import StatsCallbackHandler
    from server.models import CostEntry

    config = {**env, "model_prices": "deepseek-flash=2/8", "price_currency": "¥"}
    stats = StatsCallbackHandler()
    for i in range(3):                                                        # three reflections
        stats.llm_calls += 1
        stats.usage.start(i, {}, {"model": "deepseek-flash"})
        stats.usage.end(i, {"input_tokens": 100_000, "output_tokens": 50_000})
    left = iter([[{"ticker": "A"}], []])
    monkeypatch.setattr(system_tasks, "_pending", lambda c: next(left))
    summary, detail = system_tasks.settle(config, graph=FakeGraph([("A.SS", "2026-10-09")], []), stats=stats)
    assert summary == "结算 1 条，仍待结算 0 条，模型花费 ¥1.80（3 次调用）。"         # 0.3M×2 + 0.15M×8
    assert detail["spent"]["charged_to"] == "ian" and detail["spent"]["cost"] == pytest.approx(1.8)
    with db.session() as s:
        entry = s.query(CostEntry).one()
        assert (entry.rating, entry.ticker, entry.llm_calls, entry.input_tokens) == ("决策结算", "", 3, 300_000)
        assert entry.cost == pytest.approx(1.8) and entry.user_id == system_tasks.system_owner(s).id
    assert system_tasks.charge(config, StatsCallbackHandler(), "决策结算") is None   # no calls: nothing recorded


@pytest.mark.unit
def test_runs_are_recorded_and_a_switched_off_job_skips_its_schedule(env):
    done = system_tasks.run("settle", lambda: ("结算 2 条。", {"settled": 2}))
    assert done.status == "done" and done.trigger == "schedule" and done.finished_at >= done.started_at
    bad = system_tasks.run("settle", lambda: 1 / 0, trigger="manual")
    assert bad.status == "failed" and bad.summary.startswith("出错：ZeroDivisionError")
    with db.session() as s:
        system_tasks.set_enabled(s, "settle", False, "ian")
    assert system_tasks.scheduled("settle", lambda: ("x", None)) is None
    with db.session() as s:
        listed = {t["name"]: t for t in system_tasks.status(s)}
        assert listed["settle"]["enabled"] is False and listed["settle"]["updated_by"] == "ian"
        assert [r["status"] for r in listed["settle"]["runs"]] == ["failed", "done"]
        assert listed["screen-check"]["enabled"] is True and listed["screen-check"]["runs"] == []
        s.add(TaskRun(name="settle", status="running"))                       # a service stopped mid-run
        s.flush()
        assert system_tasks.close_interrupted(s) == 1
        assert s.query(TaskRun).filter_by(status="running").count() == 0


@pytest.mark.unit
def test_the_page_lists_the_jobs_and_only_admins_switch_or_run_them(env, monkeypatch):
    from server.app import create_app

    app = create_app(env, start_worker=False, start_scheduler=False)
    ian, amy = TestClient(app), TestClient(app)
    ian.__enter__(), amy.__enter__()
    assert ian.post("/api/auth/login", json={"username": "ian", "password": "admin-pass-1"}).status_code == 200
    assert amy.post("/api/auth/login", json={"username": "amy", "password": "user-pass-1"}).status_code == 200

    listed = amy.get("/api/system-tasks").json()
    assert [t["name"] for t in listed["tasks"]] == ["screen-check", "settle"] and not listed["can_manage"]
    assert amy.post("/api/system-tasks/settle", json={"enabled": False}, headers=H).status_code == 403

    assert ian.post("/api/system-tasks/settle", json={"enabled": False}, headers=H).json()["enabled"] is False
    assert ian.post("/api/system-tasks/nope", json={"enabled": False}, headers=H).status_code == 404
    monkeypatch.setitem(system_tasks.WORK, "settle", lambda config: ("结算 3 条。", {"settled": 3}))
    assert ian.post("/api/system-tasks/settle/run", headers=H).status_code == 202
    for _ in range(50):                                                       # it runs in the background
        runs = ian.get("/api/system-tasks").json()["tasks"][1]["runs"]
        if runs and runs[0]["status"] != "running":
            break
        time.sleep(0.05)
    assert runs[0]["status"] == "done" and runs[0]["trigger"] == "manual" and runs[0]["summary"] == "结算 3 条。"
    assert runs[0]["detail"]["by"] == "ian"
    assert amy.post("/api/system-tasks/settle/run", headers=H).status_code == 202   # anyone may run it now
    for _ in range(50):
        runs = amy.get("/api/system-tasks").json()["tasks"][1]["runs"]
        if len(runs) == 2 and runs[0]["status"] != "running":
            break
        time.sleep(0.05)
    assert runs[0]["detail"]["by"] == "amy" and runs[0]["status"] == "done"
