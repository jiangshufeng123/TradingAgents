"""The watchlist file: added to only by the user's pick."""

from __future__ import annotations

import pandas as pd
import pytest
from typer.testing import CliRunner

import cli.main as cli_main
from tradingagents import watchlist as wl


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "watchlist", "NVDA,603980.SH")
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "watchlist_file", str(tmp_path / "watchlist.txt"))
    return cli_main.DEFAULT_CONFIG


@pytest.mark.unit
def test_add_remove_and_merge_with_env(config):
    assert wl.add(config, ["600519.SS", "603980.SS", "NVDA", "000333.SZ"], "screen: 白酒") == ["600519.SS", "000333.SZ"]
    assert wl.tickers(config) == ["NVDA", "603980.SH", "600519.SS", "000333.SZ"]
    assert "screen: 白酒" in wl.path(config).read_text(encoding="utf-8")
    assert wl.remove(config, ["600519.ss", "NVDA"]) == ["600519.SS"]       # .env names stay
    assert wl.tickers(config) == ["NVDA", "603980.SH", "000333.SZ"]


@pytest.mark.unit
def test_parse_picks():
    assert wl.parse_picks("1,3", 5) == [0, 2]
    assert wl.parse_picks("2-4，1", 5) == [1, 2, 3, 0]
    assert wl.parse_picks("all", 3) == [0, 1, 2]
    assert wl.parse_picks("", 3) == [] and wl.parse_picks("n", 3) == []
    with pytest.raises(ValueError):
        wl.parse_picks("6", 5)


RESULT = pd.DataFrame({"ticker": ["600519.SS", "000333.SZ", "000651.SZ"], "name": ["贵州茅台", "美的集团", "格力电器"],
                       "industry": ["白酒Ⅱ", "白色家电", "白色家电"], "pe": [19.9, 14.4, 7.6],
                       "roe_annualized": [33.5, 22.7, 17.4]})


@pytest.mark.unit
def test_only_the_rows_picked_are_added(config, monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)   # unattended: no "analyze now?" question
    added = cli_main._pick_into_watchlist(RESULT, note="screen: x", rows="1,3")
    assert added == ["600519.SS", "000651.SZ"]
    assert [t for t, _ in wl.entries(config)] == ["600519.SS", "000651.SZ"]


@pytest.mark.unit
def test_without_a_terminal_or_rows_nothing_is_added(config, monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli_main._pick_into_watchlist(RESULT, note="x") == []
    assert wl.entries(config) == []


@pytest.mark.unit
def test_daily_analyzes_env_plus_file(config, monkeypatch):
    wl.add(config, ["600519.SS"])
    ran = []
    monkeypatch.setattr(cli_main, "run_analysis", lambda portfolio=None, flags=None: ran.append(flags["ticker"]) or {})
    monkeypatch.setattr(cli_main, "unattended_gaps", lambda flags: [])
    assert CliRunner().invoke(cli_main.app, ["daily"]).exit_code == 0
    assert ran == ["NVDA", "603980.SH", "600519.SS"]


@pytest.mark.unit
def test_watchlist_commands_and_pick_from_a_saved_screen(config, tmp_path, monkeypatch):
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "reports_dir", str(tmp_path / "reports"))
    (tmp_path / "reports" / "screens").mkdir(parents=True)
    RESULT.to_csv(tmp_path / "reports" / "screens" / "screen_20261015_130238.csv", index=False, encoding="utf-8-sig")
    runner = CliRunner()
    out = runner.invoke(cli_main.app, ["watchlist", "pick", "--add", "2"])
    assert out.exit_code == 0, out.output
    assert [t for t, _ in wl.entries(config)] == ["000333.SZ"]
    assert runner.invoke(cli_main.app, ["watchlist", "add", "0700.HK"]).exit_code == 0
    listed = runner.invoke(cli_main.app, ["watchlist", "list"]).output
    assert "NVDA" in listed and "000333.SZ" in listed and "0700.HK" in listed
    assert runner.invoke(cli_main.app, ["watchlist", "remove", "0700.HK"]).exit_code == 0
    assert [t for t, _ in wl.entries(config)] == ["000333.SZ"]


@pytest.mark.unit
def test_the_checkbox_adds_what_is_ticked_and_falls_back_to_typing(config, monkeypatch):
    import questionary

    class Box:
        def __init__(self, picked):
            self.picked = picked

        def ask(self):
            return self.picked

    monkeypatch.setattr(questionary, "checkbox", lambda *a, **k: Box([1]))
    monkeypatch.setattr(cli_main.typer, "confirm", lambda *a, **k: False)
    assert cli_main._pick_into_watchlist(RESULT, note="x") == ["000333.SZ"]

    def broken(*a, **k):
        raise RuntimeError("no console")

    monkeypatch.setattr(questionary, "checkbox", broken)
    answers = iter(["9", "3"])                         # a bad row number is asked again
    monkeypatch.setattr(cli_main.typer, "prompt", lambda *a, **k: next(answers))
    assert cli_main._pick_into_watchlist(RESULT, note="x") == ["000651.SZ"]
    assert [t for t, _ in wl.entries(config)] == ["000333.SZ", "000651.SZ"]
