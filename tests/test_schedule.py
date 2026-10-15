"""`tradingagents schedule` wraps Windows Task Scheduler; `daily` can add screen picks."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import cli.main as cli_main


@pytest.fixture
def schtasks(monkeypatch):
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "/Query":
            return 0, "TaskName: \\TradingAgents Daily\nNext Run Time: 2026/10/15 18:30:00\nLast Result: 0\nAuthor: x\n"
        return 0, "SUCCESS"

    monkeypatch.setattr(cli_main, "_on_windows", lambda: True)
    monkeypatch.setattr(cli_main, "_schtasks", fake)
    return calls


@pytest.mark.unit
def test_install_creates_the_weekday_task(schtasks):
    result = CliRunner().invoke(cli_main.app, ["schedule", "install", "--time", "18:45"])
    assert result.exit_code == 0, result.output
    args = schtasks[0]
    assert args[:4] == ("/Create", "/F", "/TN", "TradingAgents Daily")
    assert "MON,TUE,WED,THU,FRI" in args and "18:45" in args
    assert args[-1].endswith('run_daily.ps1"') and "-ExecutionPolicy Bypass" in args[-1]


@pytest.mark.unit
def test_list_run_remove(schtasks):
    runner = CliRunner()
    out = runner.invoke(cli_main.app, ["schedule", "list"]).output
    assert "Next Run Time" in out and "Author" not in out
    assert runner.invoke(cli_main.app, ["schedule", "run"]).exit_code == 0
    assert runner.invoke(cli_main.app, ["schedule", "remove"]).exit_code == 0
    assert [c[0] for c in schtasks] == ["/Query", "/Run", "/Delete"]


@pytest.mark.unit
def test_off_windows_install_prints_a_cron_line(monkeypatch):
    monkeypatch.setattr(cli_main, "_on_windows", lambda: False)
    out = CliRunner().invoke(cli_main.app, ["schedule", "install"]).output
    assert "crontab" in out and "30 18" in out


@pytest.mark.unit
def test_the_daily_screen_never_adds_to_what_is_analyzed(monkeypatch, tmp_path):
    ran, screened = [], []
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "daily_screen", "低估值大盘股")
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "watchlist", "NVDA")
    monkeypatch.setitem(cli_main.DEFAULT_CONFIG, "watchlist_file", str(tmp_path / "w.txt"))
    monkeypatch.setattr(cli_main, "_screen_picks", lambda request, top: screened.append(request) or ["600519.SS"])
    monkeypatch.setattr(cli_main, "run_analysis", lambda portfolio=None, flags=None: ran.append(flags["ticker"]) or {})
    monkeypatch.setattr(cli_main, "unattended_gaps", lambda flags: [])
    result = CliRunner().invoke(cli_main.app, ["daily"])
    assert result.exit_code == 0, result.output
    assert screened == ["低估值大盘股"] and ran == ["NVDA"]
