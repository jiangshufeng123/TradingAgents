"""Mainland China (A-share) and Hong Kong data from AkShare.

AkShare wraps the public data pages of East Money, Sina, Tonghuashun, Baidu and
Cailianpress. This vendor serves ``.SS`` / ``.SZ`` (A-shares) and ``.HK``
symbols; for any other symbol it raises ``NotThisMarketError`` (a quiet "no
data" the router skips past), so a chain such as ``akshare,yfinance`` serves
every market without per-market configuration.

Point-in-time rules, so a past-dated run reads only what was public then:

* A-share statements carry their announcement date (Sina ``公告日期``); a
  period counts only once announced on or before the analysis date.
* Hong Kong statements carry no announcement date; a past-dated run takes a
  period only after a filing lag (90 days for interims, 120 for annuals).
* Valuation comes from daily history (A-shares: East Money; HK: Baidu), read
  as of the analysis date. Live-only figures are served only to a current run.
* News and fast-news feeds hold recent items only; a window they never covered
  says so instead of reporting an absence of news.

Chinese data sites are reached directly, not through an overseas proxy: their
hosts are added to ``NO_PROXY`` when this module loads.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

_BEIJING = ZoneInfo("Asia/Shanghai")

CN_HOSTS = (
    "eastmoney.com", "sina.com.cn", "sinajs.cn", "10jqka.com.cn", "cls.cn", "baidu.com",
    "cninfo.com.cn", "jin10.com", "stats.gov.cn", "hkex.com.hk", "legulegu.com", "xueqiu.com",
)


def _bypass_proxy_for_cn_hosts() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        existing = [h.strip() for h in os.environ.get(var, "").split(",") if h.strip()]
        os.environ[var] = ",".join(existing + [h for h in CN_HOSTS if h not in existing])


_bypass_proxy_for_cn_hosts()


class NotThisMarketError(NoMarketDataError):
    """The symbol is not an A-share or Hong Kong listing; the next vendor serves it."""


class AkShareNotInstalledError(VendorNotConfiguredError):
    """AkShare is not installed (``pip install akshare``)."""


# --- symbols -------------------------------------------------------------------

def market_of(ticker: str) -> tuple[str, str] | None:
    """``("sh"|"sz"|"hk", code)`` for an A-share or HK symbol, else None."""
    canonical = normalize_symbol(ticker or "")
    if not isinstance(canonical, str) or "." not in canonical:
        return None
    code, suffix = canonical.rsplit(".", 1)
    if suffix == "SS" and code.isdigit() and len(code) == 6:
        return "sh", code
    if suffix == "SZ" and code.isdigit() and len(code) == 6:
        return "sz", code
    if suffix == "HK" and code.isdigit():
        return "hk", code.zfill(5)
    return None


def is_cn_or_hk(ticker: str) -> bool:
    return market_of(ticker) is not None


def _require(ticker: str) -> tuple[str, str]:
    found = market_of(ticker)
    if found is None:
        raise NotThisMarketError(ticker, ticker, "not an A-share or Hong Kong listing")
    return found


# --- calling AkShare -----------------------------------------------------------

def _ak():
    try:
        import akshare
    except ImportError as exc:
        raise AkShareNotInstalledError(
            "akshare is not installed; run: pip install akshare"
        ) from exc
    return akshare


_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
CACHE_SECONDS = 600
RETRIES = 3
# AkShare makes its requests without a timeout, so a stalled connection to a
# Chinese site can hang a run for hours. Each attempt is given this long.
CALL_TIMEOUT_SECONDS = 30


class AkShareTimeoutError(TimeoutError):
    """An AkShare request did not answer within CALL_TIMEOUT_SECONDS."""


def _with_timeout(func, kwargs, seconds: float):
    """``func(**kwargs)``, or AkShareTimeoutError after ``seconds``.

    The request runs on a daemon thread: a request that never returns is left
    behind rather than holding the run (or the interpreter's exit).
    """
    box: dict = {}

    def run():
        try:
            box["value"] = func(**kwargs)
        except BaseException as exc:   # handed back to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name=f"akshare-{getattr(func, '__name__', 'call')}")
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise AkShareTimeoutError(f"no answer within {seconds:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def call(func_name: str, **kwargs) -> pd.DataFrame:
    """``akshare.<func_name>(**kwargs)``, retried, and cached for a few minutes.

    Several analysts ask for the same table in one run; the cache keeps that to
    one request. Network failures are retried with a short backoff, then
    reported as the vendor being unavailable so the next vendor is tried.
    """
    key = (func_name, tuple(sorted(kwargs.items())))
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    func = getattr(_ak(), func_name)
    last = None
    for attempt in range(RETRIES):
        try:
            df = _with_timeout(func, kwargs, CALL_TIMEOUT_SECONDS)
            break
        except Exception as exc:   # network, upstream format or throttling
            last = exc
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    else:
        raise VendorUnavailableError(f"AkShare {func_name} failed: {type(last).__name__}: {str(last)[:200]}")
    if df is None:
        df = pd.DataFrame()
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), df)
    return df


# --- formatting ----------------------------------------------------------------

def _num(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _yi(value) -> str:
    """An amount in 亿 (1e8) with two decimals, or "—"."""
    number = _num(value)
    return "—" if number is None else f"{number / 1e8:,.2f}"


def _fmt(value, digits: int = 2) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:,.{digits}f}"


def _date(value) -> pd.Timestamp | None:
    parsed = pd.to_datetime(str(value), errors="coerce") if value is not None else None
    return None if parsed is None or pd.isna(parsed) else parsed.normalize()


def _as_of(as_of_date: str | None) -> pd.Timestamp:
    return pd.Timestamp(as_of_date or get_current_date()).normalize()


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


# --- news ----------------------------------------------------------------------

def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Company news from East Money (A-shares and Hong Kong)."""
    _market, code = _require(ticker)
    df = call("stock_news_em", symbol=code)
    if df.empty or "发布时间" not in df.columns:
        raise VendorUnavailableError(f"East Money returned no news list for {code}")
    df = df.assign(_t=pd.to_datetime(df["发布时间"], errors="coerce")).dropna(subset=["_t"])
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date) + timedelta(days=1)
    window = df[(df["_t"] >= start) & (df["_t"] < end)].sort_values("_t", ascending=False)
    if window.empty:
        oldest = df["_t"].min()
        if pd.notna(oldest) and oldest > start:
            # The feed holds its latest items only and does not reach back to this window.
            raise VendorUnavailableError(
                f"East Money news for {code} starts {oldest:%Y-%m-%d}, after the "
                f"requested {start_date}..{end_date}; it holds recent items only"
            )
        return f"No East Money news for {ticker} between {start_date} and {end_date}."
    parts = [f"## {ticker} news (East Money), {start_date} to {end_date}\n"]
    for _, row in window.iterrows():
        content = str(row.get("新闻内容") or "").strip().replace("\n", " ")
        parts.append(f"### {row.get('新闻标题', '')}\n"
                     f"{row['_t']:%Y-%m-%d %H:%M} · {row.get('文章来源', '')}\n\n{content[:600]}\n")
    return "\n".join(parts)


# --- fundamentals --------------------------------------------------------------

def _a_announced_periods(prefix: str, code: str, as_of: pd.Timestamp) -> dict[str, pd.Timestamp]:
    """Report period (YYYYMMDD) -> announcement date, for periods public by ``as_of``."""
    income = call("stock_financial_report_sina", stock=f"{prefix}{code}", symbol="利润表")
    periods = {}
    for _, row in income.iterrows():
        period = str(row.get("报告日", "")).split(".")[0]
        announced = _first_public(period, row.get("公告日期"))
        if announced is not None and announced <= as_of:
            periods[period] = announced
    return periods


def _first_public(period: str, sina_date) -> pd.Timestamp | None:
    """When a period's figures were first public.

    Sina's ``公告日期`` is the filing its row was last taken from, which for a
    comparative period is often the next year's report (2025-06-30 dated
    2026-08-15). The period itself was public by its statutory deadline at the
    latest, so the earlier of the two is used.
    """
    candidates = [d for d in (_date(sina_date), _filing_deadline(period)) if d is not None]
    return min(candidates) if candidates else None


def _filing_deadline(period: str) -> pd.Timestamp | None:
    """When a period's report is due at the latest (CSRC deadlines), for undated rows."""
    end = _date(period)
    if end is None:
        return None
    if end.month == 12:
        return pd.Timestamp(end.year + 1, 4, 30)
    if end.month == 6:
        return pd.Timestamp(end.year, 8, 31)
    return end + timedelta(days=31)   # Q1 by 30 Apr, Q3 by 31 Oct


def get_fundamentals(ticker: str, as_of_date: str | None = None) -> str:
    market, code = _require(ticker)
    as_of = _as_of(as_of_date)
    if market == "hk":
        return _hk_fundamentals(ticker, code, as_of, as_of_date)
    return _a_fundamentals(ticker, market, code, as_of, as_of_date)


def _valuation_section(df: pd.DataFrame, date_col: str, as_of: pd.Timestamp, fields: list[str]) -> list[str]:
    if df.empty or date_col not in df.columns:
        return ["Valuation history unavailable."]
    df = df.assign(_d=pd.to_datetime(df[date_col], errors="coerce")).dropna(subset=["_d"])
    df = df[df["_d"] <= as_of].sort_values("_d")
    if df.empty:
        return [f"No valuation data on or before {as_of:%Y-%m-%d}."]
    last = df.iloc[-1]
    year = df[df["_d"] > last["_d"] - timedelta(days=365)]
    rows = []
    for field in fields:
        if field not in df.columns:
            continue
        series = pd.to_numeric(year[field], errors="coerce").dropna()
        value = _num(last[field])
        if field in ("总市值", "流通市值"):
            rows.append([field + "(亿元)", _yi(value), _yi(series.min()) if len(series) else "—",
                         _yi(series.max()) if len(series) else "—"])
        else:
            rows.append([field, _fmt(value), _fmt(series.min()) if len(series) else "—",
                         _fmt(series.max()) if len(series) else "—"])
    return [f"As of {last['_d']:%Y-%m-%d} (1-year low / high alongside):",
            _table(["Metric", "Value", "1y low", "1y high"], rows)]


def _a_fundamentals(ticker, prefix, code, as_of, as_of_date) -> str:
    lines = [f"# Fundamentals for {ticker} (A-share, AkShare)", f"# Point-in-time as of: {as_of:%Y-%m-%d}", ""]
    if not is_historical(as_of_date):
        try:
            profile = call("stock_profile_cninfo", symbol=code)
            if not profile.empty:
                p = profile.iloc[0]
                lines += ["## Company profile (current)",
                          f"- Name: {p.get('公司名称', '')} ({p.get('A股简称', '')}); market {p.get('所属市场', '')}",
                          f"- Industry: {p.get('所属行业', '')}; listed {p.get('上市日期', '')}",
                          f"- Index membership: {p.get('入选指数', '')}",
                          f"- Main business: {str(p.get('主营业务', ''))[:300]}", ""]
        except VendorUnavailableError as exc:
            lines += [f"Company profile unavailable: {exc}", ""]
    lines.append("## Valuation (East Money daily history; 元, 市值 in 亿元)")
    lines += _valuation_section(call("stock_value_em", symbol=code), "数据日期", as_of,
                                ["当日收盘价", "总市值", "流通市值", "PE(TTM)", "PE(静)", "市净率", "PEG值", "市现率", "市销率"])
    lines.append("")

    announced = _a_announced_periods(prefix, code, as_of)
    abstract = call("stock_financial_abstract_ths", symbol=code, indicator="按报告期")
    if not abstract.empty and "报告期" in abstract.columns:
        abstract = abstract.assign(_p=abstract["报告期"].astype(str).str.replace("-", ""))
        abstract = abstract[abstract["_p"].isin(announced)].sort_values("_p").tail(8)
        cols = [c for c in ["营业总收入", "营业总收入同比增长率", "净利润", "净利润同比增长率", "扣非净利润",
                            "基本每股收益", "每股净资产", "每股经营现金流", "销售毛利率", "销售净利率",
                            "净资产收益率", "资产负债率", "流动比率"] if c in abstract.columns]
        if not abstract.empty:
            lines.append("## Key financials by report period (Tonghuashun; announced by the as-of date; "
                         "A-share figures are year-to-date cumulative)")
            header = ["Item"] + [f"{p[:4]}-{p[4:6]}-{p[6:]}" for p in abstract["_p"]]
            rows = [[c] + [str(v).replace("False", "—") for v in abstract[c]] for c in cols]
            lines.append(_table(header, rows))
            last_period = abstract["_p"].iloc[-1]
            lines.append(f"\nLatest period {last_period[:4]}-{last_period[4:6]}-{last_period[6:]} "
                         f"announced {announced[last_period]:%Y-%m-%d}.")
    return "\n".join(lines)


def _hk_fundamentals(ticker, code, as_of, as_of_date) -> str:
    lines = [f"# Fundamentals for {ticker} (Hong Kong, AkShare)", f"# Point-in-time as of: {as_of:%Y-%m-%d}", ""]
    if not is_historical(as_of_date):
        try:
            profile = call("stock_hk_company_profile_em", symbol=code)
            if not profile.empty:
                p = profile.iloc[0]
                lines += ["## Company profile (current)",
                          f"- Name: {p.get('公司名称', '')} / {p.get('英文名称', '')}",
                          f"- Industry: {p.get('所属行业', '')}; fiscal year ends {p.get('年结日', '')}; "
                          f"employees {p.get('员工人数', '')}",
                          f"- About: {str(p.get('公司介绍', ''))[:300]}", ""]
            ind = call("stock_hk_financial_indicator_em", symbol=code)
            if not ind.empty:
                i = ind.iloc[0]
                rows = [[k, _yi(i.get(k)) if "市值" in k or k in ("营业总收入", "净利润") else _fmt(i.get(k))]
                        for k in ["总市值(港元)", "市盈率", "市净率", "股息率TTM(%)", "派息比率(%)",
                                  "基本每股收益(元)", "每股净资产(元)", "营业总收入", "净利润",
                                  "销售净利率(%)", "股东权益回报率(%)", "总资产回报率(%)"] if k in ind.columns]
                lines += ["## Latest indicators (East Money, current; 市值/收入/利润 in 亿)",
                          _table(["Metric", "Value"], rows), ""]
        except VendorUnavailableError as exc:
            lines += [f"Current profile/indicators unavailable: {exc}", ""]
    lines.append("## Valuation history (Baidu, as of the analysis date)")
    rows = []
    for indicator in ("市盈率(TTM)", "市净率", "总市值"):
        try:
            df = call("stock_hk_valuation_baidu", symbol=code, indicator=indicator, period="近三年")
        except VendorUnavailableError:
            continue
        if df.empty:
            continue
        df = df.assign(_d=pd.to_datetime(df["date"], errors="coerce")).dropna(subset=["_d"])
        df = df[df["_d"] <= as_of].sort_values("_d")
        if df.empty:
            continue
        year = pd.to_numeric(df[df["_d"] > df["_d"].iloc[-1] - timedelta(days=365)]["value"], errors="coerce")
        unit = "(亿港元)" if indicator == "总市值" else ""
        rows.append([indicator + unit, f"{df['_d'].iloc[-1]:%Y-%m-%d}", _fmt(df["value"].iloc[-1]),
                     _fmt(year.min()), _fmt(year.max())])
    lines.append(_table(["Metric", "Date", "Value", "1y low", "1y high"], rows) if rows
                 else "Valuation history unavailable.")
    return "\n".join(lines)


# --- statements ----------------------------------------------------------------

_A_STATEMENT = {"balance": "资产负债表", "income": "利润表", "cashflow": "现金流量表"}
_A_KEY_ITEMS = {
    "income": ["营业总收入", "营业收入", "营业成本", "销售费用", "管理费用", "研发费用", "财务费用", "营业利润",
               "利润总额", "所得税费用", "净利润", "归属于母公司所有者的净利润", "基本每股收益", "稀释每股收益"],
    "balance": ["货币资金", "交易性金融资产", "应收票据及应收账款", "存货", "流动资产合计", "固定资产净额",
                "无形资产", "商誉", "非流动资产合计", "资产总计", "短期借款", "应付票据及应付账款", "合同负债",
                "流动负债合计", "长期借款", "应付债券", "负债合计", "未分配利润",
                "归属于母公司股东权益合计", "所有者权益(或股东权益)合计"],
    "cashflow": ["销售商品、提供劳务收到的现金", "经营活动产生的现金流量净额", "购建固定资产、无形资产和其他长期资产所支付的现金",
                 "投资活动产生的现金流量净额", "分配股利、利润或偿付利息所支付的现金",
                 "筹资活动产生的现金流量净额", "现金及现金等价物净增加额", "期末现金及现金等价物余额"],
}
_HK_STATEMENT = {"balance": "资产负债表", "income": "利润表", "cashflow": "现金流量表"}


def _statement(kind: str, ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    market, code = _require(ticker)
    as_of = _as_of(as_of_date)
    annual = str(freq).lower().startswith("a")
    if market == "hk":
        return _hk_statement(kind, ticker, code, annual, as_of, as_of_date)
    return _a_statement(kind, ticker, market, code, annual, as_of)


def _a_statement(kind, ticker, prefix, code, annual, as_of) -> str:
    df = call("stock_financial_report_sina", stock=f"{prefix}{code}", symbol=_A_STATEMENT[kind])
    if df.empty or "报告日" not in df.columns:
        raise NoMarketDataError(ticker, f"{prefix}{code}", f"no {_A_STATEMENT[kind]} rows")
    df = df.assign(_p=df["报告日"].astype(str).str.split(".").str[0])
    sina_dates = df["公告日期"] if "公告日期" in df.columns else pd.Series([None] * len(df), index=df.index)
    df = df.assign(_a=[_first_public(p, d) for p, d in zip(df["_p"], sina_dates)])
    df = df[df["_a"].notna() & (df["_a"] <= as_of)]
    if annual:
        df = df[df["_p"].str.endswith("1231")]
    df = df.drop_duplicates("_p").sort_values("_p", ascending=False).head(5 if annual else 8)
    if df.empty:
        raise NoMarketDataError(ticker, f"{prefix}{code}", f"no {_A_STATEMENT[kind]} announced by {as_of:%Y-%m-%d}")
    items = [c for c in _A_KEY_ITEMS[kind] if c in df.columns]
    header = ["Item (亿元; per-share in 元)"] + [f"{p[:4]}-{p[4:6]}-{p[6:]}" for p in df["_p"]]
    rows = []
    for item in items:
        values = df[item].tolist()
        if all(_num(v) is None for v in values):
            continue
        rows.append([item] + [_fmt(v) if "每股" in item else _yi(v) for v in values])
    announced_row = ["公告日期"] + [f"{a:%Y-%m-%d}" for a in df["_a"]]
    return "\n".join([
        f"# {_A_STATEMENT[kind]} for {ticker} ({'annual' if annual else 'by report period'}, Sina via AkShare)",
        f"# Point-in-time as of: {as_of:%Y-%m-%d}; only periods announced by then",
        "# A-share interim figures in the income and cash-flow statements are year-to-date cumulative.",
        "# 公告日期: first public date (Sina's filing date, capped at the statutory deadline); "
        "figures are the latest published version and may include later restatements.",
        "", _table(header, rows + [announced_row]),
    ])


def _hk_statement(kind, ticker, code, annual, as_of, as_of_date) -> str:
    df = call("stock_financial_hk_report_em", stock=code, symbol=_HK_STATEMENT[kind],
              indicator="年度" if annual else "报告期")
    if df.empty or "REPORT_DATE" not in df.columns:
        raise NoMarketDataError(ticker, code, f"no {_HK_STATEMENT[kind]} rows")
    df = df.assign(_d=pd.to_datetime(df["REPORT_DATE"], errors="coerce")).dropna(subset=["_d"])
    if is_historical(as_of_date):
        # No announcement date: wait out the filing window before using a period.
        lag = df["_d"].map(lambda d: timedelta(days=120 if d.month == 12 else 90))
        df = df[df["_d"] + lag <= as_of]
    periods = sorted(df["_d"].unique(), reverse=True)[: 5 if annual else 8]
    if not periods:
        raise NoMarketDataError(ticker, code, f"no {_HK_STATEMENT[kind]} available by {as_of:%Y-%m-%d}")
    df = df[df["_d"].isin(periods)]
    table = df.pivot_table(index="STD_ITEM_NAME", columns="_d", values="AMOUNT", aggfunc="first")
    order = df.drop_duplicates("STD_ITEM_NAME").set_index("STD_ITEM_NAME")
    if "STD_ITEM_CODE" in order.columns:
        table = table.loc[order.sort_values("STD_ITEM_CODE").index.intersection(table.index, sort=False)]
    table = table[sorted(table.columns, reverse=True)].head(40)
    header = ["Item (亿, reporting currency)"] + [f"{pd.Timestamp(c):%Y-%m-%d}" for c in table.columns]
    rows = [[name] + [_fmt(v) if "每股" in str(name) else _yi(v) for v in values]
            for name, values in zip(table.index, table.values)]
    note = ("# Past-dated run: a period is used once 90 days (interim) / 120 days (annual) have passed."
            if is_historical(as_of_date) else "")
    return "\n".join([
        f"# {_HK_STATEMENT[kind]} for {ticker} ({'annual' if annual else 'by report period'}, East Money via AkShare)",
        f"# As of: {as_of:%Y-%m-%d}", note, "", _table(header, rows),
    ])


get_balance_sheet = functools.partial(_statement, "balance")
get_income_statement = functools.partial(_statement, "income")
get_cashflow = functools.partial(_statement, "cashflow")


# --- China market context ------------------------------------------------------

def _month_of(label) -> pd.Timestamp | None:
    """'2026年09月份' -> 2026-09-01."""
    text = str(label)
    try:
        return pd.Timestamp(int(text[:4]), int(text[5:7]), 1)
    except (ValueError, IndexError):
        return None


def _macro_lines(as_of: pd.Timestamp) -> list[str]:
    rows = []
    try:   # NBS releases CPI around the 10th-15th of the next month.
        cpi = call("macro_china_cpi")
        cpi = cpi.assign(_m=cpi["月份"].map(_month_of)).dropna(subset=["_m"])
        cpi = cpi[cpi["_m"] + pd.DateOffset(months=1, days=14) <= as_of].sort_values("_m")
        for _, r in cpi.tail(3).iterrows():
            rows.append([f"CPI {r['_m']:%Y-%m}", f"同比 {_fmt(r.get('全国-同比增长'), 1)}%, 环比 {_fmt(r.get('全国-环比增长'), 1)}%"])
    except (VendorUnavailableError, KeyError) as exc:
        rows.append(["CPI", f"unavailable ({exc})"])
    try:   # Official PMI is published on the last day of its month.
        pmi = call("macro_china_pmi")
        pmi = pmi.assign(_m=pmi["月份"].map(_month_of)).dropna(subset=["_m"])
        pmi = pmi[pmi["_m"] + pd.offsets.MonthEnd(0) <= as_of].sort_values("_m")
        for _, r in pmi.tail(3).iterrows():
            rows.append([f"PMI {r['_m']:%Y-%m}", f"制造业 {_fmt(r.get('制造业-指数'), 1)}, 非制造业 {_fmt(r.get('非制造业-指数'), 1)}"])
    except (VendorUnavailableError, KeyError) as exc:
        rows.append(["PMI", f"unavailable ({exc})"])
    try:
        lpr = call("macro_china_lpr")
        lpr = lpr.assign(_d=pd.to_datetime(lpr["TRADE_DATE"], errors="coerce")).dropna(subset=["_d", "LPR1Y"])
        lpr = lpr[lpr["_d"] <= as_of].sort_values("_d")
        if not lpr.empty:
            r = lpr.iloc[-1]
            rows.append([f"LPR {r['_d']:%Y-%m-%d}", f"1年 {_fmt(r['LPR1Y'])}%, 5年 {_fmt(r.get('LPR5Y'))}%"])
    except (VendorUnavailableError, KeyError) as exc:
        rows.append(["LPR", f"unavailable ({exc})"])
    return ["## China macro (as published by the analysis date)", _table(["Series", "Latest"], rows)]


def _flash_news(as_of: pd.Timestamp, look_back_days: int) -> list[str]:
    start = as_of - timedelta(days=look_back_days)
    end = as_of + timedelta(days=1)
    items = []
    for func, title_col, body_col, time_cols, source in (
        ("stock_info_global_cls", "标题", "内容", ("发布日期", "发布时间"), "财联社"),
        ("stock_info_global_em", "标题", "摘要", ("发布时间",), "东方财富"),
    ):
        try:
            df = call(func, **({"symbol": "全部"} if func == "stock_info_global_cls" else {}))
        except VendorUnavailableError as exc:
            items.append((None, source, f"unavailable ({exc})", ""))
            continue
        if df.empty:
            continue
        stamp = df[list(time_cols)].astype(str).agg(" ".join, axis=1)
        df = df.assign(_t=pd.to_datetime(stamp, errors="coerce")).dropna(subset=["_t"])
        if df.empty:
            continue
        if df["_t"].min() > start and df[df["_t"] < end].empty:
            items.append((None, source, "feed holds recent items only; none reach this window", ""))
            continue
        for _, r in df[(df["_t"] >= start) & (df["_t"] < end)].iterrows():
            items.append((r["_t"], source, str(r.get(title_col, "")), str(r.get(body_col, ""))))
    dated = sorted((i for i in items if i[0] is not None), key=lambda i: i[0], reverse=True)[:25]
    notes = [f"- {src}: {msg}" for t, src, msg, _ in items if t is None]
    lines = [f"## China market fast news ({start:%Y-%m-%d} to {as_of:%Y-%m-%d}, newest first)"] + notes
    for t, src, title, body in dated:
        text = body if len(body) > len(title) else title
        lines.append(f"- {t:%m-%d %H:%M} [{src}] {text.strip()[:220]}")
    if not dated and not notes:
        lines.append("- none in this window")
    return lines


def fund_flow_lines(ticker: str, start_date: str, end_date: str) -> list[str]:
    """Daily main-force money flow for an A-share over the window; [] otherwise."""
    found = market_of(ticker)
    if found is None or found[0] == "hk":
        return []
    market, code = found
    df = call("stock_individual_fund_flow", stock=code, market=market)
    if df.empty or "日期" not in df.columns:
        return []
    df = df.assign(_d=pd.to_datetime(df["日期"], errors="coerce")).dropna(subset=["_d"])
    df = df[(df["_d"] >= pd.Timestamp(start_date)) & (df["_d"] <= pd.Timestamp(end_date))].sort_values("_d")
    if df.empty:
        return []
    rows = [[f"{r['_d']:%Y-%m-%d}", _fmt(r.get("收盘价")), _fmt(r.get("涨跌幅")),
             _fmt(_num(r.get("主力净流入-净额")) / 1e8 if _num(r.get("主力净流入-净额")) is not None else None),
             _fmt(r.get("主力净流入-净占比")), _fmt(_num(r.get("超大单净流入-净额")) / 1e8
                                              if _num(r.get("超大单净流入-净额")) is not None else None),
             _fmt(_num(r.get("小单净流入-净额")) / 1e8 if _num(r.get("小单净流入-净额")) is not None else None)]
            for _, r in df.iterrows()]
    total = pd.to_numeric(df["主力净流入-净额"], errors="coerce").sum() / 1e8
    return [f"## A-share money flow for {ticker} (East Money; 亿元; main force = large + extra-large orders)",
            _table(["Date", "Close", "Chg %", "Main net", "Main %", "Extra-large net", "Small net"], rows),
            f"Main-force net over the window: {total:,.2f} 亿元."]


def get_china_market_context(ticker: str, curr_date: str, look_back_days: int | None = None) -> str:
    """China macro, fast news and (A-shares) money flow, as of ``curr_date``."""
    if not is_cn_or_hk(ticker):
        return (f"{ticker} is not listed in mainland China or Hong Kong; the China market "
                f"context does not apply. Use the other news and macro tools.")
    _ak()
    as_of = _as_of(curr_date)
    days = int(look_back_days or 3)
    lines = [f"# China market context for {ticker}, as of {as_of:%Y-%m-%d}", ""]
    lines += _macro_lines(as_of) + [""]
    lines += _flash_news(as_of, days) + [""]
    try:
        flow = fund_flow_lines(ticker, f"{as_of - timedelta(days=14):%Y-%m-%d}", f"{as_of:%Y-%m-%d}")
        lines += flow + ([""] if flow else [])
    except VendorUnavailableError as exc:
        lines += [f"Money flow unavailable: {exc}", ""]
    if not is_historical(curr_date):
        try:
            hsgt = call("stock_hsgt_fund_flow_summary_em")
            if not hsgt.empty:
                rows = [[r.get("类型", ""), r.get("板块", ""), r.get("资金方向", ""), _fmt(r.get("成交净买额")),
                         _fmt(r.get("资金净流入")), r.get("相关指数", ""), _fmt(r.get("指数涨跌幅"))]
                        for _, r in hsgt.iterrows()]
                lines += [f"## Stock Connect flows today ({hsgt.iloc[0].get('交易日', '')}; 亿元; "
                          "northbound net-buy figures are no longer disclosed intraday and may read 0)",
                          _table(["Channel", "Board", "Direction", "Net buy", "Net inflow", "Index", "Index %"], rows)]
        except VendorUnavailableError as exc:
            lines.append(f"Stock Connect flows unavailable: {exc}")
    return "\n".join(lines)


# --- daily bars (to fill a close Yahoo has not published) ----------------------

# When each market's session has closed, Beijing time; a bar for today is only
# final after this.
_CLOSE_HHMM = {"sh": (15, 5), "sz": (15, 5), "hk": (16, 15)}


def _session_over(market: str, day: pd.Timestamp, now: datetime | None = None) -> bool:
    now = now or datetime.now(_BEIJING)
    today = pd.Timestamp(now.date())
    if day.normalize() < today:
        return True
    hh, mm = _CLOSE_HHMM[market]
    return day.normalize() == today and (now.hour, now.minute) >= (hh, mm)


def _bars(df: pd.DataFrame, cols: dict, volume_scale: float = 1.0) -> pd.DataFrame:
    if df is None or df.empty or not set(cols) <= set(df.columns):
        return pd.DataFrame()
    out = df[list(cols)].rename(columns=cols)
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce").dt.normalize()
    for c in ("Open", "High", "Low", "Close", "Volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["Volume"] = out["Volume"] * volume_scale
    return out.dropna(subset=["Date", "Close"])


def daily_bars(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, str]:
    """Unadjusted daily bars ``Date/Open/High/Low/Close/Volume`` (shares) in [start, end].

    Only sessions that have closed are returned. A-shares try East Money, then
    Sina; Hong Kong uses Sina. Returns (frame, source); an empty frame when no
    source answered.
    """
    market, code = _require(ticker)
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    en = {"日期": "Date", "开盘": "Open", "最高": "High", "最低": "Low", "收盘": "Close", "成交量": "Volume"}
    lower = {"date": "Date", "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    if market == "hk":
        attempts = [("Sina", "stock_hk_daily", {"symbol": code, "adjust": ""}, lower, 1.0)]
    else:
        attempts = [
            # East Money reports volume in lots of 100 shares.
            ("East Money", "stock_zh_a_hist",
             {"symbol": code, "period": "daily", "start_date": s, "end_date": e, "adjust": ""}, en, 100.0),
            ("Sina", "stock_zh_a_daily",
             {"symbol": f"{market}{code}", "start_date": s, "end_date": e, "adjust": ""}, lower, 1.0),
        ]
    for source, func, kwargs, cols, scale in attempts:
        try:
            bars = _bars(call(func, **kwargs), cols, scale)
        except Exception as exc:   # any source failing just means trying the next
            logger.debug("%s daily bars from %s failed: %s", ticker, source, exc)
            continue
        bars = bars[(bars["Date"] >= start.normalize()) & (bars["Date"] <= end.normalize())]
        bars = bars[[_session_over(market, d) for d in bars["Date"]]]
        if not bars.empty:
            return bars.reset_index(drop=True), source
    return pd.DataFrame(), ""
