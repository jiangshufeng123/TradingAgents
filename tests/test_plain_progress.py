"""Plain progress lines for background analyses, and how the service reads them."""

from __future__ import annotations

import io
from typing import TypedDict

import pytest

from cli.progress import PlainProgress, enabled
from server import runner


class _S(TypedDict, total=False):
    x: int


@pytest.mark.unit
def test_lines_for_each_main_step_and_tool(monkeypatch):
    from langchain_core.runnables import RunnableLambda
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph

    @tool
    def get_stock_data(symbol: str, start: str) -> str:
        """Prices."""
        return "ok"

    def market(state):
        get_stock_data.invoke({"symbol": "600519.SS", "start": "2026-07-15"})
        return {"x": 1}

    g = StateGraph(_S)
    g.add_node("Market Analyst", market)
    g.add_node("Trader", RunnableLambda(lambda s: {"x": 2}))
    g.add_node("helper", lambda s: {"x": 3})                  # not a main step: no line
    g.add_edge(START, "Market Analyst")
    g.add_edge("Market Analyst", "helper")
    g.add_edge("helper", "Trader")
    g.add_edge("Trader", END)
    out = io.StringIO()
    p = PlainProgress(["Market Analyst"], out=out)
    assert p.total == 9                                       # one analyst + the eight fixed steps
    p.new_run(2, 2)
    g.compile().invoke({"x": 0}, config={"callbacks": [p]})
    lines = [line.split(" ", 1)[1] for line in out.getvalue().strip().splitlines()]
    assert lines == ["[运行] 第 2/2 次运行",
                     "[进度 0/9] 第 2/2 次运行 市场分析师 开始",
                     "[工具] get_stock_data symbol: 600519.SS, start: 2026-07-15",
                     "[进度 1/9] 第 2/2 次运行 市场分析师 完成",
                     "[进度 1/9] 第 2/2 次运行 交易员 开始",
                     "[进度 2/9] 第 2/2 次运行 交易员 完成"]
    monkeypatch.setenv("TRADINGAGENTS_PLAIN_PROGRESS", "1")
    assert enabled()


@pytest.mark.unit
def test_the_service_reads_step_progress():
    log = ("$ tradingagents daily NVDA,600519.SS (runs 2)\n"
           "──── 1/2 NVDA 2026-10-15 ────\n"
           "20:01:00 [进度 12/12] 第 2/2 次运行 组合经理 完成\n"
           "──── 2/2 600519.SS 2026-10-15 ────\n"
           "20:15:01 [运行] 第 1/2 次运行\n"
           "20:15:02 [进度 0/12] 第 1/2 次运行 市场分析师 开始\n"
           "20:15:03 [工具] get_stock_data symbol: 600519.SS\n"
           "20:15:30 [进度 3/12] 第 1/2 次运行 基本面分析师 完成\n")
    p = runner.progress(log)
    assert (p["index"], p["total"], p["ticker"]) == (2, 2, "600519.SS")
    assert p["step"] == "基本面分析师 完成" and (p["step_done"], p["step_total"], p["run"], p["runs"]) == (3, 12, 1, 2)
    assert p["fraction"] == pytest.approx((1 + (0 + 3 / 12) / 2) / 2, abs=1e-4)
    assert p["last_line"].endswith("基本面分析师 完成")
    plain = runner.progress("──── 1/3 NVDA 2026-10-15 ────\n")
    assert plain["fraction"] == 0 and "step" not in plain                # an older log without steps


@pytest.mark.unit
def test_background_runs_get_unbuffered_plain_progress(tmp_path, monkeypatch):
    from server import db
    from server.services import users

    monkeypatch.setenv("TRADINGAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TRADINGAGENTS_DATABASE_URL", f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    db.init_db()
    with db.session() as s:
        user = users.create_user(s, "ian", "admin-pass-1", role="admin")
    env = runner.analysis_env({"reports_dir": str(tmp_path / "r"), "results_dir": str(tmp_path / "x")}, user)
    assert env["PYTHONUNBUFFERED"] == "1" and env["TRADINGAGENTS_PLAIN_PROGRESS"] == "1"


def test_without_an_output_stream_it_writes_to_standard_output(capsys):
    """The CLI makes it with no stream (as `daily` does): lines go to standard output."""
    from cli.progress import PlainProgress

    p = PlainProgress(["Market Analyst"])
    p._print("[运行] 开始分析")
    assert "[运行] 开始分析" in capsys.readouterr().out
