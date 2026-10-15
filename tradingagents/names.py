"""Security names for display: 600519.SS -> 贵州茅台, 0700.HK -> 騰訊控股, NVDA -> NVIDIA Corporation.

Sources, cached in ``<data_cache_dir>/names.json``:

* A-shares (Shanghai, Shenzhen, Beijing): Tushare ``stock_basic``, else AkShare's
  ``stock_info_a_code_name``; the whole table, refreshed weekly.
* Hong Kong: HKEX's own list of securities in Chinese (ListOfSecurities_c.xlsx),
  else the English list; refreshed weekly.
* Anything else (US): Yahoo's short name, one symbol at a time, kept 90 days.

A name is only for people to read: every lookup fails quietly to ``None``, and a
failed source is not asked again for a while, so a missing name never slows or
breaks a page, a report or a run.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TABLE_DAYS = 7
US_DAYS = 90
RETRY_SECONDS = 6 * 3600          # after a failed download, wait before asking again

HKEX_LIST_ZH = "https://www.hkex.com.hk/chi/services/trading/securities/securitieslists/ListOfSecurities_c.xlsx"
HKEX_LIST_EN = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"

_lock = threading.RLock()
_state: dict | None = None
_path: Path | None = None


def _cache_path() -> Path:
    from tradingagents.dataflows.config import get_config

    base = Path(get_config().get("data_cache_dir") or Path.home() / ".tradingagents" / "cache").expanduser()
    return base / "names.json"


def _load() -> dict:
    global _state, _path
    path = _cache_path()
    if _state is None or path != _path:
        _path = path
        try:
            _state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _state = {}
        _state.setdefault("tables", {})
        _state.setdefault("us", {})
        _state.setdefault("failed", {})
    return _state


def _save() -> None:
    if _state is None or _path is None:
        return
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(_state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _path)
    except OSError as exc:
        logger.debug("names cache not saved: %s", exc)


def reset() -> None:
    """Forget the in-memory copy (tests; another cache folder)."""
    global _state, _path
    with _lock:
        _state, _path = None, None


# --- the tables ------------------------------------------------------------------------

def _cn_table() -> dict[str, str]:
    """{6-digit code: name} for every listed A-share."""
    from tradingagents.datagw import tushare_pro

    if tushare_pro.configured() is None:      # None: token set and library installed
        try:
            df = tushare_pro.query("stock_basic", exchange="", list_status="L", fields="ts_code,name")
            if not df.empty:
                return {str(c)[:6]: str(n) for c, n in zip(df["ts_code"], df["name"], strict=False)}
        except Exception as exc:
            logger.debug("names from Tushare failed: %s", exc)
    from tradingagents.dataflows.vendors.akshare_cn import call

    df = call("stock_info_a_code_name", _retries=1)
    return {str(c).zfill(6): str(n) for c, n in zip(df["code"], df["name"], strict=False)}


def _hkex_table(url: str, code_col: str, name_col: str) -> dict[str, str]:
    import pandas as pd
    import requests

    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    raw = pd.read_excel(io.BytesIO(resp.content), header=None, dtype=str)
    header = next(i for i, row in raw.iterrows() if any(str(v).strip() == code_col for v in row.values))
    table = raw.iloc[header + 1:]
    table.columns = [str(c).strip() for c in raw.iloc[header]]
    out = {}
    for code, name in zip(table[code_col], table[name_col], strict=False):
        try:
            out[str(int(str(code).strip())).zfill(5)] = str(name).strip()
        except ValueError:
            continue
    return out


def _hk_table() -> dict[str, str]:
    """{5-digit code: name} for every Hong Kong listing, in Chinese when HKEX serves it."""
    try:
        return _hkex_table(HKEX_LIST_ZH, "股份代號", "股份名稱")
    except Exception as exc:
        logger.debug("HKEX Chinese list failed (%s); using the English list", exc)
    return _hkex_table(HKEX_LIST_EN, "Stock Code", "Name of Securities")


_LOADERS = {"cn": _cn_table, "hk": _hk_table}


MISS_REFRESH_SECONDS = 86400       # a code not in a day-old table (a new listing) refreshes it


def _table(market: str, key: str | None = None) -> dict[str, str]:
    st = _load()
    t = st["tables"].get(market)
    age = time.time() - (t or {}).get("at", 0)
    fresh = t and age < TABLE_DAYS * 86400 and not (key and key not in t["map"] and age > MISS_REFRESH_SECONDS)
    if fresh:
        return t["map"]
    if time.time() - st["failed"].get(market, 0) < RETRY_SECONDS:
        return (t or {}).get("map", {})
    try:
        table = _LOADERS[market]()
        if not table:
            raise ValueError("empty table")
    except Exception as exc:
        logger.info("security names for %s unavailable: %s", market, exc)
        st["failed"][market] = time.time()
        _save()
        return (t or {}).get("map", {})
    st["tables"][market] = {"at": time.time(), "map": table}
    st["failed"].pop(market, None)
    _save()
    return table


def _us_name(symbol: str) -> str | None:
    st = _load()
    hit = st["us"].get(symbol)
    if hit and time.time() - hit.get("at", 0) < (US_DAYS * 86400 if hit.get("name") else RETRY_SECONDS):
        return hit.get("name")
    name = None
    try:
        from tradingagents.dataflows.vendors.yahoo.names import short_name

        name = short_name(symbol)
    except Exception as exc:
        logger.debug("name of %s from Yahoo failed: %s", symbol, exc)
    st["us"][symbol] = {"name": name, "at": time.time()}
    _save()
    return name


# --- lookups -------------------------------------------------------------------------

def _where(ticker: str) -> tuple[str, str] | None:
    """("cn", 6-digit code) / ("hk", 5-digit code) / ("us", symbol); None when not a listing."""
    from tradingagents.dataflows.symbols import normalize_symbol

    t = (ticker or "").strip().upper()
    if not t:
        return None
    try:
        canonical = str(normalize_symbol(t)).upper()
    except Exception:
        canonical = t
    code, _, suffix = canonical.rpartition(".")
    if suffix in ("SS", "SH", "SZ", "BJ") and code.isdigit() and len(code) == 6:
        return "cn", code
    if suffix == "HK" and code.isdigit():
        return "hk", code.zfill(5)
    if canonical.isdigit() and len(canonical) == 6:
        return "cn", canonical
    if "." in canonical and suffix not in ("US",):
        return None                              # another exchange: no source here
    return "us", canonical


def name_of(ticker: str) -> str | None:
    """The listing's name, or None when no source knows it."""
    where = _where(ticker)
    if where is None:
        return None
    market, key = where
    with _lock:
        try:
            if market == "us":
                return _us_name(key)
            return _table(market, key).get(key)
        except Exception as exc:          # never let a name break the caller
            logger.debug("name of %s failed: %s", ticker, exc)
            return None


def names_for(tickers) -> dict[str, str | None]:
    return {t: name_of(t) for t in dict.fromkeys(tickers or [])}


def label(ticker: str) -> str:
    """'600519.SS 贵州茅台', or the bare ticker when the name is unknown."""
    name = name_of(ticker)
    return f"{ticker} {name}" if name else ticker
