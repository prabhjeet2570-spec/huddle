"""Model construction and the scripted model used by the test suite.

The provider is configuration, not architecture. Everything downstream of this
module works against the LangChain message interface, so swapping OpenAI for
Anthropic is one environment variable.

``FakeChatModel`` matters as much as the real ones: it makes the agent loop
testable without a network, an API key or a bill, which is what allows the
guardrail and failure-injection suites to run in CI.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage

from app.config import settings
from app.domain.exceptions import ModelNotConfigured


class FakeChatModel(GenericFakeChatModel):
    """Replays a scripted list of AI messages, one per invocation.

    Bound tools are accepted and ignored, so a test can script the exact
    sequence of tool calls it wants the agent to attempt, including malformed
    ones that a real model would rarely produce on demand.
    """

    script: list[AIMessage] = []
    calls: list[list[BaseMessage]] = []

    def __init__(self, script: list[AIMessage] | None = None, **kwargs: Any) -> None:
        messages = iter(script or [])
        super().__init__(messages=messages, **kwargs)
        object.__setattr__(self, "script", list(script or []))
        object.__setattr__(self, "calls", [])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> FakeChatModel:
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        self.calls.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def build_model(
    tools: list[dict[str, Any]] | None = None,
    override: BaseChatModel | None = None,
) -> BaseChatModel:
    """Return a tool-bound chat model for the configured provider."""
    model = override if override is not None else _construct()
    if tools and hasattr(model, "bind_tools"):
        return model.bind_tools(tools)
    return model


def _construct() -> BaseChatModel:
    provider = settings.llm_provider
    api_key = settings.llm_api_key.get_secret_value()

    if provider == "fake":
        return FakeChatModel([])

    if not api_key:
        # The SDKs raise their own error here, but from deep inside client
        # construction and with no hint about which variable to set.
        raise ModelNotConfigured(provider)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.llm_model,
            api_key=api_key or None,
            temperature=settings.llm_temperature,
            timeout=settings.tool_timeout_seconds,
            max_retries=0,  # retries are this project's job, not the SDK's
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.llm_model,
        api_key=api_key or None,
        temperature=settings.llm_temperature,
        timeout=settings.tool_timeout_seconds,
        max_retries=0,
    )


def usage_of(message: BaseMessage) -> tuple[int, int]:
    """Extract (prompt, completion) tokens across provider shapes."""
    usage = getattr(message, "usage_metadata", None) or {}
    if usage:
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    metadata = getattr(message, "response_metadata", {}) or {}
    raw = metadata.get("token_usage") or metadata.get("usage") or {}
    return (
        int(raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0),
        int(raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0),
    )
