"""A stand-in model for `tradingagents server selfcheck`.

With ``TRADINGAGENTS_SELFCHECK=1`` every tier gets this model instead of the configured
provider: it answers each agent at once with a fixed text in the shape the prompts ask
for (a rating line, the trader's final proposal), never calls a tool and costs nothing.
That runs a whole analysis - command line, graph, reports, cost ledger, progress output -
in seconds and offline, so a broken change shows before the service is restarted with it.
"""
from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

ENV = "TRADINGAGENTS_SELFCHECK"

TEXT = """**Rating**: Hold

**Recommendation**: Hold

**Executive Summary**: Self-check run: no model was asked; this text only proves the pipeline.

**Investment Thesis**: Self-check placeholder.

FINAL TRANSACTION PROPOSAL: **HOLD**"""


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in ("1", "true", "yes")


class SelfCheckModel(BaseChatModel):
    """Answers every call with TEXT; tools are accepted and never called."""

    @property
    def _llm_type(self) -> str:
        return "selfcheck"

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        raise NotImplementedError("the self-check model answers in text")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = AIMessage(content=TEXT, usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                                          "total_tokens": 2})
        return ChatResult(generations=[ChatGeneration(message=message)])


class SelfCheckClient:
    """Same interface the graph uses from a provider client."""

    provider = "selfcheck"

    def get_llm(self):
        return SelfCheckModel()
