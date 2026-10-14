"""Report questions and the analyst choice answered from the config (.env)."""

import pytest
import typer

import cli.run as run


@pytest.mark.unit
def test_config_answers_the_questions_a_flag_left_open():
    flags = {"save": None, "show": None, "html": None, "analysts": None}
    config = {"save_report": "true", "show_report": "false", "html_report": "yes",
              "open_report": None, "default_analysts": "market,news"}
    merged = run.flags_with_config_defaults(flags, config)
    assert merged["save"] is True and merged["show"] is False and merged["html"] is True
    assert merged["open"] is None and merged["analysts"] == "market,news"


@pytest.mark.unit
def test_a_command_line_flag_still_wins():
    merged = run.flags_with_config_defaults({"save": False, "analysts": "market"},
                                            {"save_report": "true", "default_analysts": "market,news"})
    assert merged["save"] is False and merged["analysts"] == "market"


@pytest.mark.unit
def test_nothing_set_means_the_questions_are_asked():
    merged = run.flags_with_config_defaults({}, {})
    assert merged["save"] is None and merged["show"] is None and merged.get("analysts") is None


@pytest.mark.unit
def test_a_bad_answer_is_reported():
    with pytest.raises(typer.BadParameter):
        run.flags_with_config_defaults({}, {"save_report": "sometimes"})


@pytest.mark.unit
def test_open_report_opens_the_page_without_asking(monkeypatch, tmp_path):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.run_settings = lambda: {}
    opened = []

    class _Browser:
        def open(self, url):
            opened.append(url)
            return True

    monkeypatch.setattr(run, "_graphical_browser", lambda: _Browser())
    monkeypatch.setattr(run.typer, "prompt", lambda *a, **k: pytest.fail("asked a question"))
    monkeypatch.setattr(run, "display_complete_report", lambda s: None)
    run._offer_reports({"market_report": "M", "final_trade_decision": "**Rating**: Hold"}, graph, "NVDA",
                       save=True, show=False, html=True, open_page=True)
    assert len(opened) == 1 and opened[0].endswith("complete_report.html")
