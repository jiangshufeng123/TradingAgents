"""Second probe: retry the endpoints that failed, slowly, with a known-good control first.

Run:  python scripts/probe_akshare_2.py   ->  scripts/akshare_probe_output_2.txt
"""
import io, os, sys, time, traceback, urllib.request
from contextlib import redirect_stdout, redirect_stderr

CN_HOSTS = ["eastmoney.com", "sina.com.cn", "sinajs.cn", "10jqka.com.cn", "cls.cn", "baidu.com",
            "xueqiu.com", "cninfo.com.cn", "jin10.com", "stats.gov.cn", "hkex.com.hk"]
for var in ("NO_PROXY", "no_proxy"):
    existing = [h for h in os.environ.get(var, "").split(",") if h]
    os.environ[var] = ",".join(existing + [h for h in CN_HOSTS if h not in existing])

import pandas as pd
import akshare as ak
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40); pd.set_option("display.max_colwidth", 50)

CASES = [
    ("对照: A 日线", lambda: ak.stock_zh_a_hist(symbol="600519", period="daily", start_date="20261001", end_date="20261014", adjust="qfq")),
    ("HK 日线(东财)", lambda: ak.stock_hk_hist(symbol="00700", period="daily", start_date="20260901", end_date="20261014", adjust="qfq")),
    ("HK 日线(新浪)", lambda: ak.stock_hk_daily(symbol="00700", adjust="qfq")),
    ("HK 公司资料", lambda: ak.stock_hk_company_profile_em(symbol="00700")),
    ("HK 财务指标", lambda: ak.stock_hk_financial_indicator_em(symbol="00700")),
    ("HK 利润表", lambda: ak.stock_financial_hk_report_em(stock="00700", symbol="利润表", indicator="报告期")),
    ("HK 资产负债表", lambda: ak.stock_financial_hk_report_em(stock="00700", symbol="资产负债表", indicator="报告期")),
    ("HK 现金流量表", lambda: ak.stock_financial_hk_report_em(stock="00700", symbol="现金流量表", indicator="报告期")),
    ("HK 估值(百度)", lambda: ak.stock_hk_valuation_baidu(symbol="00700", indicator="市盈率(TTM)", period="近一年")),
    ("HK 个股新闻", lambda: ak.stock_news_em(symbol="00700")),
    ("HK 个股新闻(带后缀)", lambda: ak.stock_news_em(symbol="腾讯控股")),
    ("A 现金流量表(新浪)", lambda: ak.stock_financial_report_sina(stock="sh600519", symbol="现金流量表")),
    ("A 公司概况(新浪)", lambda: ak.stock_profile_cninfo(symbol="600519")),
    ("A 个股信息(雪球)", lambda: ak.stock_individual_basic_info_xq(symbol="SH600519")),
    ("东财全球快讯", lambda: ak.stock_info_global_em()),
    ("财联社电报", lambda: ak.stock_info_global_cls(symbol="全部")),
    ("沪深港通资金", lambda: ak.stock_hsgt_fund_flow_summary_em()),
    ("中国CPI(东财)", lambda: ak.macro_china_cpi()),
    ("中国LPR", lambda: ak.macro_china_lpr()),
    ("中国PMI", lambda: ak.macro_china_pmi()),
]

out = io.StringIO()
print(f"akshare {ak.__version__}; env HTTPS_PROXY={os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')}; "
      f"system proxies={urllib.request.getproxies()}", file=out)
for name, call in CASES:
    t = time.time()
    try:
        noise = io.StringIO()
        with redirect_stdout(noise), redirect_stderr(noise):
            df = call()
        took = time.time() - t
        if isinstance(df, pd.DataFrame):
            print(f"\n=== OK  {name}  ({took:.1f}s)  shape={df.shape}", file=out)
            print("columns:", list(df.columns), file=out)
            print(df.head(3).to_string()[:1500], file=out)
            print("... last row:", file=out); print(df.tail(1).to_string()[:800], file=out)
        else:
            print(f"\n=== OK  {name}  ({took:.1f}s) {type(df).__name__}: {str(df)[:400]}", file=out)
    except Exception as e:
        print(f"\n=== ERR {name}  ({time.time() - t:.1f}s)  {type(e).__name__}: {str(e)[:300]}", file=out)
    print(f"{name}: done", flush=True)
    time.sleep(3)

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "akshare_probe_output_2.txt")
open(path, "w", encoding="utf-8").write(out.getvalue())
print("written", path)
