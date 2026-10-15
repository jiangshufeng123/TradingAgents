"""Plain one-line progress for runs without a terminal (the web service's analysis jobs).

The live terminal display shows each agent and tool as the analysis goes; a run
whose output goes to a file has no such display, so this callback prints a line
when an agent starts or finishes and when a tool is called, e.g.

    20:15:03 [进度 3/12] 市场分析师 完成
    20:15:04 [工具] get_stock_data 600519.SS 2026-07-15 2026-10-15

Turned on by TRADINGAGENTS_PLAIN_PROGRESS=1 (the web service sets it). The
``[进度 k/m]`` count is the number of main steps finished in this run, which the
service reads to move its progress bar.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

STEPS = {
    "Market Analyst": "市场分析师", "Sentiment Analyst": "舆情分析师", "Social Analyst": "舆情分析师",
    "News Analyst": "新闻分析师", "Fundamentals Analyst": "基本面分析师",
    "Bull Researcher": "多头研究员", "Bear Researcher": "空头研究员", "Style Panel": "风格研究员",
    "Research Manager": "研究经理", "Trader": "交易员",
    "Aggressive Analyst": "激进派风控", "Conservative Analyst": "保守派风控", "Neutral Analyst": "中立派风控",
    "Portfolio Manager": "组合经理", "Memory Log": "历史决策复盘",
}
# Steps counted toward k/m: the selected analysts plus these, each counted once per run.
FIXED = ("Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
         "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager")


def enabled() -> bool:
    import os

    return os.environ.get("TRADINGAGENTS_PLAIN_PROGRESS", "").strip().lower() in ("1", "true", "yes")


class PlainProgress(BaseCallbackHandler):
    """Prints a line per agent start/finish and per tool call; thread-safe (analysts run in parallel)."""

    def __init__(self, analyst_nodes: list[str], out=None):
        super().__init__()
        self.counted = set(analyst_nodes) | set(FIXED)
        self.total = len(self.counted)
        self.out = out
        self._lock = threading.Lock()
        self._runs: dict[Any, str] = {}
        self.done: set[str] = set()
        self.run_label = ""

    def new_run(self, n: int, total: int) -> None:
        """Start counting again for vote run ``n`` of ``total``."""
        with self._lock:
            self.done = set()
            self.run_label = f"第 {n}/{total} 次运行 " if total > 1 else ""
        self._print(f"[运行] {self.run_label.strip() or '开始分析'}")

    def _print(self, text: str) -> None:
        # One write per line: print() writes the text and the newline separately, so lines
        # from analysts finishing at the same moment could run together.
        self.out.write(f"{datetime.now():%H:%M:%S} {text}\n")
        self.out.flush()

    @staticmethod
    def _node(kwargs: dict, serialized) -> str | None:
        metadata = kwargs.get("metadata") or {}
        node = metadata.get("langgraph_node")
        name = kwargs.get("name") or (serialized or {}).get("name")
        # Only the node itself, not the prompts and models running inside it.
        return node if node in STEPS and name == node else None

    def on_chain_start(self, serialized, inputs, *, run_id=None, **kwargs: Any) -> None:
        node = self._node(kwargs, serialized)
        if node is None:
            return
        with self._lock:
            self._runs[run_id] = node
            done = len(self.done & self.counted)
        self._print(f"[进度 {done}/{self.total}] {self.run_label}{STEPS[node]} 开始")

    def on_chain_end(self, outputs, *, run_id=None, **kwargs: Any) -> None:
        with self._lock:
            node = self._runs.pop(run_id, None)
            if node is None:
                return
            self.done.add(node)
            done = len(self.done & self.counted)
        self._print(f"[进度 {done}/{self.total}] {self.run_label}{STEPS[node]} 完成")

    def on_chain_error(self, error, *, run_id=None, **kwargs: Any) -> None:
        with self._lock:
            node = self._runs.pop(run_id, None)
        if node is not None:
            self._print(f"[出错] {self.run_label}{STEPS[node]}: {type(error).__name__}: {str(error)[:160]}")

    def on_tool_start(self, serialized, input_str, **kwargs: Any) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        args = " ".join(str(input_str).replace("\n", " ").split())
        for ch in "{}'\"":
            args = args.replace(ch, "")
        self._print(f"[工具] {name} {args[:100]}")
