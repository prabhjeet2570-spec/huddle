"""The agent state machine.

    START -> agent -> (tools -> agent)* -> END

LangGraph ships a prebuilt ``ToolNode`` that takes the tool calls off the last
AI message, runs them and appends the results. That is exactly the loop we
want for now, so there is no reason to hand-roll it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from app.agent.tools import AgentContext
from app.agent.toolkit import structured_tools

logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    system_prompt: str
    llm_calls: int
    tool_calls: int


def build_graph(model: BaseChatModel, context: AgentContext) -> CompiledStateGraph:
    tools = structured_tools(context)
    bound = model.bind_tools(tools)

    async def call_model(state: AgentState) -> dict[str, Any]:
        messages = [
            SystemMessage(content=state["system_prompt"]),
            *state["messages"],
        ]
        response = await bound.ainvoke(messages)
        return {
            "messages": [response],
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    def after_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return END

    builder = StateGraph(AgentState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", ToolNode(tools))

    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", after_agent, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")

    return builder.compile()
