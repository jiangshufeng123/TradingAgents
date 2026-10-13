"""Several runs of one analysis and the rating they agree on."""

from __future__ import annotations

import csv

import pytest

import cli.run as cli_run
from tests.test_cli_memory_log import _FakeBuffer, _NullLive
from tradingagents.usage import LEDGER_FIELDS, append_ledger
from tradingagents.vote import agreement_label, consensus, render_markdown


@pytest.mark.unit
def test_the_majority_rating_wins_and_its_first_run_is_reported():
    vote = consensus(["Hold", "Buy", "Hold", "Underweight", "Hold"])
    assert vote["rating"] == "Hold" and vote["representative"] == 0
    assert vote["agreement"] == pytest.approx(0.6) and vote["spread"] == 3
    assert vote["counts"] == {"Buy": 1, "Hold": 3, "Underweight": 1}


@pytest.mark.unit
def test_a_tie_goes_to_the_rating_nearest_the_average_then_the_cautious_one():
    # Buy and Hold tie 2-2; the average (+1.0) is as near to each, so the cautious Hold wins.
    assert consensus(["Buy", "Buy", "Hold", "Hold", "Overweight"])["rating"] == "Hold"
    # Overweight and Sell tie 2-2; the average (0.0) is nearer Overweight.
    assert consensus(["Overweight", "Overweight", "Sell", "Sell", "Buy"])["rating"] == "Overweight"


@pytest.mark.unit
def test_runs_without_a_rating_do_not_vote():
    vote = consensus(["REVIEW", "Sell", "Sell"])
    assert vote["rating"] == "Sell" and vote["representative"] == 1 and vote["valid"] == 2
    assert consensus(["REVIEW"])["rating"] is None
    assert agreement_label(consensus(["REVIEW"])) == "no usable rating"


@pytest.mark.unit
def test_agreement_is_described():
    assert agreement_label(consensus(["Buy"] * 3)) == "unanimous"
    assert agreement_label(consensus(["Buy", "Buy", "Overweight"])) == "clear majority"
    assert "opposite" in agreement_label(consensus(["Buy", "Buy", "Underweight", "Sell", "Hold"]))


@pytest.mark.unit
def test_the_vote_section_lists_every_run():
    text = render_markdown(consensus(["Hold", "Buy", "Hold"]))
    assert "**Hold**" in text and "| 2 | Buy |" in text and "runs/" in text


@pytest.mark.unit
def test_an_older_ledger_is_upgraded_to_the_new_columns(tmp_path):
    path = tmp_path / "cost_ledger.csv"
    old = [f for f in LEDGER_FIELDS if not f.startswith("vote") and f != "agreement"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=old)
        w.writeheader()
        w.writerow({k: "x" for k in old})
    append_ledger(path, {"ticker": "NVDA", "vote_runs": 3})
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert tuple(rows[0]) == LEDGER_FIELDS
    assert rows[0]["ticker"] == "x" and rows[0]["vote_runs"] == ""
    assert rows[1]["vote_runs"] == "3"


# --- the CLI flow ------------------------------------------------------------------

class _VotingGraph:
    """Run 1 streams Buy; the extra runs come back Hold and Hold."""

    def __init__(self, extra):
        self.calls, self.graph, self.propagator = [], self, self
        self._resuming, self.extra = False, list(extra)
        self.config = {}

    def create_run_state(self, *a, **k):
        return {"messages": []}

    def get_graph_args(self, callbacks=None):
        return {}

    def begin_checkpoint(self, *a, **k):
        return None

    def checkpoint_input(self, state):
        return state

    def clear_checkpoint_on_success(self, *a, **k):
        pass

    def end_checkpoint(self):
        pass

    def stream_run(self, graph_input, **kwargs):
        yield [], {"messages": [], "final_trade_decision": "**Rating**: Buy", "final_rating": "Buy"}

    def invoke(self, state, **kwargs):
        rating = self.extra.pop(0)
        if rating == "boom":
            raise RuntimeError("provider timeout")
        return {"final_trade_decision": f"**Rating**: {rating}", "final_rating": rating}

    def record_decision(self, ticker, trade_date, final_state):
        self.calls.append(("record_decision", final_state["final_rating"]))


def _run(monkeypatch, tmp_path, graph, runs):
    import cli.main as m
    from cli.models import AnalystType

    offered = {}
    monkeypatch.setattr(cli_run, "TradingAgentsGraph", lambda *a, **k: graph)
    monkeypatch.setattr(cli_run, "message_buffer", _FakeBuffer())
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "Live", _NullLive)
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "NVDA", "analysis_date": "2026-10-14",
        "analysts": [AnalystType.MARKET], "asset_type": "stock",
    })
    monkeypatch.setattr(cli_run, "_build_run_config", lambda selections, checkpoint: {
        "data_cache_dir": str(tmp_path / "cache"), "results_dir": str(tmp_path / "results"),
        "vote_runs": runs,
    })

    def offer(final_state, graph, ticker, **kwargs):
        offered.update(state=final_state, **kwargs)
        return None

    monkeypatch.setattr(cli_run, "_offer_reports", offer)
    monkeypatch.setattr(m.typer, "prompt", lambda *a, **k: "N")
    cli_run.run_analysis()
    return offered


@pytest.mark.unit
def test_three_runs_vote_and_the_consensus_is_what_gets_logged_and_reported(tmp_path, monkeypatch):
    graph = _VotingGraph(["Hold", "Hold"])
    offered = _run(monkeypatch, tmp_path, graph, 3)

    assert graph.calls == [("record_decision", "Hold")]          # once, the consensus
    assert offered["state"]["final_rating"] == "Hold"
    assert "| 1 | Buy |" in offered["vote_markdown"] and "**Hold**" in offered["vote_markdown"]
    with open(tmp_path / "results" / "reports" / "cost_ledger.csv", encoding="utf-8-sig") as f:
        row = list(csv.DictReader(f))[0]
    assert row["vote_runs"] == "3" and row["vote_ratings"] == "Buy Hold Hold"


@pytest.mark.unit
def test_a_failed_extra_run_is_left_out_of_the_vote(tmp_path, monkeypatch):
    graph = _VotingGraph(["boom", "Buy"])
    offered = _run(monkeypatch, tmp_path, graph, 3)
    assert graph.calls == [("record_decision", "Buy")]
    assert "| 3 |" not in offered["vote_markdown"]


@pytest.mark.unit
def test_one_run_behaves_as_before(tmp_path, monkeypatch):
    graph = _VotingGraph([])
    offered = _run(monkeypatch, tmp_path, graph, 1)
    assert graph.calls == [("record_decision", "Buy")]
    assert offered["vote_markdown"] is None
