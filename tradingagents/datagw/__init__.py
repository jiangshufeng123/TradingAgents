"""The data gateway (see ``core``): which sources serve which data, in what order.

    from tradingagents.datagw import gateway
    result = gateway().fetch("daily_bars", ticker="600519.SS", start=..., end=...)
    result.data, result.source

Default order per capability (change with ``TRADINGAGENTS_GATEWAY_ORDER``, e.g.
``daily_bars=sina,tushare;cn_spot=sina``):

=============  ===========================================  ===================
capability     what                                         sources, in order
=============  ===========================================  ===================
daily_bars     unadjusted daily bars, A-shares and HK       Tushare (A only), Sina, East Money
cn_spot        every A-share's quote and valuation          Tushare, East Money, Sina
lhb            a stock's 龙虎榜 rows in a date range         Tushare, East Money
margin         a stock's margin financing on one day        Tushare, SSE/SZSE (AkShare)
=============  ===========================================  ===================
"""

from __future__ import annotations

import threading

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.datagw import quality, tushare_pro
from tradingagents.datagw.core import (  # noqa: F401
    Gateway,
    GatewayNoDataError,
    GatewayUnavailableError,
    NotConfigured,
    QualityError,
    Result,
)

_lock = threading.Lock()
_gateway: Gateway | None = None
_built_for: tuple | None = None


# --- AkShare-backed providers (the sources used before the gateway existed) --------

def _akshare_configured() -> str | None:
    try:
        import akshare  # noqa: F401
    except ImportError:
        return "akshare not installed"
    return None


def _ak_bars(source: str):
    def fetch(ticker: str, start, end):
        from tradingagents.dataflows.vendors import akshare_cn

        return akshare_cn.bars_from(source, ticker, pd.Timestamp(start), pd.Timestamp(end))
    return fetch


def _em_spot(day: str | None = None):
    from tradingagents.dataflows.vendors.akshare_cn import call

    return call("stock_zh_a_spot_em", _retries=1)


def _sina_spot(day: str | None = None):
    from tradingagents import screener

    return screener.spot_sina()


def _em_lhb(ticker: str, start, end):
    from tradingagents.dataflows.vendors import akshare_cn

    return akshare_cn.lhb_em(ticker, pd.Timestamp(start), pd.Timestamp(end))


def _exchange_margin(ticker: str, day):
    from tradingagents.dataflows.vendors import akshare_cn

    found = akshare_cn.market_of(ticker)
    if found is None or found[0] == "hk":
        raise NoMarketDataError(ticker, ticker, "margin data is A-share only")
    return akshare_cn.margin_from_exchange(found[0], found[1], pd.Timestamp(day))


def build(cache_dir=None, order: str | None = None, breaker_failures: int = 3,
          breaker_seconds: float = 600) -> Gateway:
    gw = Gateway(cache_dir, order, breaker_failures, breaker_seconds)
    ts_ok = tushare_pro.configured

    gw.capability("daily_bars", "日线（A股/港股，不复权）", check=quality.check_bars)
    gw.provider("daily_bars", "tushare", "Tushare", tushare_pro.daily_bars, ts_ok)
    gw.provider("daily_bars", "sina", "Sina", _ak_bars("Sina"), _akshare_configured)
    gw.provider("daily_bars", "eastmoney", "East Money", _ak_bars("East Money"), _akshare_configured)

    gw.capability("cn_spot", "A股全市场行情与估值", check=quality.check_spot)
    gw.provider("cn_spot", "tushare", "Tushare", tushare_pro.cn_spot, ts_ok)
    gw.provider("cn_spot", "eastmoney", "East Money", _em_spot, _akshare_configured)
    gw.provider("cn_spot", "sina", "Sina", _sina_spot)

    gw.capability("lhb", "龙虎榜", check=quality.check_lhb, empty_ok=True)
    gw.provider("lhb", "tushare", "Tushare", tushare_pro.lhb, ts_ok)
    gw.provider("lhb", "eastmoney", "East Money", _em_lhb, _akshare_configured)

    gw.capability("margin", "融资融券", check=quality.check_margin, empty_ok=True)
    gw.provider("margin", "tushare", "Tushare", tushare_pro.margin, ts_ok)
    gw.provider("margin", "exchange", "SSE/SZSE", _exchange_margin, _akshare_configured)
    return gw


def gateway(config: dict | None = None) -> Gateway:
    """The process's gateway, rebuilt when the cache folder or the order setting changes."""
    global _gateway, _built_for
    if config is None:
        from tradingagents.dataflows.config import get_config

        config = get_config()
    key = (config.get("data_cache_dir"), config.get("gateway_order") or "",
           int(config.get("gateway_breaker_failures") or 3),
           float(config.get("gateway_breaker_seconds") or 600))
    with _lock:
        if _gateway is None or _built_for != key:
            _gateway, _built_for = build(*key), key
        return _gateway


def reset(clear_health: bool = False) -> None:
    """Forget the gateway (tests; after a settings change). ``clear_health`` also wipes its record."""
    global _gateway, _built_for
    with _lock:
        if clear_health and _gateway is not None:
            _gateway.health.reset()
        _gateway, _built_for = None, None
