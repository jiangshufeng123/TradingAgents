"""Style panel: the same evidence read by investors with different beliefs.

The bull/bear debate argues one question, up or down, from two fixed sides.
Real desks disagree for another reason: a value investor, a growth investor
and a momentum trader weigh the same reports differently, and a call that only
one of them would make is weaker than one they share. After the debate, each
style reads the analysts' reports through its own beliefs and returns a stance,
a conviction and what would change its mind; the Research Manager weighs the
panel alongside the debate.

Styles come from ``research_styles`` (TRADINGAGENTS_RESEARCH_STYLES, a comma
list; empty turns the panel off). The A-share/Hong Kong "policy" style is added
for those listings unless the list already names it or is set explicitly.
"""

from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    report_or_absent,
)

logger = logging.getLogger(__name__)

STYLES = {
    "value": (
        "Value investor (Graham / Buffett)",
        "You buy a business for less than it is worth and hold it until the gap closes. You care about "
        "valuation against the company's own history and its peers, balance-sheet strength, durable "
        "competitive advantages and a margin of safety. Price momentum and news flow matter to you only "
        "as the source of a mispricing; an expensive stock with a great story is still expensive.",
    ),
    "growth": (
        "Growth investor (Fisher / Lynch)",
        "You buy companies whose earnings will be much larger in a few years. You care about the revenue "
        "and earnings growth rate and whether it is accelerating, the size of the addressable market, "
        "margins that scale, management that reinvests well, and whether the price is reasonable for the "
        "growth (PEG). A cheap stock that is not growing does not interest you.",
    ),
    "momentum": (
        "Momentum / trend trader",
        "You trade what price and volume are doing, over weeks. You care about the trend and its "
        "strength (moving averages, relative strength against the market), whether volume confirms "
        "moves, near-term catalysts, and where a volatility-based stop would sit. Valuation does not stop "
        "you from riding a trend; a broken trend gets you out regardless of the story.",
    ),
    "policy": (
        "China policy and flows strategist",
        "You read Chinese and Hong Kong equities through policy and money. You care about state policy "
        "direction for the sector (support, regulation, subsidies), the industry cycle and theme rotation, "
        "and who is buying: Northbound and Southbound flows, margin financing, 龙虎榜 seats and retail "
        "attention. You remember T+1 settlement and daily price limits when judging timing and exits.",
    ),
}

_CN_SUFFIXES = (".SS", ".SZ", ".SH", ".BJ", ".HK")


def styles_for(ticker: str, setting) -> list[str]:
    """The styles to run for ``ticker``: the configured list, with policy added for CN/HK by default."""
    if setting is None:
        names = ["value", "growth", "momentum"]
        if ticker.strip().upper().endswith(_CN_SUFFIXES):
            names.append("policy")
        return names
    names = [n.strip().lower() for n in str(setting).split(",") if n.strip()]
    return [n for n in names if n in STYLES]


def _prompt(style: str, state) -> str:
    title, beliefs = STYLES[style]
    return f"""You are a {title} on an investment committee. {beliefs}

Read the research below strictly through your own beliefs. Do not try to be balanced across styles; the
committee hears the other styles separately. Use only the evidence given; say so when what you would
need is missing.

{get_instrument_context_from_state(state)}

Market report: {report_or_absent(state.get("market_report"), "market")}

Sentiment report: {report_or_absent(state.get("sentiment_report"), "sentiment")}

News report: {report_or_absent(state.get("news_report"), "news")}

Fundamentals report: {report_or_absent(state.get("fundamentals_report"), "fundamentals")}

Answer in under 200 words, in exactly this form:
Stance: Bullish / Neutral / Bearish
Conviction: 1-5
Evidence: the two or three facts from the reports that decide it for you
Would change my mind: the one development that would reverse the stance
""" + get_language_instruction()


def create_style_panel(llm, setting=None):
    def style_panel_node(state) -> dict:
        names = styles_for(state["company_of_interest"], setting)
        if not names:
            return {"style_views": ""}

        # A provider failure ends the node like any other agent's, so a
        # checkpointed run resumes here rather than going on without the panel.
        def ask(style):
            return style, llm.invoke(_prompt(style, state)).content.strip()

        # Each thread runs in its own copy of this context, so the calls keep the
        # node's callbacks (token usage is counted under the Style Panel).
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            futures = [pool.submit(contextvars.copy_context().run, ask, n) for n in names]
            views = [f.result() for f in futures]
        text = "\n\n".join(f"### {STYLES[style][0]}\n{view}" for style, view in views)
        return {"style_views": text}

    return style_panel_node
