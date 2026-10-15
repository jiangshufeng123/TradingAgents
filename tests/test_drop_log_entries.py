"""scripts/drop_log_entries.py removes a day's pending decisions so it can be re-run."""

import importlib.util
from pathlib import Path

import pytest

from tradingagents.memory.log import TradingMemoryLog

_spec = importlib.util.spec_from_file_location(
    "drop_log_entries", Path(__file__).resolve().parents[1] / "scripts" / "drop_log_entries.py")
drop_log_entries = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drop_log_entries)


@pytest.mark.unit
def test_pending_entries_of_the_day_are_removed_and_the_rest_kept(tmp_path):
    path = tmp_path / "log.md"
    log = TradingMemoryLog({"memory_log_path": str(path)})
    log.store_decision("NVDA", "2026-10-14", "**Rating**: Buy")
    log.store_decision("NVDA", "2026-10-15", "**Rating**: Hold")
    log.store_decision("6616.HK", "2026-10-15", "**Rating**: Overweight")

    assert drop_log_entries.main(["--date", "2026-10-15", "--ticker", "6616.HK", "--log", str(path)]) == 0
    text = path.read_text(encoding="utf-8")
    assert "6616.HK" not in text and "[2026-10-15 | NVDA | Hold | pending]" in text
    assert (tmp_path / "log.md.bak").exists()

    drop_log_entries.main(["--date", "2026-10-15", "--log", str(path)])
    entries = TradingMemoryLog({"memory_log_path": str(path)}).load_entries()
    assert [(e["ticker"], e["date"]) for e in entries] == [("NVDA", "2026-10-14")]

    # the day can now be recorded again
    log.store_decision("6616.HK", "2026-10-15", "**Rating**: Hold")
    assert "[2026-10-15 | 6616.HK | Hold | pending]" in path.read_text(encoding="utf-8")
