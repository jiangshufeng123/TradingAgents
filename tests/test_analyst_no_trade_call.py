"""Analysts report evidence and never carry a trade call downstream."""

import pytest

from tradingagents.agents.analysts.turn import strip_trade_call


@pytest.mark.unit
def test_the_rating_lines_an_analyst_appends_are_removed():
    report = (
        "# NVDA 技术分析报告\n\n趋势完好。\n\n| 维度 | 读数 |\n|---|---|\n| RSI | 53 |\n\n"
        "**综合判断**：中长期上升趋势完好。\n\n"
        "**Rating**: Hold\n\n"
        "FINAL TRANSACTION PROPOSAL: HOLD\n"
    )
    out = strip_trade_call(report)
    assert "Rating" not in out and "FINAL TRANSACTION PROPOSAL" not in out
    assert "趋势完好" in out and "| RSI | 53 |" in out and "**综合判断**" in out
    assert not out.endswith("\n\n\n")


@pytest.mark.unit
@pytest.mark.parametrize("line", [
    "**FINAL TRANSACTION PROPOSAL: HOLD**",
    "FINAL TRANSACTION PROPOSAL: **BUY**",
    "- **Rating**: Overweight",
    "## Rating: Sell",
    "rating：Hold",
])
def test_every_spelling_of_a_trade_call_line_is_removed(line):
    assert strip_trade_call(f"Body.\n\n{line}\n") == "Body.\n"


@pytest.mark.unit
def test_prose_that_mentions_ratings_is_kept():
    text = "Analysts' consensus rating has drifted lower since May.\nThe credit rating agency held."
    assert strip_trade_call(text) == text


@pytest.mark.unit
def test_an_empty_report_stays_empty():
    assert strip_trade_call("") == ""
