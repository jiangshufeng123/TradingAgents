"""Each user's paper account: the core PaperAccount in a folder of the user's own."""

from __future__ import annotations

from pathlib import Path

from tradingagents.paper import PaperAccount, paper_dir


def user_dir(config: dict, user_id: int) -> Path:
    return paper_dir(config) / "users" / str(user_id)


def account(config: dict, user_id: int) -> PaperAccount:
    return PaperAccount({**config, "paper_dir": str(user_dir(config, user_id))})


def exists(config: dict, user_id: int) -> bool:
    return (user_dir(config, user_id) / "account.json").exists()


def orders_on(config: dict, user_id: int) -> bool:
    """Whether this user's analyses place paper orders: the account exists and is switched on."""
    if not exists(config, user_id):
        return False
    return bool(account(config, user_id).state.get("enabled", True))


def open_account(config: dict, user_id: int, cash: float, currency: str = "CNY") -> PaperAccount:
    """Open (or start over) the user's paper account; earlier trades are kept as *_old.csv."""
    acct = account(config, user_id)
    acct.reset(cash, currency)
    acct.state["enabled"] = True
    acct.save()
    return acct


def set_enabled(config: dict, user_id: int, enabled: bool) -> None:
    acct = account(config, user_id)
    acct.state["enabled"] = bool(enabled)
    acct.save()
