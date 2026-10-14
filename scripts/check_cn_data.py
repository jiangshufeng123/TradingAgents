"""Smoke-test the A-share / Hong Kong data the agents will see (no LLM calls, no cost).

Run:  python scripts/check_cn_data.py
Writes scripts/cn_data_check.txt
"""
import io
import os
import time
import traceback
from datetime import date, timedelta

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.router import route_to_vendor
from tradingagents.default_config import DEFAULT_CONFIG

set_config(dict(DEFAULT_CONFIG))
today = date.today()
week_ago = (today - timedelta(days=7)).isoformat()
today = today.isoformat()

CHECKS = []
for ticker in ("600519.SS", "000858.SZ", "0700.HK"):
    CHECKS += [
        (ticker, "get_news", (ticker, week_ago, today)),
        (ticker, "get_fundamentals", (ticker, today)),
        (ticker, "get_income_statement", (ticker, "quarterly", today)),
        (ticker, "get_balance_sheet", (ticker, "annual", today)),
        (ticker, "get_cashflow", (ticker, "quarterly", today)),
        (ticker, "get_china_market_context", (ticker, today, 2)),
    ]
CHECKS.append(("600519.SS (backtest 2026-06-01)", "get_fundamentals", ("600519.SS", "2026-06-01")))
CHECKS.append(("NVDA (must still use Yahoo)", "get_news", ("NVDA", week_ago, today)))

out = io.StringIO()
for label, method, args in CHECKS:
    t = time.time()
    try:
        text = str(route_to_vendor(method, *args))
        print(f"\n===== OK  {label} :: {method} ({time.time() - t:.1f}s, {len(text)} chars)\n{text[:2500]}", file=out)
    except Exception as e:
        print(f"\n===== ERR {label} :: {method} ({time.time() - t:.1f}s) {type(e).__name__}: {e}", file=out)
        print("".join(traceback.format_exc().splitlines(True)[-6:]), file=out)
    print(f"{label} {method}: done", flush=True)

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cn_data_check.txt")
open(path, "w", encoding="utf-8").write(out.getvalue())
print("written", path)
