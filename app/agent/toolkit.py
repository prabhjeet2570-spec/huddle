"""Adapters that turn the tool registry into LangChain ``StructuredTool``s.

LangGraph's prebuilt ``ToolNode`` expects real LangChain tools, so the plain
coroutines in :mod:`app.agent.tools` have to be wrapped before the graph can
use them. The agent context is closed over rather than passed through the
model: the organization and the user are not the model's to choose.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from app.agent.tools import HANDLERS, TOOLS, AgentContext


def structured_tools(context: AgentContext) -> list[StructuredTool]:
    tools: list[StructuredTool] = []
    for name, spec in TOOLS.items():
        handler = HANDLERS[name]
        schema = spec.schema

        async def run(_handler=handler, _schema=schema, **kwargs: Any) -> str:
            return await _handler(context, _schema(**kwargs))

        tools.append(
            StructuredTool.from_function(
                coroutine=run,
                name=spec.name,
                description=spec.description,
                args_schema=spec.schema,
            )
        )
    return tools
