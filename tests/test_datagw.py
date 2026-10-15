"""The data gateway: fallback, quality checks, circuit breaking, caching; Tushare adapter; model balance."""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents import datagw, llm_balance
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.datagw import core, quality, tushare_pro
from tradingagents.datagw.core import (
    Gateway,
    GatewayNoDataError,
    GatewayUnavailableError,
    QualityError,
)


def _check_positive(df, kwargs):
    if (df["x"] <= 0).any():
        raise QualityError("non-positive x")


def _gw(tmp_path, **kw):
    gw = Gateway(tmp_path, **kw)
    gw.capability("thing", "Thing", check=_check_positive)
    return gw


@pytest.mark.unit
def test_falls_back_past_failures_and_bad_data(tmp_path):
    gw = _gw(tmp_path)
    calls = []

    def down(**kw):
        calls.append("a")
        raise ConnectionError("reset")

    def bad(**kw):
        calls.append("b")
        return pd.DataFrame({"x": [-1]})

    gw.provider("thing", "a", "A", down)
    gw.provider("thing", "b", "B", bad)
    gw.provider("thing", "c", "C", lambda **kw: pd.DataFrame({"x": [1]}))
    r = gw.fetch("thing", q=1)
    assert r.source == "C" and calls == ["a", "b"]
    assert [why.split(" ")[0] for _, why in r.attempts] == ["ConnectionError:", "bad", "ok"]
    rows = {row["provider"]: row for row in gw.status()}
    assert rows["a"]["fail"] == 1 and rows["b"]["last_error"].startswith("quality")
    assert rows["c"]["state"] == "ok" and rows["c"]["ok"] == 1


@pytest.mark.unit
def test_unconfigured_source_is_skipped_without_a_failure(tmp_path):
    gw = _gw(tmp_path)
    gw.provider("thing", "a", "A", lambda **kw: 1 / 0, configured=lambda: "no token")
    gw.provider("thing", "b", "B", lambda **kw: pd.DataFrame({"x": [2]}))
    r = gw.fetch("thing")
    assert r.source == "B" and r.attempts[0] == ("A", "skipped (no token)")
    assert {row["provider"]: row["state"] for row in gw.status()}["a"] == "not configured"


@pytest.mark.unit
def test_circuit_opens_after_repeated_failures_and_retries_later(tmp_path, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(core.time, "time", lambda: clock["t"])
    gw = _gw(tmp_path, breaker_failures=2, breaker_seconds=60)
    tried = []

    def flaky(**kw):
        tried.append(clock["t"])
        raise TimeoutError("slow")

    gw.provider("thing", "a", "A", flaky)
    gw.provider("thing", "b", "B", lambda **kw: pd.DataFrame({"x": [1]}))
    gw.fetch("thing")
    gw.fetch("thing")
    assert len(tried) == 2
    r = gw.fetch("thing")                                    # open: not asked
    assert len(tried) == 2 and "circuit open" in r.attempts[0][1]
    assert {row["provider"]: row["state"] for row in gw.status()}["a"] == "circuit open"
    clock["t"] += 61                                         # half-open: one try
    gw.fetch("thing")
    assert len(tried) == 3
    gw.provider("thing", "a", "A", lambda **kw: pd.DataFrame({"x": [5]}))
    clock["t"] += 61
    assert gw.fetch("thing").source == "A"                   # success closes it
    assert gw.health.allow("thing", "a")


@pytest.mark.unit
def test_empty_answers(tmp_path):
    gw = _gw(tmp_path)
    gw.provider("thing", "a", "A", lambda **kw: pd.DataFrame())
    gw.provider("thing", "b", "B", lambda **kw: (_ for _ in ()).throw(NoMarketDataError("X", "X", "none")))
    with pytest.raises(GatewayNoDataError):
        gw.fetch("thing")
    gw.capability("listing", "Listing", empty_ok=True)
    gw.provider("listing", "a", "A", lambda **kw: pd.DataFrame())
    gw.provider("listing", "b", "B", lambda **kw: 1 / 0)
    r = gw.fetch("listing")
    assert r.source == "A" and r.data.empty                  # "not listed" is an answer
    gw.capability("dead", "Dead")
    gw.provider("dead", "a", "A", lambda **kw: 1 / 0)
    with pytest.raises(GatewayUnavailableError, match="ZeroDivisionError"):
        gw.fetch("dead")


@pytest.mark.unit
def test_cache_is_shared_on_disk_for_its_ttl(tmp_path, monkeypatch):
    calls = []

    def fetch(**kw):
        calls.append(kw)
        df = pd.DataFrame({"x": [1]})
        df.attrs["as_of"] = "2026-10-14"
        return df

    one = _gw(tmp_path)
    one.provider("thing", "a", "A", fetch)
    one.fetch("thing", ttl=3600, day="2026-10-15")
    other = _gw(tmp_path)                                     # another process, same cache folder
    other.provider("thing", "a", "A", fetch)
    r = other.fetch("thing", ttl=3600, day="2026-10-15")
    assert r.cached and r.source == "A" and r.data.attrs["as_of"] == "2026-10-14" and len(calls) == 1
    other.fetch("thing", ttl=3600, day="2026-10-16")          # different arguments: asked again
    assert len(calls) == 2
    other.fetch("thing", day="2026-10-15")                    # no ttl: always asked
    assert len(calls) == 3


@pytest.mark.unit
def test_order_setting_and_health_file_survive_a_restart(tmp_path):
    assert core.parse_order("daily_bars=sina, tushare ; cn_spot=tushare") == {
        "daily_bars": ["sina", "tushare"], "cn_spot": ["tushare"]}
    gw = datagw.build(tmp_path, order="daily_bars=sina,tushare")
    assert [p.name for p in gw.providers("daily_bars")] == ["sina", "tushare"]
    assert [p.name for p in gw.providers("lhb")] == ["tushare", "eastmoney"]
    gw.health.fail("lhb", "eastmoney", "boom")
    again = datagw.build(tmp_path)
    row = next(r for r in again.status() if r["provider"] == "eastmoney" and r["capability"] == "lhb")
    assert row["fail"] == 1 and row["last_error"] == "boom" and row["state"] == "failing"


@pytest.mark.unit
def test_probe_asks_one_source_even_with_an_open_circuit(tmp_path):
    gw = _gw(tmp_path, breaker_failures=1)
    gw.provider("thing", "a", "A", lambda **kw: 1 / 0)
    assert gw.probe("thing", "a")[0] == "failed"
    assert not gw.health.allow("thing", "a")
    gw.provider("thing", "a", "A", lambda **kw: pd.DataFrame({"x": [1, 2]}))
    state, detail, _ = gw.probe("thing", "a")
    assert state == "ok" and detail == "2 rows" and gw.health.allow("thing", "a")


@pytest.mark.unit
def test_bar_quality_check():
    good = pd.DataFrame({"Date": pd.to_datetime(["2026-10-13", "2026-10-14"]), "Open": [10, 11],
                         "High": [11, 12], "Low": [9.5, 10.5], "Close": [10.8, 11.5], "Volume": [100, 200]})
    quality.check_bars(good, {"start": "2026-10-13", "end": "2026-10-14"})
    for broken, why in [(good.assign(Close=[10.8, 0]), "non-positive"),
                        (good.assign(Close=[10.8, 20]), "outside"),
                        (good.assign(Low=[12, 10.5]), "high below low"),
                        (good.assign(Date=pd.to_datetime(["2026-10-14"] * 2)), "duplicate")]:
        with pytest.raises(QualityError, match=why):
            quality.check_bars(broken, {})
    with pytest.raises(QualityError, match="outside the requested"):
        quality.check_bars(good, {"start": "2026-10-14", "end": "2026-10-14"})


@pytest.mark.unit
def test_spot_quality_check_rejects_a_truncated_table(monkeypatch):
    monkeypatch.setattr(quality, "MIN_A_SHARES", 3000)
    df = pd.DataFrame({c: [1.0] * 10 for c in quality.SPOT_COLUMNS})
    with pytest.raises(QualityError, match="only 10 listings"):
        quality.check_spot(df, {})


# --- Tushare adapter ----------------------------------------------------------------

@pytest.fixture
def fake_tushare(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    asked = []
    tables = {}

    def query(api_name, **kw):
        asked.append((api_name, kw))
        table = tables.get(api_name)
        return table(**kw) if callable(table) else (table.copy() if table is not None else pd.DataFrame())

    monkeypatch.setattr(tushare_pro, "query", query)
    return tables, asked


@pytest.mark.unit
def test_tushare_codes():
    assert tushare_pro.ts_code("600519.SS") == ("sh", "600519.SH")
    assert tushare_pro.ts_code("000960.SZ") == ("sz", "000960.SZ")
    assert tushare_pro.ts_code("6616.HK") == ("hk", "06616.HK")
    with pytest.raises(NoMarketDataError):
        tushare_pro.ts_code("NVDA")


@pytest.mark.unit
def test_tushare_bars_convert_lots_to_shares_and_sort(fake_tushare):
    tables, asked = fake_tushare
    tables["daily"] = pd.DataFrame({"trade_date": ["20261014", "20261013"], "open": [21.0, 20.5],
                                    "high": [21.6, 21.5], "low": [20.9, 20.1], "close": [21.4, 21.3],
                                    "vol": [600.0, 500.0]})
    bars = tushare_pro.daily_bars("000960.SZ", pd.Timestamp("2026-10-13"), pd.Timestamp("2026-10-14"))
    assert list(bars["Date"]) == [pd.Timestamp("2026-10-13"), pd.Timestamp("2026-10-14")]
    assert bars["Volume"].tolist() == [50000.0, 60000.0]
    assert asked[0] == ("daily", {"ts_code": "000960.SZ", "start_date": "20261013", "end_date": "20261014"})
    with pytest.raises(NoMarketDataError):          # hk_daily: one call an hour at 2,000 points
        tushare_pro.daily_bars("6616.HK", pd.Timestamp("2026-10-13"), pd.Timestamp("2026-10-14"))


@pytest.mark.unit
def test_gateway_serves_bars_from_tushare_first(fake_tushare):
    tables, _ = fake_tushare
    tables["daily"] = pd.DataFrame({"trade_date": ["20261013"], "open": [20.5], "high": [21.5],
                                    "low": [20.1], "close": [21.3], "vol": [500.0]})
    from tradingagents.dataflows.vendors import akshare_cn

    bars, source = akshare_cn.daily_bars("000960.SZ", pd.Timestamp("2026-10-13"), pd.Timestamp("2026-10-13"))
    assert source == "Tushare" and bars["Close"].iloc[0] == 21.3


@pytest.mark.unit
def test_tushare_spot_maps_units_and_changes(fake_tushare):
    tables, _ = fake_tushare
    days = pd.bdate_range("2025-12-01", "2026-10-14")
    tables["trade_cal"] = lambda **kw: pd.DataFrame(
        {"cal_date": [f"{d:%Y%m%d}" for d in days if kw["start_date"] <= f"{d:%Y%m%d}" <= kw["end_date"]]})
    tables["daily_basic"] = lambda **kw: (pd.DataFrame() if kw["trade_date"] == "20261014" else pd.DataFrame(
        {"ts_code": ["600519.SH"], "close": [1500.0], "turnover_rate": [0.3], "volume_ratio": [1.1],
         "pe_ttm": [22.0], "pb": [8.0], "total_mv": [1.9e8], "circ_mv": [1.9e8]}))
    tables["daily"] = lambda **kw: pd.DataFrame({"ts_code": ["600519.SH"], "pct_chg": [1.0], "amount": [5e6],
                                                 "close": [1500.0 if kw["trade_date"] == "20261013" else 1200.0]})
    tables["adj_factor"] = pd.DataFrame({"ts_code": ["600519.SH"], "adj_factor": [1.0]})
    tables["stock_basic"] = pd.DataFrame({"ts_code": ["600519.SH"], "name": ["贵州茅台"], "industry": ["白酒"]})
    df = tushare_pro.cn_spot("2026-10-14")
    row = df.iloc[0]
    assert df.attrs["as_of"] == "2026-10-13"                  # today's table not out yet
    assert row["代码"] == "600519" and row["总市值"] == pytest.approx(1.9e12) and row["成交额"] == 5e9
    assert row["60日涨跌幅"] == pytest.approx(25.0) and row["年初至今涨跌幅"] == pytest.approx(25.0)
    assert row["行业"] == "白酒" and row["量比"] == 1.1


@pytest.mark.unit
def test_tushare_lhb_and_margin(fake_tushare):
    tables, asked = fake_tushare
    tables["trade_cal"] = pd.DataFrame({"cal_date": ["20261009", "20261012"]})
    tables["top_list"] = lambda **kw: pd.DataFrame(
        {"trade_date": [kw["trade_date"]], "reason": ["日涨幅偏离值达7%"], "pct_change": [10.0],
         "net_amount": [1.2e8], "net_rate": [8.5], "turnover_rate": [12.0]}) if kw["trade_date"] == "20261009" \
        else pd.DataFrame()
    df = tushare_pro.lhb("603980.SS", pd.Timestamp("2026-10-09"), pd.Timestamp("2026-10-12"))
    assert len(df) == 1 and df["net_buy"].iloc[0] == 1.2e8
    assert [kw["ts_code"] for api, kw in asked if api == "top_list"] == ["603980.SH", "603980.SH"]
    tables["margin_detail"] = pd.DataFrame({"rzye": [2.2e8], "rzmre": [3e7], "rqyl": [1000]})
    row = tushare_pro.margin("603980.SS", "2026-10-13")
    assert row["balance"] == 2.2e8 and row["short"] == 1000
    tables["margin_detail"] = pd.DataFrame()
    assert tushare_pro.margin("603980.SS", "2026-10-13") is None


@pytest.mark.unit
def test_tushare_without_permission_is_skipped_not_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    class Api:
        def query(self, name, **kw):
            raise Exception("抱歉，您没有接口(top_list)访问权限")

    monkeypatch.setattr(tushare_pro, "_pro", lambda: Api())
    monkeypatch.setattr(tushare_pro.time, "sleep", lambda s: None)
    gw = datagw.build(tmp_path)
    gw.provider("lhb", "eastmoney", "East Money", lambda **kw: pd.DataFrame(columns=quality.LHB_COLUMNS))
    r = gw.fetch("lhb", ticker="603980.SS", start=pd.Timestamp("2026-10-09"), end=pd.Timestamp("2026-10-12"))
    assert r.source == "East Money" and "no permission" in r.attempts[0][1]
    assert gw.health.snapshot().get("lhb/tushare", {}).get("fail", 0) == 0


# --- model balance --------------------------------------------------------------------

@pytest.mark.unit
def test_deepseek_balance(monkeypatch):
    import requests

    seen = {}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"is_available": True, "balance_infos": [
                {"currency": "CNY", "total_balance": "110.00", "granted_balance": "10.00",
                 "topped_up_balance": "100.00"}]}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, auth=headers["Authorization"])
        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    llm_balance._cache.clear()
    info = llm_balance.balance("deepseek")
    assert seen == {"url": "https://api.deepseek.com/user/balance", "auth": "Bearer sk-secret"}
    assert info["ok"] and info["balances"][0]["total"] == 110.0 and info["balances"][0]["topped_up"] == 100.0
    assert "sk-secret" not in repr(info)
    assert "CNY 110.00" in llm_balance.summary_line(info)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    llm_balance._cache.clear()
    assert llm_balance.balance("deepseek") == {"provider": "deepseek", "ok": False,
                                               "error": "DEEPSEEK_API_KEY not set"}
    assert not llm_balance.balance("openai")["ok"]


@pytest.mark.unit
def test_datasource_status_command(monkeypatch):
    from typer.testing import CliRunner

    from cli.display import console
    from cli.main import app

    monkeypatch.setattr(console, "width", 220)
    result = CliRunner().invoke(app, ["datasource", "status"])
    assert result.exit_code == 0, result.output
    assert "Tushare" in result.output and "not configured" in result.output


@pytest.mark.unit
def test_not_this_market_leaves_a_source_record_alone(tmp_path):
    gw = _gw(tmp_path, breaker_failures=1)
    gw.provider("thing", "a", "A", lambda **kw: 1 / 0)
    with pytest.raises(GatewayUnavailableError):
        gw.fetch("thing")
    gw.provider("thing", "a", "A", lambda **kw: (_ for _ in ()).throw(NoMarketDataError("X", "X", "HK")))
    assert gw.probe("thing", "a")[0] == "empty"
    row = next(r for r in gw.status() if r["provider"] == "a")
    assert row["ok"] == 0 and row["streak"] == 1                # still failing, not "ok"


@pytest.mark.unit
def test_tushare_rate_limit_is_a_failure_with_a_clean_message(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")

    class Api:
        def query(self, name, **kw):
            raise Exception("\n抱歉，您访问接口(hk_daily)频率超限(1次/小时)")

    monkeypatch.setattr(tushare_pro, "_pro", lambda: Api())
    with pytest.raises(tushare_pro.TushareRateLimited, match="call limit reached"):
        tushare_pro.query("daily", ts_code="600519.SH")


@pytest.mark.unit
def test_balance_line_skips_empty_currencies():
    info = {"provider": "deepseek", "ok": True, "available": True, "balances": [
        {"currency": "USD", "total": 0.0, "granted": 0.0, "topped_up": 0.0},
        {"currency": "CNY", "total": 118.85, "granted": 0.0, "topped_up": 118.85}]}
    line = llm_balance.summary_line(info)
    assert "USD" not in line and "CNY 118.85" in line
