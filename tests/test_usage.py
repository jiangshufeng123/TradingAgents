"""Token usage and cost per role, the report's usage section and the cost ledger."""

import csv
from typing import TypedDict
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from cli.stats_handler import StatsCallbackHandler
from tradingagents.reporting import write_report_tree
from tradingagents.usage import (
    UsageRecorder,
    append_ledger,
    ledger_row,
    parse_prices,
    render_markdown,
    role_from_metadata,
    summarize,
)

CONFIG = {"deep_think_llm": "deep-m", "quick_think_llm": "quick-m",
          "model_prices": "deep-m=4/16, quick-m=1/2", "price_currency": "¥"}


def _record(recorder, role, model, tokens_in, tokens_out, cached=0):
    run = uuid4()
    recorder.start(run, {"langgraph_checkpoint_ns": f"{role}:x"}, {"model": model})
    usage = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    if cached:
        usage["input_token_details"] = {"cache_read": cached}
    recorder.end(run, usage)


@pytest.mark.unit
def test_a_nested_analyst_call_counts_toward_its_analyst():
    meta = {"langgraph_node": "agent",
            "langgraph_checkpoint_ns": "Market Analyst:3965|agent:6e4e"}
    assert role_from_metadata(meta) == "Market Analyst"
    assert role_from_metadata({"langgraph_node": "Trader", "langgraph_checkpoint_ns": "Trader:1c88"}) == "Trader"
    assert role_from_metadata({}) == "Other"


@pytest.mark.unit
def test_prices_parse_from_the_env_string():
    assert parse_prices("deep-m=4/16, quick-m=1/2/0.1") == {"deep-m": (4.0, 16.0, None),
                                                            "quick-m": (1.0, 2.0, 0.1)}
    assert parse_prices("") == {}
    with pytest.raises(ValueError):
        parse_prices("deep-m=4")


@pytest.mark.unit
def test_summary_costs_each_role_by_its_model_and_splits_the_tiers():
    rec = UsageRecorder()
    _record(rec, "Market Analyst", "quick-m", 1_000_000, 100_000)
    _record(rec, "Market Analyst", "quick-m", 1_000_000, 100_000)
    _record(rec, "Portfolio Manager", "deep-m", 500_000, 50_000, cached=200_000)

    s = summarize(rec, CONFIG)

    market = next(r for r in s["rows"] if r["role"] == "Market Analyst")
    assert market["calls"] == 2 and market["input_tokens"] == 2_000_000
    assert market["cost"] == pytest.approx(2 * 1 + 0.2 * 2)          # 2M in @1, 0.2M out @2
    assert s["tiers"]["deep"]["input_tokens"] == 500_000
    assert s["tiers"]["quick"]["output_tokens"] == 200_000
    assert s["cost"] == pytest.approx(2.4 + 0.5 * 4 + 0.05 * 16)   # no cache price: full input rate
    assert s["cost_complete"] and s["total"]["cached_input_tokens"] == 200_000
    # roles come out in the order the graph runs them
    assert [r["role"] for r in s["rows"]] == ["Market Analyst", "Portfolio Manager"]


@pytest.mark.unit
def test_without_prices_tokens_still_show_and_the_report_says_how_to_add_costs():
    rec = UsageRecorder()
    _record(rec, "Trader", "quick-m", 1000, 200)
    s = summarize(rec, {**CONFIG, "model_prices": None})
    assert s["cost"] is None and s["total"]["input_tokens"] == 1000
    text = render_markdown(s)
    assert "| Trader | quick-m | quick | 1 | 1,000 | 200 | — |" in text
    assert "TRADINGAGENTS_MODEL_PRICES" in text


@pytest.mark.unit
def test_an_unpriced_model_makes_the_total_a_lower_bound():
    rec = UsageRecorder()
    _record(rec, "Trader", "quick-m", 1000, 200)
    _record(rec, "Research Manager", "other-model", 1000, 200)
    s = summarize(rec, CONFIG)
    assert not s["cost_complete"]
    assert "lower bound" in render_markdown(s)


@pytest.mark.unit
def test_the_same_model_on_both_tiers_is_split_by_role():
    rec = UsageRecorder()
    _record(rec, "Trader", "same", 10, 1)
    _record(rec, "Research Manager", "same", 20, 2)
    s = summarize(rec, {"deep_think_llm": "same", "quick_think_llm": "same"})
    assert s["tiers"]["deep"]["input_tokens"] == 20 and s["tiers"]["quick"]["input_tokens"] == 10


@pytest.mark.unit
def test_the_stats_callback_attributes_calls_inside_a_real_graph():
    handler = StatsCallbackHandler()
    reply = AIMessage("ok", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
    llm = GenericFakeChatModel(messages=iter([reply] * 4), callbacks=[handler])

    class S(TypedDict):
        x: str

    def node(state):
        llm.invoke("hi")
        return {"x": "done"}

    inner = StateGraph(S)
    inner.add_node("agent", node)
    inner.add_edge(START, "agent")
    inner.add_edge("agent", END)
    outer = StateGraph(S)
    outer.add_node("Market Analyst", inner.compile())
    outer.add_node("Trader", node)
    outer.add_edge(START, "Market Analyst")
    outer.add_edge("Market Analyst", "Trader")
    outer.add_edge("Trader", END)
    outer.compile().invoke({"x": ""}, {"callbacks": [handler]})

    roles = {role: tally for role, _model, tally in handler.usage.rows()}
    assert set(roles) == {"Market Analyst", "Trader"}
    assert roles["Market Analyst"].calls == 1 and roles["Market Analyst"].input_tokens == 7
    assert handler.get_stats()["tokens_in"] == 14     # the running totals still work


@pytest.mark.unit
def test_the_report_gets_a_usage_section_and_file(tmp_path):
    state = {"market_report": "M", "final_trade_decision": "**Rating**: Hold"}
    out = write_report_tree(state, "NVDA", tmp_path, html=True, usage_markdown="| usage table |")
    assert (tmp_path / "6_usage" / "usage.md").read_text(encoding="utf-8") == "| usage table |"
    assert "## VI. Token Usage and Cost" in out.read_text(encoding="utf-8")
    assert "usage table" in (tmp_path / "complete_report.html").read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_ledger_appends_one_row_per_run_under_one_header(tmp_path):
    rec = UsageRecorder()
    _record(rec, "Market Analyst", "quick-m", 1000, 100)
    _record(rec, "Portfolio Manager", "deep-m", 500, 50)
    s = summarize(rec, CONFIG)
    path = tmp_path / "cost_ledger.csv"
    for date in ("2026-10-08", "2026-10-09"):
        append_ledger(path, ledger_row(s, run_time="t", ticker="NVDA", trade_date=date, rating="Hold",
                                       analysts=["market", "news"], config=CONFIG,
                                       elapsed_seconds=12.34, report_dir=tmp_path / date))

    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["trade_date"] for r in rows] == ["2026-10-08", "2026-10-09"]
    assert rows[0]["quick_input_tokens"] == "1000" and rows[0]["deep_output_tokens"] == "50"
    assert rows[0]["analysts"] == "market news" and rows[0]["currency"] == "¥"
    assert float(rows[0]["cost"]) == pytest.approx(s["cost"])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")         # Excel reads the Chinese text
    assert path.read_bytes().count(b"\xef\xbb\xbf") == 1


@pytest.mark.unit
def test_cache_hits_are_charged_at_the_cache_price():
    rec = UsageRecorder()
    _record(rec, "Bull Researcher", "quick-m", 1_000_000, 0, cached=800_000)
    s = summarize(rec, {**CONFIG, "model_prices": "quick-m=2/8/0.04"})
    assert s["cost"] == pytest.approx(0.2 * 2 + 0.8 * 0.04)
    assert "80% of input" in render_markdown(s)


@pytest.mark.unit
def test_deepseek_cache_hits_are_read_from_its_own_usage_field():
    rec = UsageRecorder()
    run = uuid4()
    rec.start(run, {"langgraph_checkpoint_ns": "Trader:x"}, {"model": "deepseek-flash"})
    rec.end(run, {"input_tokens": 1000, "output_tokens": 10},
            {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 640, "prompt_cache_miss_tokens": 360})
    assert rec.rows()[0][2].cached_input_tokens == 640
