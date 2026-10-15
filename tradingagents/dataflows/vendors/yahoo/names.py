"""A listing's display name from Yahoo (for the names shown next to codes)."""

from __future__ import annotations


def short_name(symbol: str) -> str | None:
    """Yahoo's short name (else long name) for ``symbol``; None when Yahoo has none."""
    import yfinance as yf

    info = yf.Ticker(symbol).get_info() or {}
    return info.get("shortName") or info.get("longName")
