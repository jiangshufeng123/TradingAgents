"""Quality checks: an answer that fails one is treated as a fault and the next source is asked."""

from __future__ import annotations

import pandas as pd

from tradingagents.datagw.core import QualityError

BAR_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def check_bars(df: pd.DataFrame, kwargs: dict) -> None:
    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        raise QualityError(f"missing columns {missing}")
    close, high, low = (pd.to_numeric(df[c], errors="coerce") for c in ("Close", "High", "Low"))
    if close.isna().any() or (close <= 0).any():
        raise QualityError("non-positive or missing close")
    if (high < low).any():
        raise QualityError("high below low")
    if ((close > high * 1.01) | (close < low * 0.99)).any():
        raise QualityError("close outside the day's range")
    if (pd.to_numeric(df["Volume"], errors="coerce") < 0).any():
        raise QualityError("negative volume")
    if df["Date"].duplicated().any():
        raise QualityError("duplicate dates")
    start, end = kwargs.get("start"), kwargs.get("end")
    if start is not None and end is not None:
        dates = pd.to_datetime(df["Date"])
        if (dates < pd.Timestamp(start).normalize()).any() or (dates > pd.Timestamp(end).normalize()).any():
            raise QualityError("bars outside the requested dates")


SPOT_COLUMNS = ["代码", "名称", "最新价", "涨跌幅", "换手率", "成交额", "市盈率-动态", "市净率", "总市值", "流通市值"]
MIN_A_SHARES = 3000          # some 5,400 are listed; far fewer means a truncated download


def check_spot(df: pd.DataFrame, kwargs: dict) -> None:
    missing = [c for c in SPOT_COLUMNS if c not in df.columns]
    if missing:
        raise QualityError(f"missing columns {missing}")
    if len(df) < MIN_A_SHARES:
        raise QualityError(f"only {len(df)} listings (expected over {MIN_A_SHARES})")
    for col in ("最新价", "总市值"):
        share = pd.to_numeric(df[col], errors="coerce").gt(0).mean()
        if share < 0.8:
            raise QualityError(f"{col} present for only {share:.0%} of listings")
    if df["代码"].astype(str).duplicated().mean() > 0.01:
        raise QualityError("duplicate codes")


LHB_COLUMNS = ["date", "reason", "chg_pct", "net_buy", "net_pct", "turnover"]


def check_lhb(df: pd.DataFrame, kwargs: dict) -> None:
    missing = [c for c in LHB_COLUMNS if c not in df.columns]
    if missing:
        raise QualityError(f"missing columns {missing}")


def check_margin(row: dict, kwargs: dict) -> None:
    balance = row.get("balance")
    if balance is not None and balance < 0:
        raise QualityError("negative financing balance")
