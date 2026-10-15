"""Reuse an analyst's report for the same listing and trading day.

The four analysts (market, sentiment, news, fundamentals) gather data and write evidence; they
do not see the book, the memory log or the other agents. Run again for the same ticker and
day - a second vote run, a re-run after a failure, the evening batch after a manual run - and
they would fetch the same data and write much the same report, at roughly half the run's
model cost. So each finished report is kept, and a later run takes it instead of asking the
model again, within a time limit:

* a trading day still in progress (today or later): ``analyst_report_reuse_hours`` (default 4),
  since prices and news move during the day;
* a past trading day, whose data no longer changes: up to 30 days.

The key also holds what shapes the report: the analyst, asset type, instrument description,
quick model and provider, and output language. 0 hours turns reuse off.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

PAST_DAY_SECONDS = 30 * 24 * 3600
VERSION = 1                                   # bump when analyst prompts change materially

ANALYST_NAMES = {"market": "市场分析师", "social": "情绪分析师", "news": "新闻分析师",
                 "fundamentals": "基本面分析师"}


class ReportCache:
    def __init__(self, root: Path, hours: float, model: str, provider: str, language: str):
        self.root = Path(root)
        self.hours = float(hours)
        self.model, self.provider, self.language = model or "", provider or "", language or ""

    @classmethod
    def from_config(cls, config: dict) -> "ReportCache | None":
        try:
            hours = float(config.get("analyst_report_reuse_hours", 4) or 0)
        except (TypeError, ValueError):
            hours = 0.0
        if hours <= 0:
            return None
        root = Path(config.get("data_cache_dir") or Path.home() / ".tradingagents" / "cache") / "analyst_reports"
        return cls(root, hours,
                   config.get("quick_think_llm") or "",
                   config.get("quick_think_provider") or config.get("llm_provider") or "",
                   config.get("output_language") or "")

    # --- keys ------------------------------------------------------------------------

    def _key(self, analyst: str, state: dict) -> str:
        parts = [VERSION, analyst, str(state.get("company_of_interest") or "").upper(),
                 str(state.get("trade_date") or ""), state.get("asset_type") or "",
                 state.get("instrument_context") or "", self.model, self.provider, self.language]
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]

    def _path(self, analyst: str, state: dict) -> Path:
        ticker = "".join(c if c.isalnum() or c in ".-_" else "_"
                         for c in str(state.get("company_of_interest") or "unknown").upper())
        day = str(state.get("trade_date") or "unknown")[:10]
        return self.root / ticker / day / f"{analyst}-{self._key(analyst, state)}.json"

    def _max_age(self, state: dict) -> float:
        try:
            day = date.fromisoformat(str(state.get("trade_date"))[:10])
        except ValueError:
            return self.hours * 3600
        return PAST_DAY_SECONDS if day < date.today() else self.hours * 3600

    # --- use ---------------------------------------------------------------------------

    def get(self, analyst: str, state: dict) -> tuple[str, float] | None:
        """(report, written at) when a fresh enough one is kept, else None."""
        path = self._path(analyst, state)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        report, written = data.get("report") or "", float(data.get("written") or 0)
        if not report.strip() or time.time() - written > self._max_age(state):
            return None
        return report, written

    def put(self, analyst: str, state: dict, report: str) -> None:
        if not (report or "").strip():
            return
        path = self._path(analyst, state)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"report": report, "written": time.time(), "analyst": analyst,
                                       "ticker": state.get("company_of_interest"),
                                       "trade_date": state.get("trade_date"), "model": self.model},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:            # reuse is an economy; a run never fails on it
            logger.info("could not keep the %s report: %s", analyst, exc)


def announce(analyst: str, written: float) -> None:
    """Say in the run's output that a kept report was used (the web job log shows it)."""
    when = datetime.fromtimestamp(written).strftime("%m-%d %H:%M")
    name = ANALYST_NAMES.get(analyst, analyst)
    logger.info("reusing the %s report written %s", analyst, when)
    if os.environ.get("TRADINGAGENTS_PLAIN_PROGRESS"):
        print(f"{datetime.now():%H:%M:%S} [复用] {name}：沿用 {when} 的报告，不再调用模型", flush=True)
