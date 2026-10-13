"""Every report-producing agent must apply the configured output language
(#740/#801).

A non-English run should produce a fully localized report, not a mix of
languages. The bug originally happened because several agents silently omitted
the instruction (fixed in 6b384f7); this test codifies the invariant so a future
refactor can't quietly drop it again.
"""
from pathlib import Path

import pytest

from tradingagents.agents.context import get_language_instruction

_AGENTS_DIR = Path(__file__).resolve().parents[1] / "tradingagents" / "agents"

# Every node whose text reaches the saved report. If you add a report-producing
# agent, add it here — and make it call get_language_instruction().
REPORT_AGENTS = [
    "analysts/market_analyst.py",
    "analysts/news_analyst.py",
    "analysts/fundamentals_analyst.py",
    "analysts/sentiment_analyst.py",
    "researchers/bull_researcher.py",
    "researchers/bear_researcher.py",
    "managers/research_manager.py",
    "managers/portfolio_manager.py",
    "risk_mgmt/aggressive_debator.py",
    "risk_mgmt/conservative_debator.py",
    "risk_mgmt/neutral_debator.py",
    "trader/trader.py",
]


@pytest.mark.unit
class TestLanguageInstruction:
    def test_english_adds_no_tokens(self, monkeypatch):
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "English"})
        assert get_language_instruction() == ""

    def test_non_english_emits_directive(self):
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "中文"})
        out = get_language_instruction()
        assert "中文" in out
        assert "entire response" in out

    def test_the_labelled_lines_stay_as_the_format_gives_them(self):
        # The rating is read from its "**Rating**:" line; a translated label
        # leaves the reader a translated negation ("不建议卖出 (Sell)") to find
        # instead, and a Buy reads as Sell (#1435).
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "中文"})
        out = get_language_instruction(labelled_lines=True)
        assert "**Rating**:" in out and "FINAL TRANSACTION PROPOSAL" in out

    def test_agents_that_make_no_call_are_not_shown_the_rating_lines(self):
        # Naming the labels to an analyst reads as asking for them: it then
        # ends its report with its own rating, which downstream agents cite.
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "中文"})
        out = get_language_instruction()
        assert "中文" in out
        assert "Rating" not in out and "FINAL TRANSACTION PROPOSAL" not in out


@pytest.mark.unit
@pytest.mark.parametrize("rel", REPORT_AGENTS)
def test_report_agent_applies_language_instruction(rel):
    path = _AGENTS_DIR / rel
    assert path.exists(), f"missing agent module: {rel}"
    src = path.read_text(encoding="utf-8")
    assert "get_language_instruction(" in src, (
        f"{rel} does not apply get_language_instruction(); its output would "
        f"ignore the configured output_language (#740/#801)."
    )


DECIDING_AGENTS = ["managers/research_manager.py", "managers/portfolio_manager.py", "trader/trader.py"]


@pytest.mark.unit
@pytest.mark.parametrize("rel", REPORT_AGENTS)
def test_only_the_deciding_agents_get_the_labelled_line_instruction(rel):
    src = (_AGENTS_DIR / rel).read_text(encoding="utf-8")
    asks_for_labels = "get_language_instruction(labelled_lines=True)" in src
    assert asks_for_labels == (rel in DECIDING_AGENTS)
