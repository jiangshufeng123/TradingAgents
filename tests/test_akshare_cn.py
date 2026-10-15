"""A-share and Hong Kong data through AkShare, point-in-time, and out of the way for other markets."""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.dataflows import router
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.vendors import akshare_cn as cn

TODAY = "2026-10-14"


def _frames():
    return {
        "stock_news_em": pd.DataFrame({
            "关键词": ["600519"] * 3,
            "新闻标题": ["茅台重返股王", "茅台三季报前瞻", "旧闻"],
            "新闻内容": ["10月12日，贵州茅台涨1.98%", "市场关注", "x"],
            "发布时间": ["2026-10-12 15:14:00", "2026-10-13 20:07:00", "2026-10-11 09:00:00"],
            "文章来源": ["人民财讯", "海报新闻", "某报"],
            "新闻链接": ["u1", "u2", "u3"],
        }),
        "stock_financial_report_sina": pd.DataFrame({
            "报告日": ["20260630", "20260331", "20251231", "20241231"],
            "营业总收入": [92278072083.21, 50600000000.0, 175000000000.0, 170000000000.0],
            "净利润": [46000000000.0, 27000000000.0, 89000000000.0, 86000000000.0],
            "基本每股收益": [35.57, 21.0, 70.1, 68.0],
            "公告日期": ["2026-08-07", "2026-04-28", "2026-04-02", "2025-04-03"],
        }),
        "stock_financial_abstract_ths": pd.DataFrame({
            "报告期": ["2024-12-31", "2025-12-31", "2026-03-31", "2026-06-30"],
            "营业总收入": ["1700亿", "1750亿", "506亿", "922.78亿"],
            "净利润同比增长率": ["15%", "3%", "1%", "-1.95%"],
            "销售毛利率": ["91%", "90%", "90%", "89.56%"],
        }),
        "stock_value_em": pd.DataFrame({
            "数据日期": ["2025-10-14", "2026-06-30", "2026-10-12", "2026-10-13"],
            "当日收盘价": [1500.0, 1400.0, 1288.0, 1274.89],
            "总市值": [1.88e12, 1.75e12, 1.61e12, 1.593717e12],
            "PE(TTM)": [24.0, 21.0, 19.8, 19.57],
            "市净率": [8.0, 7.0, 6.4, 6.34],
        }),
        "stock_individual_fund_flow": pd.DataFrame({
            "日期": ["2026-10-09", "2026-10-12", "2026-10-13"],
            "收盘价": [1263.0, 1288.0, 1274.89], "涨跌幅": [0.2, 1.98, -1.02],
            "主力净流入-净额": [-1.2e8, 3.4e8, 5.04e7], "主力净流入-净占比": [-2.0, 6.0, 1.49],
            "超大单净流入-净额": [-0.5e8, 2.0e8, -1.88e7], "小单净流入-净额": [1e6, -2e6, -1.4e5],
        }),
        "macro_china_cpi": pd.DataFrame({"月份": ["2026年09月份", "2026年08月份"],
                                         "全国-同比增长": [0.9, 0.6], "全国-环比增长": [0.2, 0.1]}),
        "macro_china_pmi": pd.DataFrame({"月份": ["2026年09月份", "2026年08月份"],
                                         "制造业-指数": [50.1, 49.8], "非制造业-指数": [50.2, 50.0]}),
        "macro_china_lpr": pd.DataFrame({"TRADE_DATE": ["2026-09-22", "2026-08-20"],
                                         "LPR1Y": [3.0, 3.0], "LPR5Y": [3.5, 3.5]}),
        "stock_info_global_cls": pd.DataFrame({"标题": ["医保局"], "内容": ["【医保局】支持医药创新"],
                                               "发布日期": ["2026-10-14"], "发布时间": ["11:00:00"]}),
        "stock_info_global_em": pd.DataFrame({"标题": ["宝马"], "摘要": ["宝马董事长访华"],
                                              "发布时间": ["2026-10-14 12:18:38"], "链接": ["u"]}),
        "stock_hsgt_fund_flow_summary_em": pd.DataFrame({
            "交易日": ["2026-10-14"], "类型": ["沪港通"], "板块": ["沪股通"], "资金方向": ["北向"],
            "成交净买额": [0.0], "资金净流入": [0.0], "相关指数": ["上证指数"], "指数涨跌幅": [0.01]}),
        "stock_profile_cninfo": pd.DataFrame({"公司名称": ["贵州茅台酒股份有限公司"], "A股简称": ["贵州茅台"],
                                              "所属市场": ["上交所"], "所属行业": ["酒、饮料和精制茶制造业"],
                                              "上市日期": ["2001-08-27"], "入选指数": ["沪深300"], "主营业务": ["茅台酒"]}),
        "stock_financial_hk_report_em": pd.DataFrame({
            "REPORT_DATE": ["2026-06-30 00:00:00", "2026-06-30 00:00:00", "2025-12-31 00:00:00", "2025-12-31 00:00:00"],
            "STD_ITEM_CODE": ["004001001", "004009999", "004001001", "004009999"],
            "STD_ITEM_NAME": ["营业额", "股东应占溢利", "营业额", "股东应占溢利"],
            "AMOUNT": [3.96431e11, 1.1e11, 7.5e11, 2.0e11],
        }),
        "stock_hk_valuation_baidu": pd.DataFrame({"date": ["2025-10-14", "2026-10-13"], "value": [24.92, 21.5]}),
        "stock_hk_company_profile_em": pd.DataFrame({"公司名称": ["腾讯控股有限公司"], "英文名称": ["Tencent"],
                                                     "所属行业": ["软件服务"], "年结日": ["12-31"],
                                                     "员工人数": [115927], "公司介绍": ["互联网科技公司"]}),
        "stock_hk_financial_indicator_em": pd.DataFrame({"总市值(港元)": [3.866e12], "市盈率": [14.49],
                                                         "市净率": [2.96], "股息率TTM(%)": [1.25]}),
    }


@pytest.fixture
def fake(monkeypatch):
    frames = _frames()
    calls = []

    def call(name, **kwargs):
        calls.append((name, kwargs))
        return frames[name].copy()

    monkeypatch.setattr(cn, "call", call)
    monkeypatch.setattr(cn, "get_current_date", lambda: TODAY)
    monkeypatch.setattr("tradingagents.dataflows.date_window.get_current_date", lambda: TODAY)
    monkeypatch.setattr(cn, "_ak", lambda: object())
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("ticker, expected", [
    ("600519.SS", ("sh", "600519")), ("600519.SH", ("sh", "600519")), ("000001.SZ", ("sz", "000001")),
    ("0700.HK", ("hk", "00700")), ("700.HK", ("hk", "00700")), ("09992.HK", ("hk", "09992")),
    ("NVDA", None), ("7203.T", None), ("BTC-USD", None),
])
def test_only_a_share_and_hk_symbols_are_served(ticker, expected):
    assert cn.market_of(ticker) == expected


@pytest.mark.unit
def test_other_markets_pass_straight_to_the_next_vendor(monkeypatch):
    monkeypatch.setattr(router, "get_config", lambda: {"data_vendors": {"news_data": "akshare,yfinance"}, "tool_vendors": {}})
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance", lambda *a, **k: "YAHOO")
    monkeypatch.setattr(cn, "call", lambda *a, **k: pytest.fail("AkShare called for a US symbol"))
    assert router.route_to_vendor("get_news", "NVDA", "2026-10-07", TODAY) == "YAHOO"


@pytest.mark.unit
def test_without_akshare_installed_the_chain_moves_on(monkeypatch):
    def missing():
        raise cn.AkShareNotInstalledError("akshare is not installed")

    monkeypatch.setattr(cn, "_ak", missing)
    cn._CACHE.clear()
    monkeypatch.setattr(router, "get_config", lambda: {"data_vendors": {"news_data": "akshare,yfinance"}, "tool_vendors": {}})
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance", lambda *a, **k: "YAHOO")
    assert router.route_to_vendor("get_news", "600519.SS", "2026-10-07", TODAY) == "YAHOO"
    assert issubclass(cn.AkShareNotInstalledError, VendorNotConfiguredError)


@pytest.mark.unit
def test_news_is_kept_to_the_window(fake):
    out = cn.get_news("600519.SS", "2026-10-12", "2026-10-12")
    assert "茅台重返股王" in out and "三季报前瞻" not in out and "旧闻" not in out


@pytest.mark.unit
def test_a_window_older_than_the_feed_is_not_reported_as_no_news(fake):
    with pytest.raises(VendorUnavailableError):
        cn.get_news("600519.SS", "2026-09-01", "2026-09-05")


@pytest.mark.unit
def test_a_share_statements_use_only_periods_announced_by_the_date(fake):
    out = cn.get_income_statement("600519.SS", "quarterly", "2026-05-01")
    assert "2026-03-31" in out and "2025-12-31" in out
    assert "2026-06-30" not in out                       # announced 2026-08-07
    annual = cn.get_income_statement("600519.SS", "annual", TODAY)
    assert "2025-12-31" in annual and "2026-06-30" not in annual
    assert "922.78" in cn.get_income_statement("600519.SS", "quarterly", TODAY)   # 亿元


@pytest.mark.unit
def test_hk_statements_wait_out_the_filing_window_in_a_past_run(fake):
    past = cn.get_income_statement("0700.HK", "quarterly", "2026-08-01")
    assert "2025-12-31" in past and "2026-06-30" not in past
    current = cn.get_income_statement("0700.HK", "quarterly", TODAY)
    assert "2026-06-30" in current and "3,964.31" in current


@pytest.mark.unit
def test_a_share_fundamentals_read_valuation_and_financials_as_of_the_date(fake):
    out = cn.get_fundamentals("600519.SS", "2026-07-01")
    assert "2026-06-30" in out and "17,500.00" in out    # valuation row on or before the date (亿元)
    assert "19.57" not in out                            # later valuation withheld
    assert "Company profile" not in out                  # live profile withheld from a past run
    assert "-1.95%" not in out                           # H1 not announced until August
    now = cn.get_fundamentals("600519.SS", TODAY)
    assert "19.57" in now and "贵州茅台" in now and "-1.95%" in now


@pytest.mark.unit
def test_hk_fundamentals(fake):
    out = cn.get_fundamentals("0700.HK", TODAY)
    assert "腾讯控股" in out and "14.49" in out and "21.50" in out


@pytest.mark.unit
def test_china_context_respects_publication_dates(fake):
    out = cn.get_china_market_context("600519.SS", "2026-10-12", 3)
    assert "CPI 2026-08" in out and "CPI 2026-09" not in out     # Sept CPI out mid-October
    assert "PMI 2026-09" in out                                   # published 30 Sept
    assert "LPR 2026-09-22" in out
    assert "Main-force net" in out
    assert "Stock Connect" not in out                             # live-only, past run
    today = cn.get_china_market_context("600519.SS", TODAY, 1)
    assert "医保局" in today and "宝马" in today and "Stock Connect" in today


@pytest.mark.unit
def test_china_context_does_not_apply_elsewhere(fake):
    assert "does not apply" in cn.get_china_market_context("NVDA", TODAY)


@pytest.mark.unit
def test_money_flow_only_for_a_shares(fake):
    assert cn.fund_flow_lines("0700.HK", "2026-10-01", TODAY) == []
    lines = "\n".join(cn.fund_flow_lines("600519.SS", "2026-10-10", TODAY))
    assert "2026-10-09" not in lines and "3.40" in lines


@pytest.mark.unit
def test_cn_hosts_bypass_the_proxy():
    import os
    assert "eastmoney.com" in os.environ.get("NO_PROXY", "")


@pytest.mark.unit
def test_calls_are_cached_and_failures_become_unavailable(monkeypatch):
    cn._CACHE.clear()
    hits = []

    class _Fake:
        @staticmethod
        def ok(**k):
            hits.append(1)
            return pd.DataFrame({"a": [1]})

        @staticmethod
        def broken(**k):
            raise ConnectionError("reset")

    monkeypatch.setattr(cn, "_ak", lambda: _Fake)
    monkeypatch.setattr(cn.time, "sleep", lambda s: None)
    cn.call("ok", x=1)
    cn.call("ok", x=1)
    assert len(hits) == 1
    with pytest.raises(VendorUnavailableError):
        cn.call("broken")


@pytest.mark.unit
def test_a_comparative_period_counts_from_its_statutory_deadline_not_sinas_later_date(fake, monkeypatch):
    frames = _frames()
    sina = frames["stock_financial_report_sina"]
    # Sina dates the 2025-06-30 row by the 2026 interim that restated it.
    sina.loc[len(sina)] = ["20250630", 9.1e10, 4.5e10, 36.18, "2026-08-15"]
    monkeypatch.setattr(cn, "call", lambda name, **k: frames[name].copy())
    out = cn.get_income_statement("600519.SS", "quarterly", "2026-06-01")
    assert "2025-06-30" in out                     # public by 2025-08-31 at the latest
    assert "2025-08-31" in out and "2026-06-30" not in out


@pytest.mark.unit
def test_sentiment_says_when_a_share_money_flow_is_unavailable(monkeypatch):
    from tradingagents.agents.analysts import sentiment_analyst as sa

    def down(*a, **k):
        raise VendorUnavailableError("push2his reset")

    monkeypatch.setattr(cn, "fund_flow_lines", down)
    assert "unavailable" in sa._a_share_money_flow("600519.SS", "2026-10-07", TODAY)
    assert sa._a_share_money_flow("0700.HK", "2026-10-07", TODAY) == ""
    assert sa._a_share_money_flow("NVDA", "2026-10-07", TODAY) == ""


@pytest.mark.unit
def test_a_request_that_hangs_is_abandoned_not_waited_on(monkeypatch):
    import threading as th
    cn._CACHE.clear()
    release = th.Event()

    class _Fake:
        @staticmethod
        def stuck(**k):
            release.wait(5)
            return pd.DataFrame()

    monkeypatch.setattr(cn, "_ak", lambda: _Fake)
    monkeypatch.setattr(cn, "CALL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cn.time, "sleep", lambda s: None)
    started = cn.time.time()
    with pytest.raises(VendorUnavailableError, match="AkShareTimeoutError"):
        cn.call("stuck")
    assert cn.time.time() - started < 2
    release.set()


@pytest.mark.unit
def test_daily_bars_scale_east_money_lots_and_skip_an_open_session(monkeypatch):
    from datetime import datetime

    seen = []

    def fake_call(func, **kw):
        seen.append(func)
        return pd.DataFrame({"日期": ["2026-10-14", "2026-10-15"], "开盘": [20.5, 21.0], "最高": [21.5, 21.6],
                             "最低": [20.1, 20.9], "收盘": [21.3, 21.4], "成交量": [60000, 1000]})

    monkeypatch.setattr(cn, "call", fake_call)
    noon = datetime(2026, 10, 15, 12, 0, tzinfo=cn._BEIJING)
    monkeypatch.setattr(cn, "_session_over",
                        lambda market, day, now=None, _f=cn._session_over: _f(market, day, noon))
    bars = cn.bars_from("East Money", "000960.SZ", pd.Timestamp("2026-10-14"), pd.Timestamp("2026-10-15"))
    assert seen == ["stock_zh_a_hist"]
    assert list(bars["Date"]) == [pd.Timestamp("2026-10-14")]      # 10-15 still trading at noon
    assert bars["Volume"].iloc[0] == 6_000_000                       # lots of 100 -> shares


@pytest.mark.unit
def test_daily_bars_try_sina_then_east_money(monkeypatch):
    seen = []

    def fake_call(func, **kw):
        seen.append(func)
        if func == "stock_zh_a_daily":
            assert kw["symbol"] == "sz000960"
            raise RuntimeError("Sina down")
        return pd.DataFrame({"日期": ["2026-10-14"], "开盘": [20.5], "最高": [21.5], "最低": [20.1],
                             "收盘": [21.3], "成交量": [60000]})

    monkeypatch.setattr(cn, "call", fake_call)
    bars, source = cn.daily_bars("000960.SZ", pd.Timestamp("2026-10-14"), pd.Timestamp("2026-10-14"))
    assert seen == ["stock_zh_a_daily", "stock_zh_a_hist"]
    assert source == "East Money" and bars["Close"].iloc[0] == 21.3
