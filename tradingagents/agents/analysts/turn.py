"""One turn of a tool-using analyst, and its last turn once its tool rounds are spent."""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# A line that states a trade call: "**Rating**: Hold", "FINAL TRANSACTION
# PROPOSAL: **HOLD**", with or without bold, heading or list markers.
_TRADE_CALL_LINE = re.compile(
    r"^[\s>#*_\-]*(rating|final transaction proposal)[\s*_]*[:：].*$",
    re.IGNORECASE | re.MULTILINE,
)

WRAP_UP = (
    "You have used every tool round this report allows. Write your final report "
    "now from the tool results above, and say which data you could not retrieve."
)


def take_turn(prompt, llm, tools, messages):
    """The analyst's reply and its report, which stays empty while it calls tools.

    Told to wrap up, it answers without tools: a model offered tools may call one
    whatever it is told. Its earlier calls and their results reach it as text,
    since a provider may refuse a history holding tool calls when none are bound.
    """
    if messages and isinstance(messages[-1], HumanMessage) and messages[-1].content == WRAP_UP:
        # The prompt lists the tools it offers; a model told of tools it cannot
        # call may still try, and some providers then answer with nothing.
        prompt = prompt.partial(tool_names="none; your tool rounds are spent")
        result = (prompt | llm).invoke(_as_text(messages))
        return result, strip_trade_call(result.content)
    result = (prompt | llm.bind_tools(tools)).invoke(messages)
    return result, "" if result.tool_calls else strip_trade_call(result.content)


def strip_trade_call(report):
    """The report without the rating / trade-proposal lines an analyst may add.

    Analysts report evidence; the research manager, trader and portfolio
    manager make the call. A rating left in an analyst report reaches the risk
    debaters as part of the "research report" and gets cited as the formal
    research verdict against the research manager's actual plan.
    """
    if not isinstance(report, str) or not report:
        return report
    cleaned = _TRADE_CALL_LINE.sub("", report)
    return re.sub(r"\n{3,}", "\n\n", cleaned).rstrip() + ("\n" if report.endswith("\n") else "")


def _as_text(messages):
    """The history with its tool calls and tool results written out as plain messages."""
    written = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            said = message.content if isinstance(message.content, str) else ""
            calls = "; ".join(f"{c['name']}({json.dumps(c['args'], default=str)})" for c in message.tool_calls)
            written.append(AIMessage(content=f"{said}\n[Called {calls}]".strip()))
        elif isinstance(message, ToolMessage):
            written.append(HumanMessage(content=f"[{message.name or 'Tool'} returned]\n{message.content}"))
        else:
            written.append(message)
    return written
