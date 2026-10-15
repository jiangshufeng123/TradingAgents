"""Which Tushare Pro endpoints does this token reach? (No model is called.)

    pip install tushare
    python scripts/check_tushare.py

Reads TUSHARE_TOKEN from .env (never printed). Each line: OK with the row count,
NO PERMISSION (needs more points or a separate purchase), or the error. The
results decide what the data gateway takes from Tushare first.
"""
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

token = os.environ.get("TUSHARE_TOKEN", "").strip()
if not token:
    sys.exit("TUSHARE_TOKEN is not set in .env")

import tushare as ts  # noqa: E402

pro = ts.pro_api(token)
end = date.today()
start = end - timedelta(days=30)
s, e = f"{start:%Y%m%d}", f"{end:%Y%m%d}"
period = "20260630"

CHECKS = [
    ("股票列表 stock_basic", lambda: pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")),
    ("交易日历 trade_cal", lambda: pro.trade_cal(exchange="SSE", start_date=s, end_date=e)),
    ("日线 daily", lambda: pro.daily(ts_code="600519.SH", start_date=s, end_date=e)),
    ("复权因子 adj_factor", lambda: pro.adj_factor(ts_code="600519.SH", start_date=s, end_date=e)),
    ("每日指标 daily_basic (PE/PB/市值/换手)", lambda: pro.daily_basic(ts_code="600519.SH", start_date=s, end_date=e)),
    ("全市场每日指标 daily_basic(trade_date)", lambda: pro.daily_basic(trade_date=_last_trade_day())),
    ("涨跌停价 stk_limit", lambda: pro.stk_limit(ts_code="600519.SH", start_date=s, end_date=e)),
    ("停复牌 suspend_d", lambda: pro.suspend_d(trade_date=_last_trade_day())),
    ("利润表 income", lambda: pro.income(ts_code="600519.SH", period=period)),
    ("资产负债表 balancesheet", lambda: pro.balancesheet(ts_code="600519.SH", period=period)),
    ("现金流量表 cashflow", lambda: pro.cashflow(ts_code="600519.SH", period=period)),
    ("财务指标 fina_indicator", lambda: pro.fina_indicator(ts_code="600519.SH", period=period)),
    ("全市场财务指标 fina_indicator_vip", lambda: pro.fina_indicator_vip(period=period)),
    ("业绩预告 forecast", lambda: pro.forecast(ts_code="600519.SH", start_date="20260101", end_date=e)),
    ("个股资金流 moneyflow", lambda: pro.moneyflow(ts_code="600519.SH", start_date=s, end_date=e)),
    ("龙虎榜 top_list", lambda: pro.top_list(trade_date=_last_trade_day())),
    ("融资融券明细 margin_detail", lambda: pro.margin_detail(trade_date=_last_trade_day())),
    ("沪深港通资金 moneyflow_hsgt", lambda: pro.moneyflow_hsgt(start_date=s, end_date=e)),
    ("指数日线 index_daily (沪深300)", lambda: pro.index_daily(ts_code="000300.SH", start_date=s, end_date=e)),
    ("申万行业成分 index_member", lambda: pro.index_member(ts_code="850531.SI")),
    ("港股日线 hk_daily", lambda: pro.hk_daily(ts_code="00700.HK", start_date=s, end_date=e)),
    ("新闻快讯 news", lambda: pro.news(src="sina", start_date=f"{end:%Y-%m-%d} 09:00:00",
                                    end_date=f"{end:%Y-%m-%d} 10:00:00")),
]

_days = {}


def _last_trade_day() -> str:
    if "d" not in _days:
        cal = pro.trade_cal(exchange="SSE", start_date=s, end_date=e, is_open="1")
        days = sorted(cal["cal_date"])
        _days["d"] = days[-2] if len(days) > 1 else days[-1]     # yesterday's session is complete
    return _days["d"]


print(f"Tushare {ts.__version__}; window {s}-{e}\n")
for name, fn in CHECKS:
    try:
        df = fn()
        rows = 0 if df is None else len(df)
        print(f"  OK             {name}: {rows} rows")
    except Exception as exc:
        text = str(exc)
        if "权限" in text or "积分" in text:
            print(f"  NO PERMISSION  {name}: {text[:80]}")
        elif "每分钟" in text or "频率" in text:
            print(f"  RATE LIMIT     {name}: {text[:80]}")
        else:
            print(f"  ERROR          {name}: {type(exc).__name__}: {text[:80]}")
    time.sleep(0.4)
