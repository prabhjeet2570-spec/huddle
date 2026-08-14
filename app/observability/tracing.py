"""OpenTelemetry tracing following the GenAI semantic conventions.

The span tree is the deliverable here. One span per turn, with a child span
per LLM call and per tool call nested underneath, so that the
reasoning -> tool -> observation loop is legible in a trace viewer without
reading a single log line. Every trace carries the conversation id and, once
the conversation reaches a terminal state, its outcome.

Attribute names follow the OTel GenAI conventions (``gen_ai.*``) so the traces
are readable by any conformant backend rather than only by this project.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from app.config import settings

logger = logging.getLogger(__name__)

# --- GenAI semantic convention attribute names -----------------------------
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_OPERATION = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"

# --- Project-specific attributes -------------------------------------------
HUDDLE_OUTCOME = "huddle.conversation.outcome"
HUDDLE_ORG_ID = "huddle.org.id"
HUDDLE_USER_ID = "huddle.user.id"
HUDDLE_TOOL_RISK = "huddle.tool.risk"
HUDDLE_TOOL_STATUS = "huddle.tool.status"
HUDDLE_TOOL_ATTEMPTS = "huddle.tool.attempts"
HUDDLE_TOOL_ARGUMENTS = "huddle.tool.arguments"
HUDDLE_TOOL_RESULT = "huddle.tool.result"
HUDDLE_SAGA_STEP = "huddle.saga.step"
HUDDLE_SAGA_PHASE = "huddle.saga.phase"

_configured = False


def configure_tracing() -> None:
    """Install the tracer provider. Safe to call more than once."""
    global _configured
    if _configured or not settings.otel_enabled:
        return

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": "1.0.0",
            }
        )
    )

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{settings.otel_endpoint}/v1/traces")
            )
        )
    except Exception:
        logger.exception("OTLP exporter unavailable; traces stay local")

    if settings.otel_console_export:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("huddle.agent")


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def _set(span: Span, key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, (str, bool, int, float)):
        span.set_attribute(key, value)
    else:
        span.set_attribute(key, str(value))


@contextmanager
def turn_span(
    conversation_id: Any,
    org_id: Any,
    user_id: Any,
    turn_number: int,
) -> Iterator[Span]:
    """Root span for one user message. Every other span nests under it."""
    with get_tracer().start_as_current_span(
        "huddle.turn", kind=SpanKind.SERVER
    ) as span:
        _set(span, GEN_AI_CONVERSATION_ID, str(conversation_id))
        _set(span, HUDDLE_ORG_ID, str(org_id))
        _set(span, HUDDLE_USER_ID, str(user_id))
        _set(span, "huddle.turn.number", turn_number)
        yield span


@contextmanager
def llm_span(model: str, provider: str, conversation_id: Any) -> Iterator[Span]:
    """One model invocation, per GenAI conventions."""
    with get_tracer().start_as_current_span(
        f"chat {model}", kind=SpanKind.CLIENT
    ) as span:
        _set(span, GEN_AI_SYSTEM, provider)
        _set(span, GEN_AI_OPERATION, "chat")
        _set(span, GEN_AI_REQUEST_MODEL, model)
        _set(span, GEN_AI_REQUEST_TEMPERATURE, settings.llm_temperature)
        _set(span, GEN_AI_CONVERSATION_ID, str(conversation_id))
        yield span


def record_llm_usage(
    span: Span,
    prompt_tokens: int,
    completion_tokens: int,
    response_model: str | None = None,
    finish_reason: str | None = None,
) -> None:
    _set(span, GEN_AI_USAGE_INPUT_TOKENS, prompt_tokens)
    _set(span, GEN_AI_USAGE_OUTPUT_TOKENS, completion_tokens)
    _set(span, GEN_AI_RESPONSE_MODEL, response_model)
    _set(span, GEN_AI_RESPONSE_FINISH_REASONS, finish_reason)


@contextmanager
def tool_span(
    tool_name: str,
    risk: str,
    conversation_id: Any,
    tool_call_id: str | None = None,
    arguments: Any = None,
) -> Iterator[Span]:
    """One tool invocation, nested under the LLM turn that requested it."""
    with get_tracer().start_as_current_span(
        f"execute_tool {tool_name}", kind=SpanKind.INTERNAL
    ) as span:
        _set(span, GEN_AI_OPERATION, "execute_tool")
        _set(span, GEN_AI_TOOL_NAME, tool_name)
        _set(span, GEN_AI_TOOL_CALL_ID, tool_call_id)
        _set(span, GEN_AI_CONVERSATION_ID, str(conversation_id))
        _set(span, HUDDLE_TOOL_RISK, risk)
        _set(span, HUDDLE_TOOL_ARGUMENTS, arguments)
        yield span


def record_tool_result(
    span: Span,
    status: str,
    attempts: int,
    result: str | None = None,
    error: BaseException | None = None,
) -> None:
    _set(span, HUDDLE_TOOL_STATUS, status)
    _set(span, HUDDLE_TOOL_ATTEMPTS, attempts)
    if result is not None:
        _set(span, HUDDLE_TOOL_RESULT, result[:2000])
    if error is not None:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))


@contextmanager
def saga_span(name: str, step: str, phase: str) -> Iterator[Span]:
    with get_tracer().start_as_current_span(f"saga {name}.{step}") as span:
        _set(span, HUDDLE_SAGA_STEP, step)
        _set(span, HUDDLE_SAGA_PHASE, phase)
        yield span


def tag_outcome(span: Span, outcome: str) -> None:
    """Stamp how the conversation ended onto the trace."""
    _set(span, HUDDLE_OUTCOME, outcome)
    if outcome in ("failed", "escalated"):
        span.set_status(Status(StatusCode.ERROR, outcome))
