"""Screens keep their top picks; the forward check says, per strategy, whether they beat the index."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from server import db
from server.models import Job, ScreenPick, User
from server.services import screen_track, users
from tradingagents import screener


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{(tmp_path / 'st.db').as_posix()}")
    db.init_db()
    with db.session() as s:
        users.create_user(s, "ian", "admin-pass-1", role="admin")
    from tradingagents.default_config import DEFAULT_CONFIG

    return {**DEFAULT_CONFIG, "reports_dir": str(tmp_path / "reports"), "data_cache_dir": str(tmp_path / "cache")}


def _result(tickers):
    return pd.DataFrame({"ticker": tickers, "score": [90 - i for i in range(len(tickers))]})


def _bars(start, closes):
    return pd.DataFrame({"Date": pd.bdate_range(start, periods=len(closes)), "Close": closes})


@pytest.mark.unit
def test_picks_are_kept_once_per_day_and_strategy(env):
    spec = screener.Spec.from_preset("quality_growth")
    with db.session() as s:
        assert screen_track.record(s, None, 1, _result(["A.SS", "B.SS", None]), spec, "2026-10-16") == 2
        assert screen_track.record(s, None, 1, _result(["B.SS", "C.SS"]), spec, "2026-10-16") == 1
        custom = screener.Spec(sort=[{"field": "mcap", "desc": True}])
        assert screen_track.record(s, None, 1, _result(["A.SS"]), custom, "2026-10-16") == 1
        assert screen_track.strategy_of(custom) == ("custom", "自定义")
        many = _result([f"T{i}.SS" for i in range(15)])
        assert screen_track.record(s, None, 1, many, screener.Spec.from_preset("balanced"), "2026-10-16") == 10


@pytest.mark.unit
def test_the_check_fills_returns_against_the_index_once_enough_sessions_traded(env):
    spec = screener.Spec.from_preset("trend_momentum")
    with db.session() as s:
        screen_track.record(s, None, 1, _result(["UP.SS", "DOWN.SS", "NEW.SS"]), spec, "2026-09-01")
    prices = {
        "UP.SS": _bars("2026-09-01", [10 + i * 0.1 for i in range(30)]),       # +5% in 5 sessions
        "DOWN.SS": _bars("2026-09-01", [10 - i * 0.1 for i in range(30)]),
        "NEW.SS": _bars("2026-09-01", [10, 10.1, 10.2]),                         # not 5 sessions yet
        "000300.SS": _bars("2026-09-01", [100 + i * 0.2 for i in range(30)]),    # +1%
    }
    with db.session() as s:
        n = screen_track.check(s, lambda t, d: prices[t], today=date(2026, 10, 16))
        assert n == 2
        up = s.query(ScreenPick).filter_by(ticker="UP.SS").one()
        assert up.ret_5 == pytest.approx(0.05) and up.bench_5 == pytest.approx(0.01)
        assert up.ret_20 == pytest.approx(0.2) and s.query(ScreenPick).filter_by(ticker="NEW.SS").one().ret_5 is None
        out = screen_track.summary(s)
    row = out["strategies"][0]
    assert row["name"] == "趋势动量" and row["picks"] == 3 and row["checked_5"] == 2
    assert row["beat_5"] == pytest.approx(0.5) and row["excess_5"] == pytest.approx(((0.05 - 0.01) + (-0.05 - 0.01)) / 2)
    assert out["pending"] == 1 and out["recent"][0]["ticker"] in {"UP.SS", "DOWN.SS"}
    day = row["days"][0]                                                       # the details under the strategy
    assert row["older_days"] == 0 and day["as_of"] == "2026-09-01" and day["check_from"] == "2026-09-08"
    assert [p["ticker"] for p in day["picks"]] == ["UP.SS", "DOWN.SS", "NEW.SS"]
    assert day["picks"][0]["excess_5"] == pytest.approx(0.04) and day["picks"][2]["excess_5"] is None
    assert day["checked_5"] == 2 and day["excess_5"] == pytest.approx(-0.01) and day["bench_5"] == pytest.approx(0.01)
    assert day["job"] is None                                                  # no screen job of the viewer's
    with db.session() as s:                                                    # too young: nothing to do
        screen_track.record(s, None, 1, _result(["X.SS"]), spec, "2026-10-14")
        assert screen_track.check(s, lambda t, d: (_ for _ in ()).throw(AssertionError("no fetch")),
                                  today=date(2026, 10, 16)) == 0


@pytest.mark.unit
def test_a_preset_screen_runs_without_the_model_and_keeps_its_picks(env, monkeypatch):
    from server import runner
    from server.services import jobs

    universe = pd.DataFrame({
        "code": ["600519", "000858", "600000"], "name": ["贵州茅台", "五粮液", "浦发银行"],
        "ticker": ["600519.SS", "000858.SZ", "600000.SS"], "st": [False, False, False],
        "industry": ["酿酒", "酿酒", "银行"], "price": [1500, 130, 8], "pe": [22, 15, 6], "pb": [8, 4, 0.5],
        "mcap": [19000, 5000, 2400], "amount": [50, 30, 2], "roe_annualized": [36, 20, 8],
        "profit_yoy": [12, 3, -50], "revenue_yoy": [10, 5, -2], "chg_60d": [5, -3, -10], "chg_ytd": [10, 0, -20],
        "gross_margin": [91, 75, 0], "eps": [30, 5, 0.3], "ocf_per_share": [25, 4, 0], "turnover_pct": [0.3, 0.6, 1],
    })
    universe.attrs.update(source="test", missing=[], as_of="2026-10-16")
    monkeypatch.setattr(screener, "build_universe", lambda config: (universe, "20260630"))
    monkeypatch.setattr(screener, "price_loader", lambda config: None)
    monkeypatch.setattr(screener, "interpret", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model")))
    with db.session() as s:
        job = jobs.create(s, 1, "screen", {"request": "", "preset": "quality_growth"})
        user = s.get(User, 1)
    out = runner.run_screen(job, user, env, lambda: False)
    assert [r["ticker"] for r in out["rows"]] == ["600519.SS", "000858.SZ"]      # the bank fails roe >= 10
    assert out["ranking"] == {"quality": 2, "growth": 2, "momentum": 0.5, "value": 0.5}
    assert out["rows"][0]["score"] >= out["rows"][1]["score"] and "quality_score" in out["rows"][0]
    with db.session() as s:
        kept = s.query(ScreenPick).order_by(ScreenPick.rank).all()
        assert [(p.ticker, p.strategy, p.as_of) for p in kept] == [
            ("600519.SS", "quality_growth", "2026-10-16"), ("000858.SZ", "quality_growth", "2026-10-16")]


@pytest.mark.unit
def test_review_job_reviews_the_chosen_rows_and_records_the_cost(env, monkeypatch):
    from server import runner
    from server.models import CostEntry
    from server.services import jobs
    from tradingagents.dataflows.vendors import akshare_cn
    import tradingagents.llm_clients as llm_clients

    rows = [{"ticker": "600519.SS", "name": "贵州茅台", "score": 80}, {"ticker": None, "name": "北交所某股"},
            {"ticker": "000858.SZ", "name": "五粮液", "score": 70}]
    with db.session() as s:
        screen = jobs.create(s, 1, "screen", {"request": "x"})
        jobs.finish(s, screen, {"rows": rows, "conditions": "- top 20"})
        job = jobs.create(s, 1, "review", {"screen_job": screen.id, "rows": [], "top": 5})
        user = s.get(User, 1)

    class Client:
        def get_llm(self):
            from types import SimpleNamespace

            return SimpleNamespace(invoke=lambda p: SimpleNamespace(
                content='{"verdict": "回避", "thesis": "t", "risks": ["r"], "watch": "w"}',
                usage_metadata={"input_tokens": 1000, "output_tokens": 100}))
    monkeypatch.setattr(llm_clients, "create_tier_client", lambda config, tier, **k: Client())
    monkeypatch.setattr(akshare_cn, "get_news", lambda t, s, e: "No news.")
    out = runner.run_review(job, user, {**env, "model_prices": "deepseek-flash=2/8", "quick_think_llm": "deepseek-flash"},
                            lambda: False)
    assert [r["ticker"] for r in out["reviews"]] == ["600519.SS", "000858.SZ"]       # no code: skipped
    assert {r["verdict"] for r in out["reviews"]} == {"回避"}
    assert out["runs"][0]["cost"] == pytest.approx((1000 * 2 + 100 * 8) / 1e6) and out["runs"][0]["rating"] == "回避"
    with db.session() as s:
        assert runner.review_rows(s.get(Job, screen.id), [3, 2, 9], None) == [rows[2]]


@pytest.mark.unit
def test_the_details_link_only_the_viewers_own_screens_and_keep_the_newest_days(env):
    from server.services import jobs

    spec = screener.Spec.from_preset("balanced")
    with db.session() as s:
        users.create_user(s, "other", "other-pass-1")
    with db.session() as s:
        for i in range(screen_track.DETAIL_DAYS + 2):
            screen_track.record(s, None, 2, _result([f"D{i}.SS"]), spec, (date(2026, 1, 1) + timedelta(days=i)).isoformat())
        mine, theirs = s.query(ScreenPick).filter_by(ticker="D31.SS").one(), s.query(ScreenPick).filter_by(ticker="D30.SS").one()
        own_job, their_job = jobs.create(s, 1, "screen", {}), jobs.create(s, 2, "screen", {})
        mine.user_id, mine.job_id, theirs.job_id = 1, own_job.id, their_job.id
        s.flush()
        own_id, their_id, own_serial, their_serial = own_job.id, their_job.id, own_job.serial, their_job.serial
        row = screen_track.summary(s, user_id=1)["strategies"][0]
    assert len(row["days"]) == screen_track.DETAIL_DAYS and row["older_days"] == 2
    assert row["days"][0]["as_of"] == "2026-02-01" and row["days"][0]["job"] == own_id and row["days"][1]["job"] is None
    first, second = row["days"][0], row["days"][1]
    assert first["own_jobs"] == [own_id] and first["picks"][0]["job"] == own_id and first["picks"][0]["mine"]
    assert first["jobs"] == [{"id": own_id, "serial": own_serial, "mine": True}] and first["picks"][0]["serial"] == own_serial
    assert second["jobs"] == [{"id": their_id, "serial": their_serial, "mine": False}] and second["own_jobs"] == []
    assert not second["picks"][0]["mine"] and second["picks"][0]["serial"] == their_serial

