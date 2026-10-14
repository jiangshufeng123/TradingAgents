"""Probe which AkShare endpoints work on this machine, and what they return.

Run:  python scripts/probe_akshare.py
Writes scripts/akshare_probe_output.txt (columns, sample rows, errors).
"""
import io
import os
import sys
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr

# Chinese data sites are reached directly, not through an overseas proxy.
CN_HOSTS = ["eastmoney.com", "sina.com.cn", "sinajs.cn", "10jqka.com.cn", "cls.cn",
            "legulegu.com", "baidu.com", "xueqiu.com", "cninfo.com.cn", "szse.cn", "sse.com.cn",
            "hkex.com.hk", "csindex.com.cn", "jin10.com", "stats.gov.cn"]
for var in ("NO_PROXY", "no_proxy"):
    existing = [h for h in os.environ.get(var, "").split(",") if h]
    os.environ[var] = ",".join(existing + [h for h in CN_HOSTS if h not in existing])

import pandas as pd
import akshare as ak

pd.set_option("display.width", 250)
pd.set_option("display.max_columns", 40)
pd.set_option("display.max_colwidth", 60)

START, END = "20260801", "20261014"
CASES = [
    # A-shares
    ("A 日线", lambda: ak.stock_zh_a_hist(symbol="600519", period="daily", start_date=START, end_date=END, adjust="qfq")),
    ("A 个股信息", lambda: ak.stock_individual_info_em(symbol="600519")),
    ("A 个股新闻", lambda: ak.stock_news_em(symbol="600519")),
    ("A 利润表(新浪)", lambda: ak.stock_financial_report_sina(stock="sh600519", symbol="利润表")),
    ("A 资产负债表(新浪)", lambda: ak.stock_financial_report_sina(stock="sh600519", symbol="资产负债表")),
    ("A 财务摘要(同花顺)", lambda: ak.stock_financial_abstract_ths(symbol="600519", indicator="按报告期")),
    ("A 财务指标(新浪)", lambda: ak.stock_financial_analysis_indicator(symbol="600519", start_year="2024")),
    ("A 估值(东财)", lambda: ak.stock_value_em(symbol="600519")),
    ("A 估值(乐咕)", lambda: ak.stock_a_indicator_lg(symbol="600519")),
    ("A 资金流", lambda: ak.stock_individual_fund_flow(stock="600519", market="sh")),
    ("A 业绩预告/报告披露", lambda: ak.stock_yjbb_em(date="20260630")),
    ("A 高管增减持", lambda: ak.stock_ggcg_em(symbol="全部")),
    # Hong Kong
    ("HK 日线", lambda: ak.stock_hk_hist(symbol="00700", period="daily", start_date=START, end_date=END, adjust="qfq")),
    ("HK 公司资料", lambda: ak.stock_hk_company_profile_em(symbol="00700")),
    ("HK 财务指标", lambda: ak.stock_hk_financial_indicator_em(symbol="00700")),
    ("HK 利润表", lambda: ak.stock_financial_hk_report_em(stock="00700", symbol="利润表", indicator="报告期")),
    ("HK 资产负债表", lambda: ak.stock_financial_hk_report_em(stock="00700", symbol="资产负债表", indicator="报告期")),
    ("HK 估值(百度)", lambda: ak.stock_hk_valuation_baidu(symbol="00700", indicator="市盈率(TTM)", period="近一年")),
    ("HK 个股新闻", lambda: ak.stock_news_em(symbol="00700")),
    # Market-wide
    ("财联社电报", lambda: ak.stock_info_global_cls(symbol="全部")),
    ("东财全球快讯", lambda: ak.stock_info_global_em()),
    ("沪深港通资金", lambda: ak.stock_hsgt_fund_flow_summary_em()),
    ("中国CPI", lambda: ak.macro_china_cpi_yearly()),
    ("中国LPR", lambda: ak.macro_china_lpr()),
]

out = io.StringIO()
print(f"akshare {ak.__version__}  python {sys.version.split()[0]}  "
      f"HTTPS_PROXY={'set' if os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') else 'unset'}", file=out)
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
            if len(df) > 3:
                print("... last row:", file=out)
                print(df.tail(1).to_string()[:800], file=out)
        else:
            print(f"\n=== OK  {name}  ({took:.1f}s)  type={type(df).__name__}: {str(df)[:500]}", file=out)
    except Exception as e:
        print(f"\n=== ERR {name}  ({time.time() - t:.1f}s)  {type(e).__name__}: {str(e)[:300]}", file=out)
        print(traceback.format_exc().splitlines()[-3:], file=out)
    print(f"{name}: done", flush=True)

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "akshare_probe_output.txt")
with open(path, "w", encoding="utf-8") as f:
    f.write(out.getvalue())
print("written", path)
