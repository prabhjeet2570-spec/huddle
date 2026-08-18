"""The agent state machine.

    START -> guard -> agent -> (tools -> agent)* -> finalize -> END
               |                  |
               +--> finalize <----+   (budget breach, escalation, confirmation)

Three things distinguish this from the stock ReAct loop:

* **guard** runs before the model and can end the turn without ever calling
  it, which is what bounds a conversation that has already breached its
  budget.
* **tools** routes through :class:`~app.agent.executor.ToolExecutor` rather
  than LangGraph's ``ToolNode``, so validation, blast-radius gating, retries
  and escalation all apply.
* **state** carries the budget, the outcome and the escalation reason, so a
  turn's disposition is a value the graph computes rather than something
  inferred afterwards from the transcript.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from app.agent.executor import ToolExecutor
from app.agent.llm import usage_of
from app.config import settings
from app.domain.enums import ConversationStatus
from app.observability.tracing import llm_span, record_llm_usage
from app.reliability.budget import Budget
from app.reliability.retry import call_with_retry

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    system_prompt: str
    budget: Budget
    llm_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    outcome: str
    escalation_reason: str | None
    awaiting_confirmation: bool


def build_graph(
    model: BaseChatModel,
    executor: ToolExecutor,
    provider: str | None = None,
    model_name: str | None = None,
) -> CompiledStateGraph:
    provider_name = provider or settings.llm_provider
    display_model = model_name or settings.llm_model

    async def guard(state: AgentState) -> dict[str, Any]:
        """Refuse to start a turn the conversation cannot afford."""
        budget: Budget = state["budget"]
        breach = budget.breach()
        if breach is None:
            return {}
        return {
            "outcome": ConversationStatus.ESCALATED,
            "escalation_reason": str(breach),
            "messages": [
                AIMessage(
                    content=(
                        "This conversation has reached its safety budget, so I "
                        "have stopped and flagged it for a person to review. "
                        "Nothing was changed."
                    )
                )
            ],
        }

    async def call_model(state: AgentState) -> dict[str, Any]:
        budget: Budget = state["budget"]
        messages = [
            SystemMessage(content=state["system_prompt"]),
            *state["messages"],
        ]

        with llm_span(display_model, provider_name, "") as span:
            try:
                # The model call gets the same treatment as a tool call: a 429
                # or a provider 5xx is retried, a 401 or a malformed request is
                # not, because it will fail identically every time.
                response, _ = await call_with_retry(
                    "llm.chat", lambda: model.ainvoke(messages)
                )
            except Exception as error:
                # Escalate rather than letting a stack trace become a 500. The
                # user gets a sentence and the conversation is flagged.
                span.record_exception(error)
                logger.warning("llm call failed: %s: %s", type(error).__name__, error)
                # Name the underlying failure, not the retry wrapper. An
                # operator reading the alert needs to know it was a 503, not
                # that we gave up after three tries.
                root = getattr(error, "last_error", error)
                return {
                    "outcome": ConversationStatus.ESCALATED,
                    "escalation_reason": (
                        f"language model unavailable: {type(root).__name__}: {root}"
                    ),
                    "messages": [
                        AIMessage(
                            content=(
                                "I cannot reach the assistant service right "
                                "now, so I have stopped rather than guess. "
                                "Nothing was changed. Please try again shortly."
                            )
                        )
                    ],
                }

            prompt_tokens, completion_tokens = usage_of(response)
            record_llm_usage(
                span,
                prompt_tokens,
                completion_tokens,
                response_model=getattr(response, "response_metadata", {}).get("model"),
                finish_reason=(getattr(response, "response_metadata", {}) or {}).get(
                    "finish_reason"
                ),
            )

        budget.charge_tokens(prompt_tokens, completion_tokens)
        return {
            "messages": [response],
            "llm_calls": state.get("llm_calls", 0) + 1,
            "prompt_tokens": state.get("prompt_tokens", 0) + prompt_tokens,
            "completion_tokens": state.get("completion_tokens", 0) + completion_tokens,
        }

    async def run_tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)

        outputs: list[BaseMessage] = []
        escalated = False
        awaiting = False
        reason: str | None = None

        for call in last.tool_calls:
            result = await executor.execute(
                call["name"], call.get("args") or {}, call.get("id")
            )
            outputs.append(
                ToolMessage(
                    content=result.content,
                    tool_call_id=call.get("id") or "",
                    name=call["name"],
                )
            )
            if result.escalated:
                escalated = True
                reason = result.content
            if result.awaiting_confirmation:
                awaiting = True

        update: dict[str, Any] = {
            "messages": outputs,
            "tool_calls": state.get("tool_calls", 0) + len(last.tool_calls),
            "awaiting_confirmation": awaiting
            or state.get("awaiting_confirmation", False),
        }
        if escalated:
            update["outcome"] = ConversationStatus.ESCALATED
            update["escalation_reason"] = reason
        return update

    def after_guard(state: AgentState) -> str:
        return END if state.get("outcome") else "agent"

    def after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    def after_tools(state: AgentState) -> str:
        """Escalation ends the turn. Everything else goes back to the model.

        The model still gets a chance to speak after a blocked or failed call,
        because the user needs a sentence explaining what happened, not a
        silent stop.
        """
        if state.get("outcome") == ConversationStatus.ESCALATED:
            return "escalation_notice"
        return "agent"

    async def escalation_notice(state: AgentState) -> dict[str, Any]:
        return {
            "messages": [
                AIMessage(
                    content=(
                        "I could not complete that reliably, so I have stopped "
                        "and flagged this conversation for a person to review. "
                        "Nothing was left half-done."
                    )
                )
            ]
        }

    builder = StateGraph(AgentState)
    builder.add_node("guard", guard)
    builder.add_node("agent", call_model)
    builder.add_node("tools", run_tools)
    builder.add_node("escalation_notice", escalation_notice)

    builder.add_edge(START, "guard")
    builder.add_conditional_edges("guard", after_guard, {"agent": "agent", END: END})
    builder.add_conditional_edges("agent", after_agent, {"tools": "tools", END: END})
    builder.add_conditional_edges(
        "tools",
        after_tools,
        {"agent": "agent", "escalation_notice": "escalation_notice"},
    )
    builder.add_edge("escalation_notice", END)

    return builder.compile()
