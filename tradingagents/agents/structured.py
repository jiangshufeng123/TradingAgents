"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    The binding asks for the raw reply too (``include_raw``), so when a model answers in
    text instead of the schema, that answer is used rather than thrown away and asked for
    again (see ``invoke_structured_with_text``). Logs a warning when the binding fails so
    the user understands the agent will use free-text generation for every call.
    """
    try:
        try:
            return llm.with_structured_output(schema, include_raw=True)
        except TypeError:          # a client without include_raw: parsed results only
            return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def _text_of(message: Any) -> str:
    """The text of a raw reply that carries no tool call, else ''."""
    if message is None or getattr(message, "tool_calls", None):
        return ""
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return content if isinstance(content, str) and content.strip() else ""


def invoke_structured_with_text(structured_llm: Any | None, prompt: Any, agent_name: str) -> tuple[T | None, str]:
    """Run the structured call: ``(parsed, "")``, or ``(None, text)`` when the model wrote its
    answer as text instead of the schema, or ``(None, "")`` when there is nothing to use.

    A thinking model that may not be forced to call the schema tool (DeepSeek's V4 and
    reasoner reject ``tool_choice``) sometimes answers in prose. The prompts ask for that
    same answer as labelled text, so the prose is the free-text answer already: using it
    saves a second call to the same model (for the Portfolio Manager, the dearest one).
    """
    if structured_llm is None:
        return None, ""
    try:
        result = structured_llm.invoke(prompt)
    except Exception as exc:
        logger.warning(
            "%s: structured-output invocation failed (%s); retrying once as free text",
            agent_name, exc,
        )
        return None, ""
    raw = None
    if isinstance(result, dict) and "parsed" in result and "raw" in result:
        raw, result = result.get("raw"), result.get("parsed")
    if result is not None:
        return result, ""
    text = _text_of(raw)
    if text:
        logger.info("%s: the model answered in text rather than the structured format; "
                    "using that answer (no second call)", agent_name)
        return None, text
    # A thinking model can answer with neither a tool call nor text; ask again as text.
    logger.warning(
        "%s: structured-output invocation failed (%s); retrying once as free text",
        agent_name, "structured output returned no parsed result",
    )
    return None, ""


def invoke_structured(structured_llm: Any | None, prompt: Any, agent_name: str) -> T | None:
    """Run the structured call; ``None`` when there is none or it fails.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape), so a caller can forward the same value to its free-text fallback.
    """
    return invoke_structured_with_text(structured_llm, prompt, agent_name)[0]


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; else the model's own text answer, or
    one free-text call when there is none."""
    result, text = invoke_structured_with_text(structured_llm, prompt, agent_name)
    if result is not None:
        return render(result)
    if text:
        return text
    return plain_llm.invoke(prompt).content
