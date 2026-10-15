"""Learning from settled decisions: track record and revised beliefs (tradingagents/memory/learning.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tradingagents.memory import TradingMemoryLog, learning


def _settle(log, ticker, date, rating, raw, alpha, resolved, reflection="lesson"):
    log.store_decision(ticker, date, f"**Rating**: {rating}")
    log.update_with_outcome(ticker, date, raw, alpha, 5, reflection, resolution_date=resolved)


@pytest.fixture
def log(tmp_path):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory" / "trading_memory.md")})
    _settle(log, "NVDA", "2026-09-01", "Buy", 0.03, 0.02, "2026-09-08", "momentum confirmed")
    _settle(log, "NVDA", "2026-09-02", "Buy", -0.02, -0.03, "2026-09-09", "chased a gap")
    _settle(log, "603980.SS", "2026-09-03", "Underweight", -0.05, -0.04, "2026-09-10")
    _settle(log, "603980.SS", "2026-09-04", "Hold", 0.01, 0.0, "2026-09-11")
    _settle(log, "0700.HK", "2026-09-05", "Overweight", 0.02, 0.01, "2026-09-20")
    log.store_decision("NVDA", "2026-10-14", "**Rating**: Buy")          # still pending
    return log


@pytest.mark.unit
def test_track_record_by_rating_and_ticker(log):
    text = learning.track_record(log.load_entries(), "NVDA")
    assert "5 settled decision(s)" in text
    assert "All calls: 3/4 right (75%)" in text
    assert "- Buy: 2 call(s), 1/2 right (50%)" in text
    assert "On NVDA: 1/2 right" in text and "2026-09-02 Buy -3.0%" in text
    assert "sample is small" in text


@pytest.mark.unit
def test_track_record_is_cut_at_the_point_in_time(log):
    text = learning.track_record(log.load_entries(), "NVDA", as_of="2026-09-10")
    assert "3 settled decision(s)" in text and "Overweight" not in text
    assert learning.track_record(log.load_entries(), "NVDA", as_of="2026-09-01") == ""


@pytest.mark.unit
def test_a_poor_bullish_record_asks_for_stronger_evidence():
    entries = [{"date": f"2026-08-{i:02d}", "ticker": "X", "rating": "Buy", "pending": False,
                "raw": "-1.0%", "alpha": "-1.0%" if i % 3 else "+1.0%", "resolved": "2026-09-30"}
               for i in range(1, 25)]
    assert "bullish calls have been wrong" in learning.track_record(entries, "X")


class _LLM:
    def __init__(self, text):
        self.text, self.prompts = text, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.text)


@pytest.mark.unit
def test_beliefs_are_revised_every_n_settlements_and_versioned(log):
    config = {"memory_log_path": str(log._log_path), "belief_update_every": 5}
    llm = _LLM("Here you go:\n- Do not chase gaps (evidence: 1 right / 2 wrong)\n- Weigh fund flow (evidence: 2 right / 0 wrong)")
    version = learning.update_beliefs(log, llm, config)
    assert version["based_on"] == 5 and version["as_of"] == "2026-09-20"
    assert version["beliefs"].count("\n") == 1 and "Here you go" not in version["beliefs"]
    prompt = llm.prompts[0]
    assert "RIGHT" in prompt and "WRONG" in prompt and "chased a gap" in prompt
    # nothing new settled: no second call
    assert learning.update_beliefs(log, llm, config) is None and len(llm.prompts) == 1
    stored = json.loads(learning.beliefs_path(config).read_text(encoding="utf-8"))
    assert len(stored) == 1 and len(stored[0]["seen"]) == 5


@pytest.mark.unit
def test_a_historical_run_reads_only_beliefs_that_existed_then(log):
    config = {"memory_log_path": str(log._log_path)}
    learning.update_beliefs(log, _LLM("- rule A (evidence: 1 right / 0 wrong)"), config)
    assert learning.beliefs_as_of(config, "2026-09-19") is None
    assert "rule A" in learning.render_beliefs(learning.beliefs_as_of(config, "2026-09-20"))
    context = learning.learning_context(log, config, "NVDA", as_of=None)
    assert "track record" in context and "rule A" in context


@pytest.mark.unit
def test_too_few_settlements_or_an_empty_answer_change_nothing(log):
    config = {"memory_log_path": str(log._log_path), "belief_update_every": 10}
    assert learning.update_beliefs(log, _LLM("- x"), config) is None
    config["belief_update_every"] = 5
    assert learning.update_beliefs(log, _LLM("no rules today"), config) is None
    assert learning.beliefs_as_of(config) is None


@pytest.mark.unit
def test_the_managers_read_the_learning_context():
    from tradingagents.agents.managers.research_manager import create_research_manager

    seen = []

    class Structured:
        def invoke(self, prompt):
            seen.append(prompt if isinstance(prompt, str) else str(prompt))
            raise ValueError("no structured output")

    class Fake:
        def with_structured_output(self, *a, **k):
            return Structured()

        def invoke(self, prompt):
            seen.append(prompt if isinstance(prompt, str) else str(prompt))
            return SimpleNamespace(content="**Recommendation**: Hold\n\nok")

    node = create_research_manager(Fake())
    state = {"company_of_interest": "NVDA", "learning_context": "Your track record (3 settled)",
             "investment_debate_state": {"history": "h", "bull_history": "", "bear_history": "", "count": 1}}
    node(state)
    assert any("Your track record (3 settled)" in p for p in seen)


# --- style panel -----------------------------------------------------------------

@pytest.mark.unit
def test_styles_default_and_settings():
    from tradingagents.agents.researchers.style_panel import styles_for

    assert styles_for("NVDA", None) == ["value", "growth", "momentum"]
    assert styles_for("603980.SH", None) == ["value", "growth", "momentum", "policy"]
    assert styles_for("0700.HK", "value, momentum, astrology") == ["value", "momentum"]
    assert styles_for("NVDA", "off") == []


@pytest.mark.unit
def test_the_panel_asks_each_style_and_labels_the_views():
    from tradingagents.agents.researchers.style_panel import create_style_panel

    asked = []

    class LLM:
        def invoke(self, prompt):
            asked.append(prompt)
            return SimpleNamespace(content="Stance: Bullish\nConviction: 4")

    node = create_style_panel(LLM(), "value,growth")
    out = node({"company_of_interest": "NVDA", "market_report": "m", "sentiment_report": "",
                "news_report": "n", "fundamentals_report": "f"})
    assert len(asked) == 2 and any("Graham" in p for p in asked) and any("Fisher" in p for p in asked)
    assert "### Value investor" in out["style_views"] and "### Growth investor" in out["style_views"]
    assert create_style_panel(LLM(), "off")({"company_of_interest": "NVDA"}) == {"style_views": ""}


@pytest.mark.unit
def test_the_graph_places_the_panel_before_the_research_manager():
    from tradingagents.graph.conditional_logic import ConditionalLogic
    from tradingagents.graph.setup import GraphSetup

    setup = GraphSetup(object(), object(), ConditionalLogic(), 3)
    with_panel = setup.setup_graph(["market"]).compile().get_graph()
    assert "Style Panel" in with_panel.nodes
    assert any(e.source == "Style Panel" and e.target == "Research Manager" for e in with_panel.edges)
    without = setup.setup_graph(["market"], research_styles="off").compile().get_graph()
    assert "Style Panel" not in without.nodes
