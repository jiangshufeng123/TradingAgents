"""Quick reviews of a screen's leaders: one quick-model call each, a fixed answer shape."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tradingagents import review

NEWS = """## 600519.SS news (East Money), 2026-09-25 to 2026-10-16

### 贵州茅台：三季度营收同比增长 9%
2026-10-15 18:00 · 证券时报

公司公告三季度营业收入同比增长百分之九，净利润增长百分之八……

### 控股股东增持计划实施完毕
2026-10-10 09:00 · 上交所

累计增持……
"""
ROW = {"ticker": "600519.SS", "name": "贵州茅台", "industry": "酿酒", "pe": 22.5, "roe_annualized": 36.0,
       "score": 78.4, "quality_score": 92.0, "value_score": float("nan")}


class LLM:
    def __init__(self, answer, delay=0.0, fail_on=None):
        self.answer, self.delay, self.fail_on, self.prompts = answer, delay, fail_on, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("provider down")
        time.sleep(self.delay)
        return SimpleNamespace(content=self.answer, usage_metadata={"input_tokens": 3000, "output_tokens": 500},
                               response_metadata={"token_usage": {"prompt_cache_hit_tokens": 1000}})


@pytest.mark.unit
def test_a_review_reads_the_numbers_and_the_news_and_answers_in_shape():
    answer = ('{"verdict": "关注", "thesis": "ROE 36% 且三季度营收增 9%", "risks": ["估值不低", "需求放缓", "a", "b"],'
              ' "watch": "批价"}')
    llm = LLM(answer)
    out = review.review([ROW], llm, news=lambda t, s, e: NEWS, ranking="质量成长", price=(2, 8, 0.04),
                        as_of="2026-10-16")[0]
    assert out["verdict"] == "关注" and out["risks"] == ["估值不低", "需求放缓", "a"] and out["watch"] == "批价"
    assert out["news_items"] == 2
    assert out["cost"] == pytest.approx((2000 * 2 + 1000 * 0.04 + 500 * 8) / 1e6)
    prompt = llm.prompts[0]
    assert "三季度营收同比增长 9%" in prompt and "ROE(年化): 36.00" in prompt and "质量 92" in prompt
    assert "价值" not in prompt.split("因子得分")[1].split("\n")[0]          # a missing score is left out
    assert "质量成长" in prompt


@pytest.mark.unit
def test_unreadable_answers_failures_and_timeouts_are_not_invented():
    assert review.parse("我认为应该回避，因为……")["verdict"] == "回避"
    assert review.parse("{not json")["verdict"] == "观望" and not review.parse("{not json")["readable"]
    assert review.parse('{"verdict": "买入", "thesis": "x"}')["verdict"] == "观望"
    rows = [ROW, {"ticker": "000858.SZ", "name": "五粮液"}]
    out = review.review(rows, LLM('{"verdict": "观望"}', fail_on="000858.SZ"), news=None)
    assert out[0]["verdict"] == "观望" and "error" in out[1] and "provider down" in out[1]["error"]
    started = time.time()
    slow = review.review([ROW], LLM('{"verdict": "关注"}', delay=5), news=None, timeout=0.3)
    assert slow[0]["error"] == "模型超时" and time.time() - started < 3


@pytest.mark.unit
def test_news_digest_keeps_headlines_short():
    digest = review.news_digest(NEWS, items=1)
    assert digest.startswith("- [2026-10-15 18:00] 贵州茅台：三季度营收同比增长 9%：公司公告")
    assert review.news_digest("No East Money news for X between a and b.") == ""
