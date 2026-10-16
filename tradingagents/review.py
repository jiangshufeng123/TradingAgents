"""A quick second look at a screen's leaders: one quick-model call per stock.

The screen ranks on numbers; this reads the same numbers plus the stock's recent news and
says, per stock, whether it deserves a full analysis: 关注 (worth a full analysis now),
观望 (nothing wrong, nothing pressing) or 回避 (a red flag the numbers do not show: a
share sale by holders, a regulator's inquiry, an earnings warning, a one-off gain behind
the profit growth...). It is a filter in front of the full multi-agent analysis, which
costs about a hundred times more; it is not a rating and is never traded on.

Each call gets only what is passed in (no tools), answers in a fixed JSON shape, and is
bounded in time; a stock whose call fails gets no review rather than an invented one.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, timedelta

logger = logging.getLogger(__name__)

VERDICTS = ("关注", "观望", "回避")
NEWS_DAYS = 21
NEWS_ITEMS = 8
CALL_SECONDS = 120

_FACTOR_NAMES = {"value": "价值", "quality": "质量", "growth": "成长", "momentum": "动量",
                 "low_vol": "低波动", "liquidity": "流动性"}
_METRICS = [("price", "最新价", "元"), ("pe", "市盈率(动态)", "倍"), ("pb", "市净率", "倍"),
            ("dividend_yield", "股息率", "%"), ("mcap", "总市值", "亿元"), ("roe_annualized", "ROE(年化)", "%"),
            ("gross_margin", "毛利率", "%"), ("revenue_yoy", "营收同比", "%"), ("profit_yoy", "净利同比", "%"),
            ("eps", "每股收益", "元"), ("ocf_per_share", "每股经营现金流", "元"), ("chg_60d", "60日涨跌", "%"),
            ("chg_ytd", "年初至今涨跌", "%"), ("mom_120_20", "120日涨跌(不含近20日)", "%"),
            ("vol_60", "60日年化波动率", "%"), ("drawdown_60", "距60日高点回撤", "%"), ("turnover_pct", "换手率", "%"),
            ("amount", "成交额", "亿元")]


def _num(v) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:                                    # NaN
        return None
    return f"{f:,.2f}"


def facts(row: dict) -> str:
    lines = [f"- {label}: {_num(row.get(key))} {unit}" for key, label, unit in _METRICS if _num(row.get(key))]
    scores = [f"{name} {round(float(row[f'{k}_score']))}" for k, name in _FACTOR_NAMES.items()
              if _num(row.get(f"{k}_score"))]
    if scores:
        lines.append("- 因子得分(0-100，同行业内排名，越高越好): " + "，".join(scores))
    if _num(row.get("score")):
        lines.append(f"- 综合分: {round(float(row['score']))}")
    return "\n".join(lines)


def news_digest(text: str, items: int = NEWS_ITEMS) -> str:
    """The first ``items`` headlines of a news block (``### title`` / date line / body), each
    with a short excerpt."""
    if not text or text.startswith("No "):
        return ""
    out = []
    for block in text.split("\n### ")[1:items + 1]:
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        if not lines:
            continue
        title, rest = lines[0], lines[1:]
        when = rest[0].split("·")[0].strip() if rest else ""
        body = rest[1][:120] if len(rest) > 1 else ""
        out.append(f"- [{when}] {title}" + (f"：{body}" if body else ""))
    return "\n".join(out)


def prompt(row: dict, news: str, ranking: str) -> str:
    return f"""你是 A 股研究员，要在完整研究之前对一只选股结果做快速初筛。只依据下面给出的数据和新闻，不要编造没有给出的事实。

股票：{row.get('ticker')} {row.get('name') or ''}（{row.get('industry') or '行业未知'}）
这次选股的排序方式：{ranking or '综合打分'}

数据：
{facts(row) or '- （没有数据）'}

近 {NEWS_DAYS} 天新闻（最多 {NEWS_ITEMS} 条，可能不全）：
{news or '- （没有取到新闻）'}

请判断这只股票是否值得马上做一次完整分析：
- 关注：数据和新闻都支持，值得马上完整分析；
- 观望：没有明显问题，但也没有急迫的理由；
- 回避：有数字没体现的风险，比如大股东减持、监管问询或处罚、业绩预警、利润主要来自一次性收益、财务数据前后矛盾、商誉减值风险。

只输出 JSON，不要其他文字：
{{"verdict": "关注|观望|回避", "thesis": "一到两句看好的理由，引用具体数据或新闻", "risks": ["最多三条主要风险，每条一句"], "watch": "接下来最该跟踪的一件事"}}"""


def parse(text: str) -> dict:
    """The model's JSON answer, checked; falls back to 观望 with the raw text when unreadable."""
    raw = str(text or "")
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    data = {}
    if m:
        try:
            data = json.loads(m.group(0))
        except ValueError:
            data = {}
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        verdict = next((v for v in VERDICTS if v in verdict or (not data and v in raw[:40])), "观望")
    risks = data.get("risks") or []
    if isinstance(risks, str):
        risks = [risks]
    return {"verdict": verdict,
            "thesis": str(data.get("thesis") or ("" if data else raw.strip()[:300])).strip(),
            "risks": [str(r).strip() for r in risks if str(r).strip()][:3],
            "watch": str(data.get("watch") or "").strip(),
            "readable": bool(data)}


def _usage(message) -> tuple[int, int, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    meta = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    cached = int((usage.get("input_token_details") or {}).get("cache_read") or meta.get("prompt_cache_hit_tokens") or 0)
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), cached


def review(rows: list[dict], llm, *, news=None, ranking: str = "", price=None, workers: int = 5,
           timeout: float = CALL_SECONDS, as_of: str | None = None) -> list[dict]:
    """One review per row, in the rows' order. ``news(ticker, start, end)`` returns the news
    block (or raises); ``price`` is (input, output, cached) per million tokens for the cost."""
    end = as_of or date.today().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=NEWS_DAYS)).isoformat()

    def one(row: dict) -> dict:
        ticker = row.get("ticker")
        digest = ""
        if news is not None and ticker:
            try:
                digest = news_digest(news(ticker, start, end))
            except Exception as exc:
                logger.info("%s: no news for the review (%s)", ticker, exc)
        message = llm.invoke(prompt(row, digest, ranking))
        out = parse(getattr(message, "content", message))
        tokens_in, tokens_out, cached = _usage(message)
        cost = None
        if price is not None:
            p_in, p_out, p_cached = price
            p_cached = p_in if p_cached is None else p_cached
            cost = ((tokens_in - cached) * p_in + cached * p_cached + tokens_out * p_out) / 1_000_000
        return {"ticker": ticker, "name": row.get("name"), **out, "news_items": len(digest.splitlines()),
                "input_tokens": tokens_in, "output_tokens": tokens_out, "cost": cost}

    import time

    results: list[dict] = []
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(rows) or 1)), thread_name_prefix="review")
    futures = [pool.submit(one, r) for r in rows]
    deadline = time.monotonic() + timeout          # the calls run side by side: one budget for all
    try:
        for row, future in zip(rows, futures):
            try:
                results.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
            except FutureTimeout:
                results.append({"ticker": row.get("ticker"), "name": row.get("name"), "error": "模型超时"})
            except Exception as exc:
                logger.warning("%s: review failed: %s", row.get("ticker"), exc)
                results.append({"ticker": row.get("ticker"), "name": row.get("name"),
                                "error": f"{type(exc).__name__}: {exc}"[:200]})
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # a call that never returns cannot hold the job
    return results
