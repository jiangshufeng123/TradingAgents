"""Multi-factor scoring for the screener: rank what passed the filters, in code.

A screen first filters (the conditions the user can read and edit), then scores what is
left on six factors and ranks by their weighted mix. The model never ranks stocks: it can
only pick a preset or set the weights, both shown with the result.

Factors, each the average of its metrics' percentile ranks (0-100, higher is better):

==========  =================================================  =========================
factor      metrics                                             where from
==========  =================================================  =========================
value       earnings yield (1/PE), book yield (1/PB),           the whole-market quote
            dividend yield                                      table (Tushare's has the
                                                                dividend yield)
quality     annualized ROE, gross margin, cash conversion       latest quarterly results
            (operating cash flow / earnings)
growth      revenue growth, profit growth (year on year)        latest quarterly results
momentum    60-day change, year-to-date change; for the         quotes; daily bars
            leaders, 120-day change skipping the last 20
low_vol     low turnover; for the leaders, low 60-day           quotes; daily bars
            volatility and shallow 60-day drawdown
liquidity   turnover value                                      quotes
==========  =================================================  =========================

Ranks are taken within the industry when it has at least ``MIN_PEERS`` listings in the
pool (so banks are compared with banks), else across the pool. A missing metric counts
as the middle (50) rather than as the worst or the best.

Two stages keep the data cost down: every filtered listing is scored on the quote and
results metrics; the ``tech_top`` best of those also get the daily-bar metrics (one
cached price download each) and are scored again with them.

Screening reads today's market; it is for picking what to analyze now, and the forward
check (``server/services/screen_track.py``) is what says whether a preset works.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

FACTORS = {
    "value": "价值",
    "quality": "质量",
    "growth": "成长",
    "momentum": "动量",
    "low_vol": "低波动",
    "liquidity": "流动性",
}

# metric -> (factor, higher is better)
METRICS = {
    "ep": ("value", True), "bp": ("value", True), "dividend_yield": ("value", True),
    "roe_annualized": ("quality", True), "gross_margin": ("quality", True), "cash_conversion": ("quality", True),
    "revenue_yoy": ("growth", True), "profit_yoy": ("growth", True),
    "chg_60d": ("momentum", True), "chg_ytd": ("momentum", True), "mom_120_20": ("momentum", True),
    "turnover_pct": ("low_vol", False), "vol_60": ("low_vol", False), "drawdown_60": ("low_vol", False),
    "amount": ("liquidity", True),
}
TECH_METRICS = ("mom_120_20", "vol_60", "drawdown_60")
MIN_PEERS = 5


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    summary: str
    weights: dict
    filters: tuple = ()


PRESETS = {p.key: p for p in (
    Preset("balanced", "均衡", "六个因子等权，适合没有特定偏好时先看一遍全市场。",
           {"value": 1, "quality": 1, "growth": 1, "momentum": 1, "low_vol": 1, "liquidity": 0.5},
           ({"field": "mcap", "op": ">=", "value": 30}, {"field": "amount", "op": ">=", "value": 0.5})),
    Preset("dividend_lowvol", "红利低波", "估值低、分红高、走势稳的大中盘股，偏防守。",
           {"value": 2, "low_vol": 2, "quality": 1, "liquidity": 0.5},
           ({"field": "pe", "op": "between", "value": [0, 30]}, {"field": "mcap", "op": ">=", "value": 100})),
    Preset("quality_growth", "质量成长", "盈利能力强、还在增长的公司，估值放在次要位置。",
           {"quality": 2, "growth": 2, "momentum": 0.5, "value": 0.5},
           ({"field": "roe_annualized", "op": ">=", "value": 10}, {"field": "profit_yoy", "op": ">", "value": 0},
            {"field": "mcap", "op": ">=", "value": 50})),
    Preset("value_recovery", "价值修复", "便宜且基本面过得去，等待估值修复，偏逆向。",
           {"value": 2, "quality": 1, "growth": 0.5, "low_vol": 0.5},
           ({"field": "pe", "op": "between", "value": [0, 20]}, {"field": "pb", "op": "<", "value": 2},
            {"field": "mcap", "op": ">=", "value": 50})),
    Preset("trend_momentum", "趋势动量", "中期走势强、成交活跃的股票，偏进攻，回撤也更大。",
           {"momentum": 2, "liquidity": 1, "quality": 0.5},
           ({"field": "amount", "op": ">=", "value": 2}, {"field": "chg_60d", "op": ">", "value": 0})),
)}
DEFAULT_PRESET = "balanced"


def weights_for(preset: str | None, weights: dict | None) -> dict:
    """The factor weights to use: explicit weights win, then the preset's, then balanced.
    Unknown factors and negative or non-numeric weights are dropped."""
    chosen = {}
    for k, v in (weights or {}).items():
        try:
            if k in FACTORS and float(v) > 0:
                chosen[k] = float(v)
        except (TypeError, ValueError):
            continue
    if chosen:
        return chosen
    return dict(PRESETS.get(preset or DEFAULT_PRESET, PRESETS[DEFAULT_PRESET]).weights)


# --- metrics --------------------------------------------------------------------------

def base_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """The quote/results metrics, derived where needed (non-positive PE/PB/EPS: no yield)."""
    out = pd.DataFrame(index=df.index)
    pe = pd.to_numeric(df.get("pe"), errors="coerce")
    pb = pd.to_numeric(df.get("pb"), errors="coerce")
    eps = pd.to_numeric(df.get("eps"), errors="coerce")
    ocf = pd.to_numeric(df.get("ocf_per_share"), errors="coerce")
    out["ep"] = (1 / pe).where(pe > 0, 0.0).where(pe.notna())
    out["bp"] = (1 / pb).where(pb > 0)
    out["cash_conversion"] = (ocf / eps).where(eps > 0).clip(-3, 3)
    for name in ("dividend_yield", "roe_annualized", "gross_margin", "revenue_yoy", "profit_yoy",
                 "chg_60d", "chg_ytd", "turnover_pct", "amount"):
        out[name] = pd.to_numeric(df[name], errors="coerce") if name in df.columns else float("nan")
    for name in TECH_METRICS:
        out[name] = pd.to_numeric(df[name], errors="coerce") if name in df.columns else float("nan")
    return out


def tech_metrics(bars: pd.DataFrame) -> dict:
    """Daily-bar metrics from a price frame (Date, Close), oldest first."""
    close = pd.to_numeric(bars["Close"], errors="coerce").dropna()
    out = {name: float("nan") for name in TECH_METRICS}
    if len(close) >= 121:
        out["mom_120_20"] = (close.iloc[-21] / close.iloc[-121] - 1) * 100
    if len(close) >= 61:
        last = close.iloc[-61:]
        returns = last.pct_change().dropna()
        out["vol_60"] = float(returns.std() * math.sqrt(250) * 100)
        out["drawdown_60"] = float((1 - last.iloc[-1] / last.max()) * 100)
    return out


def fetch_tech(tickers: list[str], as_of: str, load, workers: int = 6) -> pd.DataFrame:
    """Daily-bar metrics per ticker (rows indexed by ticker); a ticker that fails is left out."""
    def one(t):
        try:
            return t, tech_metrics(load(t, as_of))
        except Exception as exc:
            logger.info("%s: no daily bars for scoring (%s)", t, exc)
            return t, None
    rows = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tickers) or 1))) as pool:
        for t, m in pool.map(one, tickers):
            if m is not None:
                rows[t] = m
    return pd.DataFrame.from_dict(rows, orient="index", columns=list(TECH_METRICS))


# --- scoring --------------------------------------------------------------------------

def _rank(values: pd.Series, groups: pd.Series, higher: bool) -> pd.Series:
    """Percentile rank 0-100 (higher is better), within groups of MIN_PEERS or more, else
    across all; missing values score 50."""
    v = values.astype(float)
    if not higher:
        v = -v
    overall = v.rank(pct=True) * 100
    sizes = groups.map(groups.value_counts())
    by_group = v.groupby(groups).rank(pct=True) * 100
    ranked = by_group.where(sizes >= MIN_PEERS, overall)
    return ranked.where(v.notna(), 50.0)


def score(df: pd.DataFrame, weights: dict) -> pd.DataFrame:
    """``df`` with a ``<factor>_score`` column per factor and ``score``, the weighted mix."""
    out = df.copy()
    metrics = base_metrics(out)
    groups = out["industry"].fillna("—").astype(str) if "industry" in out.columns else pd.Series("—", index=out.index)
    per_factor: dict[str, list[pd.Series]] = {f: [] for f in FACTORS}
    for name, (factor, higher) in METRICS.items():
        col = metrics[name]
        if col.notna().sum() == 0:
            continue                                   # a metric nobody has does not dilute the factor
        per_factor[factor].append(_rank(col, groups, higher))
    for factor, ranks in per_factor.items():
        out[f"{factor}_score"] = (sum(ranks) / len(ranks)) if ranks else 50.0
    total = sum(weights.values()) or 1.0
    out["score"] = sum(out[f"{f}_score"] * w for f, w in weights.items()) / total
    return out


def rank(df: pd.DataFrame, weights: dict, limit: int, as_of: str, load=None, tech_top: int = 60) -> pd.DataFrame:
    """Score ``df``, add the daily-bar metrics for the best ``tech_top`` and score those
    again with them; the top ``limit``, best first."""
    if df.empty:
        out = df.copy()
        for f in FACTORS:
            out[f"{f}_score"] = pd.Series(dtype=float)
        out["score"] = pd.Series(dtype=float)
        return out
    first = score(df, weights).sort_values("score", ascending=False)
    if load is None or tech_top <= 0:
        return first.head(limit).reset_index(drop=True)
    leaders = first.head(max(limit, tech_top)).copy()
    tickers = [t for t in leaders["ticker"].dropna().tolist()]
    tech = fetch_tech(tickers, as_of, load)
    for name in TECH_METRICS:
        leaders[name] = leaders["ticker"].map(tech[name]) if not tech.empty else float("nan")
    second = score(leaders.drop(columns=[c for c in leaders.columns if c.endswith("_score") or c == "score"]),
                   weights)
    return second.sort_values("score", ascending=False).head(limit).reset_index(drop=True)
