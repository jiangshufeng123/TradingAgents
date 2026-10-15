"""Bring the single-user files into an account (safe to run again).

* .env TRADINGAGENTS_WATCHLIST and watchlist.txt -> watchlist
* TRADINGAGENTS_PORTFOLIO (portfolio.json)        -> holdings and cash
* <reports>/cost_ledger.csv                       -> costs
* <reports>/paper (the CLI's paper account)       -> the user's own paper folder

The files themselves are left as they are, so the CLI keeps working.
"""

from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.models import CostEntry
from server.services import paper as paper_svc
from server.services import portfolio as portfolio_svc
from server.services import watchlist as watchlist_svc


def _reports_base(config: dict) -> Path:
    return Path(config.get("reports_dir") or Path(config.get("results_dir", ".")) / "reports").expanduser()


def _float(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def import_all(s: Session, user_id: int, config: dict) -> dict:
    from tradingagents import watchlist as file_watchlist
    from tradingagents.paper import paper_dir

    summary = {}
    env_names = file_watchlist.from_env(config)
    file_names = [t for t, _ in file_watchlist.entries(config)]
    summary["watchlist"] = (watchlist_svc.add(s, user_id, env_names, ".env", "import")
                            + watchlist_svc.add(s, user_id, file_names, "watchlist.txt", "import"))

    summary["holdings"] = 0
    if config.get("portfolio_path") and Path(config["portfolio_path"]).expanduser().exists():
        from tradingagents.portfolio import load_portfolio

        book = load_portfolio(config["portfolio_path"])
        portfolio_svc.set_cash(s, user_id, book.cash, book.currency or "CNY")
        for p in book.positions:
            portfolio_svc.set_holding(s, user_id, p.ticker, p.quantity, p.average_price)
        summary["holdings"] = len(book.positions)

    summary["costs"] = 0
    ledger = _reports_base(config) / "cost_ledger.csv"
    if ledger.exists():
        seen = {(c.run_time.strftime("%Y-%m-%d %H:%M:%S"), c.ticker)
                for c in s.scalars(select(CostEntry).where(CostEntry.user_id == user_id))}
        with open(ledger, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                key = (row.get("run_time", ""), row.get("ticker", ""))
                if not key[0] or key in seen:
                    continue
                try:
                    when = datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                s.add(CostEntry(user_id=user_id, run_time=when, ticker=row.get("ticker", ""),
                                trade_date=row.get("trade_date", ""), rating=row.get("rating", ""),
                                llm_calls=int(_float(row.get("llm_calls")) or 0),
                                input_tokens=int(_float(row.get("input_tokens")) or 0),
                                output_tokens=int(_float(row.get("output_tokens")) or 0),
                                cost=_float(row.get("cost")), currency=row.get("currency", ""),
                                report_dir=row.get("report_dir", "")))
                seen.add(key)
                summary["costs"] += 1

    summary["paper"] = False
    source = paper_dir(config)
    target = paper_svc.user_dir(config, user_id)
    if (source / "account.json").exists() and not (target / "account.json").exists():
        target.mkdir(parents=True, exist_ok=True)
        for name in ("account.json", "trades.csv", "equity.csv"):
            if (source / name).exists():
                shutil.copy2(source / name, target / name)
        summary["paper"] = True
    s.flush()
    return summary
