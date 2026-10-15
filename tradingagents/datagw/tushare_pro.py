"""Tushare Pro (https://tushare.pro): the gateway's first source for A-share and HK data.

Needs ``TUSHARE_TOKEN`` in .env and ``pip install tushare``. Points decide which
endpoints answer; at 2,000 points everything used here is open (check with
``python scripts/check_tushare.py``). Calls are spaced to stay under the
per-minute limit (``TUSHARE_CALLS_PER_MINUTE``, default 180).

Units are converted to the ones the rest of the code uses: volume in shares
(Tushare's A-share ``vol`` is lots of 100), amounts and market caps in yuan
(Tushare: thousands of yuan and 万元).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, timedelta

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.datagw.core import NotConfigured

TUSHARE_HOSTS = ("tushare.pro", "waditu.com")
for _var in ("NO_PROXY", "no_proxy"):       # a Chinese service: reached directly, not via a proxy
    _have = [h.strip() for h in os.environ.get(_var, "").split(",") if h.strip()]
    os.environ[_var] = ",".join(_have + [h for h in TUSHARE_HOSTS if h not in _have])

class TushareRateLimited(RuntimeError):
    """Tushare refused the call: the per-minute or per-hour limit for this endpoint is used up."""


_lock = threading.Lock()
_api = None
_api_token = None
_last_call = 0.0


def configured() -> str | None:
    if not os.environ.get("TUSHARE_TOKEN", "").strip():
        return "TUSHARE_TOKEN not set"
    try:
        import tushare  # noqa: F401
    except ImportError:
        return "tushare not installed (pip install tushare)"
    return None


def _pro():
    global _api, _api_token
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise NotConfigured("TUSHARE_TOKEN not set")
    if _api is None or token != _api_token:
        import tushare as ts

        _api, _api_token = ts.pro_api(token), token
    return _api


def query(api_name: str, **kwargs) -> pd.DataFrame:
    """One Tushare call, spaced to respect the per-minute limit; no permission -> NotConfigured."""
    global _last_call
    per_minute = float(os.environ.get("TUSHARE_CALLS_PER_MINUTE") or 180)
    with _lock:
        wait = _last_call + 60.0 / per_minute - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    try:
        df = _pro().query(api_name, **kwargs)
    except Exception as exc:
        text = str(exc)
        if "没有接口" in text or "权限" in text:
            raise NotConfigured(f"no permission for {api_name}") from exc
        if "频率超限" in text or "每分钟" in text:
            raise TushareRateLimited(f"{api_name}: call limit reached ({' '.join(text.split())[:80]})") from exc
        raise
    return df if df is not None else pd.DataFrame()


# --- symbols -----------------------------------------------------------------------

def ts_code(ticker: str) -> tuple[str, str]:
    """(market, Tushare code): 600519.SS -> ("sh", "600519.SH"), 6616.HK -> ("hk", "06616.HK")."""
    from tradingagents.dataflows.vendors.akshare_cn import market_of

    found = market_of(ticker)
    if found is None:
        raise NoMarketDataError(ticker, ticker, "not an A-share or Hong Kong listing")
    market, code = found
    return market, f"{code}.{ {'sh': 'SH', 'sz': 'SZ', 'hk': 'HK'}[market] }"


def open_days(start, end, exchange: str = "SSE") -> list[pd.Timestamp]:
    cal = query("trade_cal", exchange=exchange, start_date=f"{pd.Timestamp(start):%Y%m%d}",
                end_date=f"{pd.Timestamp(end):%Y%m%d}", is_open="1")
    if cal.empty:
        return []
    return sorted(pd.to_datetime(cal["cal_date"]).dt.normalize().tolist())


# --- capabilities --------------------------------------------------------------------

def daily_bars(ticker: str, start, end) -> pd.DataFrame:
    """Unadjusted bars of closed sessions in [start, end]: Date/Open/High/Low/Close/Volume (shares)."""
    from tradingagents.dataflows.vendors import akshare_cn

    market, code = ts_code(ticker)
    if market == "hk":
        # hk_daily is limited to one call an hour below 5,000 points: HK bars come from Sina.
        raise NoMarketDataError(ticker, ticker, "Tushare bars are used for A-shares only")
    s, e = f"{pd.Timestamp(start):%Y%m%d}", f"{pd.Timestamp(end):%Y%m%d}"
    df = query("daily", ts_code=code, start_date=s, end_date=e)
    if df.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "Date": pd.to_datetime(df["trade_date"]).dt.normalize(),
        "Open": pd.to_numeric(df["open"], errors="coerce"),
        "High": pd.to_numeric(df["high"], errors="coerce"),
        "Low": pd.to_numeric(df["low"], errors="coerce"),
        "Close": pd.to_numeric(df["close"], errors="coerce"),
        "Volume": pd.to_numeric(df["vol"], errors="coerce") * 100.0,      # lots of 100 -> shares
    }).dropna(subset=["Close"]).sort_values("Date")
    out = out[[akshare_cn._session_over(market, d) for d in out["Date"]]]
    return out.reset_index(drop=True)


def _adj_close(day: pd.Timestamp) -> pd.Series:
    """Every A-share's close times its adjustment factor on ``day`` (comparable across dates)."""
    d = f"{day:%Y%m%d}"
    px = query("daily", trade_date=d, fields="ts_code,close")
    adj = query("adj_factor", trade_date=d, fields="ts_code,adj_factor")
    if px.empty or adj.empty:
        return pd.Series(dtype=float)
    merged = px[["ts_code", "close"]].merge(adj[["ts_code", "adj_factor"]], on="ts_code")
    return pd.Series(pd.to_numeric(merged["close"], errors="coerce").values
                     * pd.to_numeric(merged["adj_factor"], errors="coerce").values,
                     index=merged["ts_code"].values)


def cn_spot(day: str | None = None) -> pd.DataFrame:
    """Every A-share at the latest close Tushare has published, in East Money's column names.

    ``frame.attrs["as_of"]`` is that trading day. 60-day and year-to-date changes
    are computed from adjusted closes. Tushare publishes a day's table after the
    close (usually by 17:00), so during a session the previous close is served.
    """
    today = pd.Timestamp(day or date.today()).normalize()
    # back to last December (year-to-date) and at least 60 sessions (60-day change)
    days = open_days(min(today - timedelta(days=150), pd.Timestamp(today.year - 1, 12, 1)), today)
    basic, as_of = pd.DataFrame(), None
    for d in reversed(days[-3:]):
        basic = query("daily_basic", trade_date=f"{d:%Y%m%d}",
                      fields="ts_code,close,turnover_rate,volume_ratio,pe_ttm,pb,dv_ttm,total_mv,circ_mv")
        if not basic.empty:
            as_of = d
            break
    if as_of is None:
        return pd.DataFrame()
    bars = query("daily", trade_date=f"{as_of:%Y%m%d}", fields="ts_code,pct_chg,amount")
    bars = bars[["ts_code", "pct_chg", "amount"]] if not bars.empty else pd.DataFrame(
        columns=["ts_code", "pct_chg", "amount"])
    names = query("stock_basic", exchange="", list_status="L", fields="ts_code,name,industry")
    names = names[["ts_code", "name", "industry"]] if not names.empty else pd.DataFrame(
        columns=["ts_code", "name", "industry"])
    df = basic.merge(bars, on="ts_code", how="left").merge(names, on="ts_code", how="left")

    past = [d for d in days if d <= as_of]
    now_adj = _adj_close(as_of)

    def change_since(ref: pd.Timestamp | None) -> pd.Series:
        if ref is None:
            return pd.Series(float("nan"), index=df.index)
        then = _adj_close(ref)
        return (df["ts_code"].map(now_adj) / df["ts_code"].map(then) - 1) * 100

    ref_60 = past[-61] if len(past) > 60 else None
    year_end = [d for d in past if d.year < as_of.year]
    ref_ytd = year_end[-1] if year_end else None

    out = pd.DataFrame({
        "代码": df["ts_code"].str[:6], "名称": df["name"],
        "最新价": pd.to_numeric(df["close"], errors="coerce"),
        "涨跌幅": pd.to_numeric(df["pct_chg"], errors="coerce"),
        "换手率": pd.to_numeric(df["turnover_rate"], errors="coerce"),
        "量比": pd.to_numeric(df["volume_ratio"], errors="coerce"),
        "成交额": pd.to_numeric(df["amount"], errors="coerce") * 1e3,       # 千元 -> 元
        "市盈率-动态": pd.to_numeric(df["pe_ttm"], errors="coerce"),
        "市净率": pd.to_numeric(df["pb"], errors="coerce"),
        "总市值": pd.to_numeric(df["total_mv"], errors="coerce") * 1e4,     # 万元 -> 元
        "流通市值": pd.to_numeric(df["circ_mv"], errors="coerce") * 1e4,
        "60日涨跌幅": change_since(ref_60).values,
        "年初至今涨跌幅": change_since(ref_ytd).values,
        "行业": df["industry"],
        "股息率": pd.to_numeric(df["dv_ttm"], errors="coerce") if "dv_ttm" in df.columns else float("nan"),
    })
    out.attrs["as_of"] = f"{as_of:%Y-%m-%d}"
    return out


def lhb(ticker: str, start, end) -> pd.DataFrame:
    """The listing's 龙虎榜 rows between start and end (one call per trading day)."""
    market, code = ts_code(ticker)
    if market == "hk":
        raise NoMarketDataError(ticker, ticker, "龙虎榜 is A-share only")
    frames = []
    for d in open_days(start, end):
        df = query("top_list", trade_date=f"{d:%Y%m%d}", ts_code=code)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["date", "reason", "chg_pct", "net_buy", "net_pct", "turnover"])
    df = pd.concat(frames, ignore_index=True)
    return pd.DataFrame({
        "date": pd.to_datetime(df["trade_date"]), "reason": df["reason"],
        "chg_pct": pd.to_numeric(df["pct_change"], errors="coerce"),
        "net_buy": pd.to_numeric(df["net_amount"], errors="coerce"),         # 元
        "net_pct": pd.to_numeric(df["net_rate"], errors="coerce"),
        "turnover": pd.to_numeric(df["turnover_rate"], errors="coerce"),
    })


def margin(ticker: str, day) -> dict | None:
    """Margin financing for the listing on ``day``; None when it is not a margin underlying."""
    market, code = ts_code(ticker)
    if market == "hk":
        raise NoMarketDataError(ticker, ticker, "margin data is A-share only")
    day = pd.Timestamp(day)
    df = query("margin_detail", trade_date=f"{day:%Y%m%d}", ts_code=code)
    if df.empty:
        return None
    row = df.iloc[0]
    num = lambda v: None if pd.isna(v) else float(v)       # noqa: E731
    return {"date": day, "balance": num(row.get("rzye")), "buy": num(row.get("rzmre")),
            "short": num(row.get("rqyl"))}
