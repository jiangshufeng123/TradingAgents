"""The forward scoreboard and the unattended daily watchlist run."""

from __future__ import annotations

import csv

import pytest
from typer.testing import CliRunner

import cli.main as main
from tradingagents import scoreboard as sb
from tradingagents.memory import TradingMemoryLog

SEP = TradingMemoryLog._SEPARATOR


def _log(tmp_path, entries):
    path = tmp_path / "trading_memory.md"
    text = ""
    for date, ticker, rating, raw, alpha in entries:
        tag = (f"[{date} | {ticker} | {rating} | pending]" if raw is None
               else f"[{date} | {ticker} | {rating} | {raw} | {alpha} | 5d | resolved:2026-10-20]")
        text += f"{tag}\n\nDECISION:\n**Rating**: {rating}{SEP}"
    path.write_text(text, encoding="utf-8")
    return path


ENTRIES = [
    ("2026-10-01", "NVDA", "Buy", "+4.0%", "+2.0%"),        # right
    ("2026-10-02", "NVDA", "Overweight", "-1.0%", "-2.0%"),  # wrong
    ("2026-10-02", "600519.SS", "Underweight", "-3.0%", "-1.5%"),  # right
    ("2026-10-03", "600519.SS", "Hold", "+1.0%", "+0.2%"),   # no direction
    ("2026-10-14", "0700.HK", "Buy", None, None),           # pending
    ("2026-10-14", "0700.HK", "REVIEW", None, None),         # no rating
]


@pytest.mark.unit
def test_settled_decisions_are_scored_by_rating_ticker_and_overall(tmp_path):
    board = sb.build(_log(tmp_path, ENTRIES))
    assert len(board.rows) == 4 and board.pending == 1 and board.unscored == 1
    overall = board.overall()
    assert (overall.right, overall.directional) == (2, 3)
    assert board.by_rating()["Buy"].mean_alpha == pytest.approx(0.02)
    assert board.by_ticker()["600519.SS"].count == 2
    follow, hold = board.follow_vs_hold()
    assert follow == pytest.approx((0.04 - 0.01) / 4)          # long on Buy/Overweight only
    assert hold == pytest.approx((0.04 - 0.01 - 0.03 + 0.01) / 4)


@pytest.mark.unit
def test_the_scoreboard_is_written_as_markdown_and_csv(tmp_path):
    board = sb.build(_log(tmp_path, ENTRIES))
    md, path = sb.write(board, tmp_path / "out", "2026-10-21")
    text = md.read_text(encoding="utf-8")
    assert "67% (2/3)" in text and "| Underweight | 1 |" in text and "+0.75%" in text
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["ticker"] for r in rows] == ["NVDA", "600519.SS", "NVDA", "600519.SS"]
    assert rows[0]["direction_right"] == "yes" and rows[3]["direction_right"] == ""


@pytest.mark.unit
def test_an_empty_log_says_nothing_has_settled(tmp_path):
    text = sb.render_markdown(sb.build(tmp_path / "missing.md"))
    assert "No settled decisions yet" in text


# --- the CLI -------------------------------------------------------------------

UNATTENDED_ENV = {
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese", "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "1", "TRADINGAGENTS_LLM_PROVIDER": "deepseek",
    "TRADINGAGENTS_QUICK_THINK_LLM": "deepseek-flash",
}


@pytest.mark.unit
def test_daily_runs_every_watchlist_ticker_and_survives_a_failure(monkeypatch):
    for k, v in UNATTENDED_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setitem(main.DEFAULT_CONFIG, "watchlist", "NVDA, 600519.SS,0700.HK")
    seen = []

    def fake_run(portfolio=None, flags=None, **k):
        seen.append(flags)
        if flags["ticker"] == "600519.SS":
            raise RuntimeError("provider timeout")

    monkeypatch.setattr(main, "run_analysis", fake_run)
    result = CliRunner().invoke(main.app, ["daily", "--date", "2026-10-15"])
    assert result.exit_code == 0, result.output
    assert [f["ticker"] for f in seen] == ["NVDA", "600519.SS", "0700.HK"]
    assert all(f["save"] is True and f["show"] is False and f["date"] == "2026-10-15" for f in seen)
    assert seen[0]["analysts"] == "market,sentiment,news,fundamentals"
    assert "600519.SS: RuntimeError" in result.output and "Analyzed 2/3" in result.output


@pytest.mark.unit
def test_daily_refuses_to_start_when_a_question_would_need_answering(monkeypatch):
    for k in UNATTENDED_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(main, "run_analysis", lambda **k: pytest.fail("started without settings"))
    result = CliRunner().invoke(main.app, ["daily", "NVDA"])
    assert result.exit_code == 1 and "TRADINGAGENTS_LLM_PROVIDER" in result.output


@pytest.mark.unit
def test_scoreboard_command_reads_and_saves_without_settling(monkeypatch, tmp_path):
    log = _log(tmp_path, ENTRIES)
    monkeypatch.setitem(main.DEFAULT_CONFIG, "memory_log_path", str(log))
    monkeypatch.setitem(main.DEFAULT_CONFIG, "reports_dir", str(tmp_path / "reports"))
    result = CliRunner().invoke(main.app, ["scoreboard", "--no-settle"])
    assert result.exit_code == 0, result.output
    assert "Direction right" in result.output
    assert (tmp_path / "reports" / "scoreboard.csv").exists()
