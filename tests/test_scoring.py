"""Multi-factor scoring: filters decide who is in, code ranks them."""
from __future__ import annotations

import pandas as pd
import pytest

from tradingagents import scoring


def _pool():
    # two banks (cheap, slow), three tech names (dear, growing, volatile), one loss-maker
    return pd.DataFrame({
        "ticker": ["B1.SS", "B2.SS", "B3.SS", "B4.SS", "B5.SS", "T1.SZ", "T2.SZ", "T3.SZ", "L1.SZ"],
        "industry": ["银行"] * 5 + ["半导体"] * 3 + ["半导体"],
        "pe": [5, 6, 7, 8, 9, 60, 40, 80, -10], "pb": [0.5, 0.6, 0.7, 0.8, 0.9, 6, 4, 8, 3],
        "dividend_yield": [6, 5, 4, 3, 2, 0.2, 0.5, 0, 0],
        "roe_annualized": [10, 11, 9, 12, 8, 15, 20, 5, -5], "gross_margin": [0, 0, 0, 0, 0, 40, 50, 30, 10],
        "eps": [1, 1, 1, 1, 1, 2, 3, 0.5, -1], "ocf_per_share": [1, 1, 1, 1, 1, 2, 4, 0.2, -2],
        "revenue_yoy": [2, 3, 1, 4, 0, 30, 50, 10, -20], "profit_yoy": [1, 2, 0, 3, -1, 40, 60, 5, -80],
        "chg_60d": [1, 2, 0, 3, -1, 30, 20, 40, -30], "chg_ytd": [5, 6, 4, 7, 3, 60, 50, 80, -40],
        "turnover_pct": [0.3, 0.4, 0.2, 0.5, 0.3, 5, 4, 8, 6], "amount": [10, 8, 6, 5, 4, 30, 25, 40, 3],
    })


@pytest.mark.unit
def test_presets_rank_by_their_intent():
    pool = _pool()
    defensive = scoring.rank(pool, scoring.weights_for("dividend_lowvol", None), 3, "2026-10-16")
    attack = scoring.rank(pool, scoring.weights_for("trend_momentum", None), 3, "2026-10-16")
    assert set(defensive["ticker"]) <= {"B1.SS", "B2.SS", "B3.SS", "B4.SS", "B5.SS"}
    assert attack["ticker"].iloc[0] in {"T1.SZ", "T3.SZ"} and "L1.SZ" not in set(attack["ticker"])
    assert list(defensive.columns).count("score") == 1
    assert all(f"{f}_score" in defensive.columns for f in scoring.FACTORS)
    assert defensive["score"].is_monotonic_decreasing


@pytest.mark.unit
def test_ranks_are_within_the_industry(monkeypatch):
    """The best-valued bank and the best-valued chip maker both score high on value: banks
    are compared with banks (an industry needs MIN_PEERS listings for that)."""
    monkeypatch.setattr(scoring, "MIN_PEERS", 4)
    scored = scoring.score(_pool(), {"value": 1}).set_index("ticker")
    assert scored.loc["B1.SS", "value_score"] > 80 and scored.loc["T2.SZ", "value_score"] > 60
    assert scored.loc["L1.SZ", "value_score"] < scored.loc["T2.SZ", "value_score"]   # a loss: no earnings yield


@pytest.mark.unit
def test_missing_metrics_are_neutral_and_unknown_weights_ignored():
    pool = _pool().drop(columns=["dividend_yield"])
    scored = scoring.score(pool, {"value": 1})
    assert scored["value_score"].between(0, 100).all()
    assert scoring.weights_for(None, {"value": 2, "made_up": 5, "growth": -1}) == {"value": 2.0}
    assert scoring.weights_for("nope", None) == scoring.PRESETS["balanced"].weights
    assert scoring.rank(pool.iloc[0:0], {"value": 1}, 5, "2026-10-16").empty


@pytest.mark.unit
def test_the_leaders_get_price_metrics_from_daily_bars():
    days = pd.bdate_range("2026-01-01", periods=140)
    steady = pd.DataFrame({"Date": days, "Close": [10 + i * 0.01 for i in range(140)]})
    wild = pd.DataFrame({"Date": days, "Close": [10 + (3 if i % 2 else -3) + i * 0.01 for i in range(140)]})
    asked = []

    def load(ticker, as_of):
        asked.append(ticker)
        if ticker == "B3.SS":
            raise RuntimeError("no bars")
        return wild if ticker == "B1.SS" else steady

    out = scoring.rank(_pool(), {"low_vol": 1}, 3, "2026-10-16", load=load, tech_top=5).set_index("ticker")
    assert len(asked) == 5                                          # only the leaders are fetched
    assert "B1.SS" not in out.index                                 # the volatile one drops back
    m = scoring.tech_metrics(steady)
    assert m["vol_60"] < 1 and m["drawdown_60"] == pytest.approx(0) and m["mom_120_20"] > 0



@pytest.mark.unit
def test_a_small_industry_is_ranked_across_the_pool():
    """Four chip makers are fewer than MIN_PEERS: they are ranked against the whole pool,
    where the banks' far lower PE puts them behind."""
    scored = scoring.score(_pool(), {"value": 1}).set_index("ticker")
    assert scored.loc["T2.SZ", "value_score"] < 60 and scored.loc["B1.SS", "value_score"] == 100
