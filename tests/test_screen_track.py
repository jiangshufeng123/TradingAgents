"""Screens keep their top picks; the forward check says, per strategy, whether they beat the index."""
from __future__ import annotations

from datetime import date

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
