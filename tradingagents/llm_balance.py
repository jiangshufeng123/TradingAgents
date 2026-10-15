"""The model provider's account balance, where the provider publishes one.

DeepSeek: ``GET https://api.deepseek.com/user/balance`` with the API key, which
answers whether the account can be used and, per currency, the total balance
split into granted (free) and topped-up amounts. The key is read from the
environment (.env) and never shown or returned.
"""

from __future__ import annotations

import os
import time

ENDPOINTS = {
    "deepseek": ("https://api.deepseek.com/user/balance", "DEEPSEEK_API_KEY"),
}

_cache: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 300


def supported(provider: str) -> bool:
    return (provider or "").lower() in ENDPOINTS


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def balance(provider: str = "deepseek", timeout: float = 10, max_age: float = CACHE_SECONDS) -> dict:
    """``{"provider", "ok", "available", "balances": [{currency, total, granted, topped_up}],
    "checked_at", "error"}``; ``ok`` is False (with ``error``) when it could not be read."""
    provider = (provider or "").lower()
    if provider not in ENDPOINTS:
        return {"provider": provider, "ok": False, "error": "this provider publishes no balance endpoint"}
    hit = _cache.get(provider)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    url, env = ENDPOINTS[provider]
    key = os.environ.get(env, "").strip()
    if not key:
        return {"provider": provider, "ok": False, "error": f"{env} not set"}
    import requests

    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                            timeout=timeout)
        if resp.status_code == 401:
            return {"provider": provider, "ok": False, "error": "the API key was refused (401)"}
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        # The message names the URL, never the key (it travels in a header).
        return {"provider": provider, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    result = {
        "provider": provider, "ok": True, "available": bool(data.get("is_available")),
        "balances": [{"currency": b.get("currency"), "total": _num(b.get("total_balance")),
                      "granted": _num(b.get("granted_balance")),
                      "topped_up": _num(b.get("topped_up_balance"))}
                     for b in data.get("balance_infos") or []],
        "checked_at": time.time(), "error": None,
    }
    _cache[provider] = (time.time(), result)
    return result


def summary_line(info: dict) -> str:
    if not info.get("ok"):
        return f"{info.get('provider')}: balance unavailable ({info.get('error')})"
    parts = [f"{b['currency']} {b['total']:,.2f} (topped up {b['topped_up']:,.2f}, granted {b['granted']:,.2f})"
             for b in info["balances"] if b.get("total")]       # a currency never topped up shows 0
    state = "" if info.get("available") else " — account cannot be used now"
    return f"{info['provider']} balance: {'; '.join(parts) or 'none'}{state}"
