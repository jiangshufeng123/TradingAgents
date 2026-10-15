"""Natural-language screening: the model writes conditions, code applies them."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents import screener
from tradingagents.dataflows.vendors import akshare_cn

SPOT = pd.DataFrame({
    "代码": ["600519", "000858", "300750", "688981", "430047", "600000"],
    "名称": ["贵州茅台", "五粮液", "宁德时代", "中芯国际", "诺思兰德", "*ST浦发"],
    "最新价": [1500, 130, 250, 90, 10, 8], "涨跌幅": [1.0, 0.5, -1.0, 2.0, 0.0, -5.0],
    "换手率": [0.3, 0.6, 1.2, 2.0, 3.0, 1.0], "量比": [1.0, 1.1, 0.9, 1.5, 1.0, 1.0],
    "成交额": [5e9, 3e9, 8e9, 6e9, 1e8, 2e8],
    "市盈率-动态": [22.0, 15.0, 25.0, 80.0, -5.0, 6.0], "市净率": [8.0, 4.0, 5.0, 3.0, 2.0, 0.5],
    "总市值": [1.9e12, 5e11, 1.1e12, 7e11, 3e9, 2.4e11], "流通市值": [1.9e12, 5e11, 1e12, 2e11, 2e9, 2.4e11],
    "60日涨跌幅": [5.0, -3.0, 20.0, 35.0, 1.0, -10.0], "年初至今涨跌幅": [10, 0, 30, 50, 2, -20],
})
RESULTS = pd.DataFrame({
    "股票代码": ["600519", "000858", "300750", "688981", "600000"],
    "所处行业": ["酿酒行业", "酿酒行业", "电池", "半导体", "银行"],
    "营业总收入-同比增长": [10.0, 5.0, 20.0, 30.0, -2.0], "净利润-同比增长": [12.0, 3.0, 25.0, 40.0, -50.0],
    "净资产收益率": [18.0, 12.0, 10.0, 3.0, 4.0], "销售毛利率": [91.0, 75.0, 25.0, 20.0, 0.0],
    "每股收益": [30.0, 5.0, 8.0, 0.5, 0.3], "每股经营现金流量": [25.0, 4.0, 10.0, 1.0, 0.0],
})


@pytest.fixture
def universe(tmp_path, monkeypatch):
    def fake_call(func, **kw):
        return {"stock_zh_a_spot_em": SPOT, "stock_yjbb_em": RESULTS}[func].copy()

    monkeypatch.setattr(akshare_cn, "call", fake_call)
    monkeypatch.setattr(screener, "results_paged", lambda period, cache: RESULTS.copy())
    monkeypatch.setattr(screener, "latest_complete_period", lambda today=None: "20260630")
    df, period = screener.build_universe({"data_cache_dir": str(tmp_path)})
    return df


@pytest.mark.unit
def test_latest_complete_period():
    assert screener.latest_complete_period(date(2026, 10, 15)) == "20260630"
    assert screener.latest_complete_period(date(2026, 11, 1)) == "20260930"
    assert screener.latest_complete_period(date(2027, 3, 1)) == "20260930"
    assert screener.latest_complete_period(date(2027, 5, 1)) == "20270331"


@pytest.mark.unit
def test_universe_joins_quotes_and_results(universe):
    row = universe.set_index("code").loc["600519"]
    assert row["ticker"] == "600519.SS" and row["mcap"] == pytest.approx(19000)
    assert row["roe_annualized"] == pytest.approx(36.0) and row["industry"] == "酿酒行业"
    assert pd.isna(universe.set_index("code").loc["430047", "ticker"])     # Beijing: no Yahoo prices
    assert bool(universe.set_index("code").loc["600000", "st"])


@pytest.mark.unit
def test_conditions_industries_sort_and_st(universe):
    spec = screener.Spec(filters=[{"field": "pe", "op": "between", "value": [0, 30]},
                                  {"field": "roe_annualized", "op": ">", "value": 15}],
                         industries=["酿酒"], sort=[{"field": "mcap", "desc": True}])
    result = screener.apply(spec, universe)
    assert list(result["ticker"]) == ["600519.SS", "000858.SZ"]
    cheap = screener.apply(screener.Spec(filters=[{"field": "pe", "op": "<", "value": 10}]), universe)
    assert cheap.empty                                                     # only the ST bank, excluded
    assert len(screener.apply(screener.Spec(filters=[{"field": "pe", "op": "<", "value": 10}],
                                            exclude_st=False), universe)) == 1


@pytest.mark.unit
def test_where_syntax():
    spec = screener.parse_where("pe<20， roe_annualized>=15; mcap>100")
    assert [(f["field"], f["op"], f["value"]) for f in spec.filters] == [
        ("pe", "<", 20.0), ("roe_annualized", ">=", 15.0), ("mcap", ">", 100.0)]
    with pytest.raises(ValueError):
        screener.parse_where("dividend>3")


@pytest.mark.unit
def test_the_model_only_writes_conditions_and_bad_ones_are_dropped(universe):
    answer = ('Sure:\n{"filters": [{"field": "pe", "op": "<", "value": 30}, {"field": "made_up", "op": "<", "value": 1},'
              ' {"field": "pb", "op": "~", "value": 1}], "industries": ["酿酒行业"], "sort": [{"field": "roe", "desc": true}],'
              ' "limit": 5, "explanation": "低估值白酒股"}')
    seen = []

    class LLM:
        def invoke(self, prompt):
            seen.append(prompt)
            return SimpleNamespace(content=answer)

    spec = screener.interpret("便宜的白酒股", LLM(), ["酿酒行业", "电池"], "20260630")
    assert [f["field"] for f in spec.filters] == ["pe"] and spec.limit == 5
    assert "酿酒行业, 电池" in seen[0] and "便宜的白酒股" in seen[0]
    assert "pe (dynamic P/E" in spec.describe() and "industry in: 酿酒行业" in spec.describe()
    result = screener.apply(spec, universe)
    assert list(result["ticker"]) == ["600519.SS", "000858.SZ"]


@pytest.mark.unit
def test_results_are_saved_for_daily(universe, tmp_path):
    spec = screener.Spec(industries=["酿酒"])
    result = screener.apply(spec, universe)
    csv_path, latest = screener.save(result, spec, "白酒", {"reports_dir": str(tmp_path)})
    assert csv_path.exists() and latest.read_text(encoding="utf-8").split() == ["600519.SS", "000858.SZ"]
    assert "贵州茅台" in screener.render(result, spec, "20260630")


@pytest.mark.unit
def test_daily_reads_a_ticker_file(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as cli_main

    (tmp_path / "t.txt").write_text("600519.SS\n# note\n000858.SZ\n", encoding="utf-8")
    (tmp_path / "t.csv").write_text("ticker,name\n300750.SZ,宁德时代\n", encoding="utf-8")
    assert cli_main._tickers_from_file(str(tmp_path / "t.txt")) == ["600519.SS", "000858.SZ"]
    assert cli_main._tickers_from_file(str(tmp_path / "t.csv")) == ["300750.SZ"]

    ran = []
    monkeypatch.setattr(cli_main, "run_analysis", lambda portfolio=None, flags=None: ran.append(flags["ticker"]) or {})
    monkeypatch.setattr(cli_main, "unattended_gaps", lambda flags: [])
    result = CliRunner().invoke(cli_main.app, ["daily", "NVDA", "--from-file", str(tmp_path / "t.txt")])
    assert result.exit_code == 0, result.output
    assert ran == ["NVDA", "600519.SS", "000858.SZ"]


@pytest.mark.unit
def test_screen_command_with_where(universe, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import cli.main as cli_main

    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "data_cache_dir", str(tmp_path))
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "watchlist_file", str(tmp_path / "w.txt"))
    result = CliRunner().invoke(cli_main.app, ["screen", "--where", "pe<30, roe_annualized>15"])
    assert result.exit_code == 0, result.output
    latest = tmp_path / "reports" / "screens" / "latest.txt"
    assert latest.read_text(encoding="utf-8").split() == ["600519.SS", "300750.SZ", "000858.SZ"]
    assert "Nothing added to the watchlist" in result.output          # no terminal, no --add: nothing joins


@pytest.mark.unit
def test_sina_fills_in_when_east_money_is_down(tmp_path, monkeypatch):
    import json as _json

    import requests

    rows = [{"symbol": "sh600519", "code": "600519", "name": "贵州茅台", "trade": "1500", "changepercent": "1.0",
             "turnoverratio": "0.3", "amount": "5000000000", "per": "22.0", "pb": "8.0",
             "mktcap": "190000000", "nmc": "190000000"}]

    class Resp:
        def __init__(self, text):
            self.text = text

    def fake_get(url, params=None, timeout=None):
        return Resp("1" if "Count" in url else _json.dumps(rows))

    def fake_call(func, **kw):
        if func == "stock_zh_a_spot_em":
            raise akshare_cn.VendorUnavailableError("push2 down")
        return RESULTS.copy()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(screener.time, "sleep", lambda s: None)
    monkeypatch.setattr(akshare_cn, "call", fake_call)
    monkeypatch.setattr(screener, "results_paged", lambda period, cache: RESULTS.copy())
    df, _ = screener.build_universe({"data_cache_dir": str(tmp_path)})
    row = df.set_index("code").loc["600519"]
    assert df.attrs["source"] == "Sina" and row["mcap"] == pytest.approx(19000) and row["pe"] == 22.0
    assert "chg_60d" in df.attrs["missing"] and "pe" not in df.attrs["missing"]
    spec = screener.Spec(filters=[{"field": "chg_60d", "op": ">", "value": 10}])
    assert screener.unavailable(spec, df) == ["chg_60d"]
    # the model is not offered fields the source lacks
    assert "chg_60d" not in screener._prompt("x", [], "20260630", df.attrs["missing"])


@pytest.mark.unit
def test_without_quarterly_results_the_screen_runs_on_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(akshare_cn, "call", lambda func, **kw: SPOT.copy())

    def down(period, cache):
        raise RuntimeError("results page 5 failed")

    monkeypatch.setattr(screener, "results_paged", down)
    df, _ = screener.build_universe({"data_cache_dir": str(tmp_path)})
    assert "roe_annualized" in df.attrs["missing"] and "pe" not in df.attrs["missing"]
    spec = screener.Spec(filters=[{"field": "pe", "op": "<", "value": 20},
                                  {"field": "roe_annualized", "op": ">", "value": 15}], industries=["酿酒"])
    assert screener.apply(spec, df).empty                    # a missing field matches nothing, no error
    assert len(screener.apply(screener.Spec(filters=[{"field": "pe", "op": "<", "value": 20}]), df)) == 1


@pytest.mark.unit
def test_results_are_paged_with_retries_and_resume(tmp_path, monkeypatch):
    import requests

    calls = []
    fail_once = {"2": True}

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_get(self, url, params=None, timeout=None):
        n = params["pageNumber"]
        calls.append(n)
        if fail_once.pop(n, False):
            raise requests.exceptions.ChunkedEncodingError("Response ended prematurely")
        rows = [{"SECURITY_CODE": f"60000{n}", "WEIGHTAVG_ROE": 10.0, "PUBLISHNAME": "银行", "OTHER": 1}]
        return Resp({"result": {"pages": 3, "data": rows}})

    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr(screener.time, "sleep", lambda s: None)
    df = screener.results_paged("20260630", tmp_path)
    assert calls == ["1", "2", "2", "3"]
    assert list(df.columns) == ["股票代码", "净资产收益率", "所处行业"] and len(df) == 3
    calls.clear()
    screener.results_paged("20260630", tmp_path)                # every page is on disk now
    assert calls == []


@pytest.mark.unit
def test_call_takes_a_longer_single_attempt(monkeypatch):
    seen = []

    def fake_with_timeout(func, kwargs, seconds):
        seen.append((kwargs, seconds))
        return pd.DataFrame({"x": [1]})

    monkeypatch.setattr(akshare_cn, "_with_timeout", fake_with_timeout)
    monkeypatch.setattr(akshare_cn, "_ak", lambda: SimpleNamespace(some_table=lambda **k: None))
    akshare_cn._CACHE.clear()
    akshare_cn.call("some_table", date="20260630", _timeout=300, _retries=1)
    assert seen == [({"date": "20260630"}, 300)]


@pytest.mark.unit
def test_a_model_that_never_answers_times_out():
    import threading

    stop = threading.Event()

    class Stuck:
        def invoke(self, prompt):
            stop.wait(5)
            return SimpleNamespace(content="{}")

    with pytest.raises(TimeoutError, match="--where"):
        screener.interpret("x", Stuck(), [], "20260630", timeout=0.6)
    stop.set()


@pytest.mark.unit
def test_progress_is_reported_while_waiting(monkeypatch):
    import threading

    clock = {"t": 0.0}
    monkeypatch.setattr(screener.time, "time", lambda: clock["t"])
    release = threading.Event()

    class Slow:
        def invoke(self, prompt):
            release.wait(5)
            return SimpleNamespace(content='{"filters": []}')

    seen = []

    def progress(s):
        seen.append(s)
        if len(seen) == 2:
            release.set()

    real_join = threading.Thread.join

    def join(self, timeout=None):
        clock["t"] += 5                      # each wait slice advances the fake clock 5 s
        real_join(self, 0.01)

    monkeypatch.setattr(threading.Thread, "join", join)
    spec = screener.interpret("x", Slow(), [], "20260630", timeout=120, progress=progress)
    assert seen[:2] == [10.0, 20.0] and spec.filters == []
