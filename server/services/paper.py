"""Each user's paper account: the core PaperAccount in a folder of the user's own."""

from __future__ import annotations

from pathlib import Path

from tradingagents.paper import PaperAccount, paper_dir


def user_dir(config: dict, user_id: int) -> Path:
    return paper_dir(config) / "users" / str(user_id)


def account(config: dict, user_id: int) -> PaperAccount:
    return PaperAccount({**config, "paper_dir": str(user_dir(config, user_id))})
