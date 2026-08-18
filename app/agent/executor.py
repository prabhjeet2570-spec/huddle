"""The tool executor.

Every tool call the model makes passes through here, and this is where the
guardrails actually live. In order:

1. **Unknown tool** - rejected. A hallucinated tool name never reaches code.
2. **Budget** - a conversation past its tool-call or token budget is halted
   and escalated rather than allowed to keep spending.
3. **Schema validation** - arguments are parsed by Pydantic. Invalid arguments
   are returned to the model with the specific failure so it can retry.
4. **Blast radius** - reads and holds run. Confirmations and cancellations are
   parked as a pending confirmation and are *not* executed.
5. **Retry** - the handler runs under a deadline with bounded exponential
   backoff, retrying only failures classified as retryable.
6. **Escalation** - when retries are exhausted, the conversation is marked as
   needing human review and an alert record is written.

The prompt asks the model to behave. This module is what happens when it does
not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agent.tools import TOOLS, AgentContext, err, summarize
from app.config import settings
from app.domain.enums import ActionRisk, AlertKind, ToolCallStatus
from app.domain.exceptions import (
    BudgetExceeded,
    GuardrailError,
    HuddleError,
)
from app.infrastructure.repositories.conversation_repository import (
    ConversationRepository,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.observability.tracing import record_tool_result, tool_span
from app.reliability.budget import Budget
from app.reliability.retry import RetriesExhausted, call_with_retry
from app.reliability.saga import SagaFailed

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolResult:
    """What the executor decided, in a form the graph can route on."""

    tool_name: str
    content: str
    status: ToolCallStatus
    risk: ActionRisk
    attempts: int = 1
    latency_ms: float = 0.0
    #: Set when the call must stop the conversation rather than loop again.
    escalated: bool = False
    #: Set when the call was parked awaiting explicit user confirmation.
    awaiting_confirmation: bool = False
    error_type: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    def __init__(
        self,
        context: AgentContext,
        conversations: ConversationRepository,
        telemetry: TelemetryRepository,
        budget: Budget,
        handlers: dict[str, Any],
    ) -> None:
        self._context = context
        self._conversations = conversations
        self._telemetry = telemetry
        self._budget = budget
        self._handlers = handlers

    async def execute(
        self,
        tool_name: str,
        raw_arguments: dict[str, Any],
        tool_call_id: str | None = None,
        *,
        preconfirmed: bool = False,
    ) -> ToolResult:
        """Run one tool call through the full guardrail stack."""
        spec = TOOLS.get(tool_name)
        if spec is None:
            return await self._reject(
                tool_name,
                ActionRisk.READ,
                ToolCallStatus.INVALID_ARGUMENTS,
                err(
                    f"There is no tool called '{tool_name}'. Available tools: "
                    f"{', '.join(TOOLS)}."
                ),
                error_type="UnknownTool",
                arguments=raw_arguments,
            )

        breach = self._budget.breach()
        if breach is not None:
            return await self._escalate_budget(spec.name, spec.risk, breach)

        try:
            arguments = spec.schema.model_validate(raw_arguments)
        except ValidationError as error:
            return await self._reject(
                spec.name,
                spec.risk,
                ToolCallStatus.INVALID_ARGUMENTS,
                err(
                    f"Invalid arguments for {spec.name}: "
                    f"{_describe(error)}. Fix them and call the tool again."
                ),
                error_type="ValidationError",
                arguments=raw_arguments,
            )

        if (
            spec.risk is ActionRisk.HIGH
            and settings.require_confirmation_for_high_risk
            and not preconfirmed
        ):
            return await self._park_for_confirmation(spec.name, spec.risk, arguments)

        return await self._run(spec.name, spec.risk, arguments, tool_call_id)

    # --- Stages ------------------------------------------------------------

    async def _run(
        self,
        tool_name: str,
        risk: ActionRisk,
        arguments: BaseModel,
        tool_call_id: str | None = None,
    ) -> ToolResult:
        handler = self._handlers[tool_name]
        payload = arguments.model_dump(mode="json")
        started = time.perf_counter()
        attempts = 1

        def _count(attempt: int, _error: BaseException | None) -> None:
            nonlocal attempts
            attempts = attempt

        with tool_span(
            tool_name,
            risk,
            self._context.conversation_id,
            tool_call_id,
            payload,
        ) as span:
            try:
                content, outcome = await call_with_retry(
                    tool_name,
                    lambda: handler(self._context, arguments),
                    on_attempt=_count,
                )
            except RetriesExhausted as error:
                latency = (time.perf_counter() - started) * 1000
                record_tool_result(
                    span, ToolCallStatus.ESCALATED, attempts, error=error
                )
                return await self._escalate_retries(
                    tool_name, risk, error, attempts, latency, payload
                )
            except GuardrailError as error:
                # Rate limiting is a refusal, not a failure. The model should
                # explain it, not retry it.
                latency = (time.perf_counter() - started) * 1000
                record_tool_result(span, ToolCallStatus.BLOCKED, attempts, error=error)
                return await self._record(
                    ToolResult(
                        tool_name=tool_name,
                        content=err(str(error)),
                        status=ToolCallStatus.BLOCKED,
                        risk=risk,
                        attempts=attempts,
                        latency_ms=latency,
                        error_type=error.code,
                    ),
                    payload,
                )
            except SagaFailed as error:
                # The transaction rolled back. State is clean (or an alert was
                # already raised saying it is not), but a booking that cannot
                # complete still needs a person to look at it.
                latency = (time.perf_counter() - started) * 1000
                record_tool_result(
                    span, ToolCallStatus.ESCALATED, attempts, error=error
                )
                return await self._escalate_retries(
                    tool_name, risk, error, attempts, latency, payload
                )
            except HuddleError as error:
                latency = (time.perf_counter() - started) * 1000
                record_tool_result(
                    span, ToolCallStatus.DOMAIN_ERROR, attempts, error=error
                )
                return await self._record(
                    ToolResult(
                        tool_name=tool_name,
                        content=err(str(error)),
                        status=ToolCallStatus.DOMAIN_ERROR,
                        risk=risk,
                        attempts=attempts,
                        latency_ms=latency,
                        error_type=error.code,
                    ),
                    payload,
                )
            except Exception as error:
                latency = (time.perf_counter() - started) * 1000
                logger.exception("unhandled error in tool %s", tool_name)
                record_tool_result(
                    span, ToolCallStatus.ESCALATED, attempts, error=error
                )
                return await self._escalate_retries(
                    tool_name, risk, error, attempts, latency, payload
                )

            latency = (time.perf_counter() - started) * 1000
            record_tool_result(span, ToolCallStatus.OK, outcome.attempts, content)

        self._budget.charge_tool_call()
        return await self._record(
            ToolResult(
                tool_name=tool_name,
                content=content,
                status=ToolCallStatus.OK,
                risk=risk,
                attempts=outcome.attempts,
                latency_ms=latency,
            ),
            payload,
        )

    async def _park_for_confirmation(
        self,
        tool_name: str,
        risk: ActionRisk,
        arguments: BaseModel,
    ) -> ToolResult:
        """Record the action and hand the decision back to the user.

        The validated arguments are stored verbatim and replayed on
        confirmation, so what the user agrees to is exactly what runs.
        """
        summary = summarize(tool_name, arguments)
        await self._conversations.create_pending_confirmation(
            self._context.conversation_id,
            tool_name,
            arguments.model_dump(mode="json"),
            summary,
            self._context.booking.now,
        )
        self._budget.charge_tool_call()
        return await self._record(
            ToolResult(
                tool_name=tool_name,
                content=(
                    "Status: confirmation_required\n"
                    f"Pending action: {summary}\n"
                    "This action was NOT performed. Read the details back to "
                    "the user and ask them to confirm in their next message."
                ),
                status=ToolCallStatus.BLOCKED,
                risk=risk,
                awaiting_confirmation=True,
                error_type="ConfirmationRequired",
            ),
            arguments.model_dump(mode="json"),
        )

    async def _escalate_budget(
        self,
        tool_name: str,
        risk: ActionRisk,
        breach: BudgetExceeded,
    ) -> ToolResult:
        await self._telemetry.raise_alert(
            AlertKind.BUDGET_EXCEEDED,
            str(breach),
            org_id=self._context.org_id,
            conversation_id=self._context.conversation_id,
            severity="warning",
            details={
                "resource": breach.resource,
                "used": breach.used,
                "limit": breach.limit,
                "tool_name": tool_name,
            },
        )
        return await self._record(
            ToolResult(
                tool_name=tool_name,
                content=err(str(breach)),
                status=ToolCallStatus.BLOCKED,
                risk=risk,
                escalated=True,
                error_type="BudgetExceeded",
            ),
            None,
        )

    async def _escalate_retries(
        self,
        tool_name: str,
        risk: ActionRisk,
        error: BaseException,
        attempts: int,
        latency_ms: float,
        arguments: dict[str, Any] | None,
    ) -> ToolResult:
        await self._telemetry.raise_alert(
            AlertKind.TOOL_RETRIES_EXHAUSTED,
            f"{tool_name} failed after {attempts} attempts: "
            f"{type(error).__name__}: {error}",
            org_id=self._context.org_id,
            conversation_id=self._context.conversation_id,
            severity="critical",
            details={
                "tool_name": tool_name,
                "attempts": attempts,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        return await self._record(
            ToolResult(
                tool_name=tool_name,
                content=err(
                    f"{tool_name} is failing repeatedly and could not be "
                    "completed. This conversation has been flagged for human "
                    "review. Tell the user plainly and do not retry."
                ),
                status=ToolCallStatus.ESCALATED,
                risk=risk,
                attempts=attempts,
                latency_ms=latency_ms,
                escalated=True,
                error_type=type(error).__name__,
            ),
            arguments,
        )

    async def _reject(
        self,
        tool_name: str,
        risk: ActionRisk,
        status: ToolCallStatus,
        content: str,
        error_type: str,
        arguments: dict[str, Any] | None,
    ) -> ToolResult:
        # A rejected call still costs budget. Otherwise a model that emits
        # nothing but malformed calls loops for free.
        self._budget.charge_tool_call()
        return await self._record(
            ToolResult(
                tool_name=tool_name,
                content=content,
                status=status,
                risk=risk,
                error_type=error_type,
            ),
            arguments,
        )

    async def _record(
        self,
        result: ToolResult,
        arguments: dict[str, Any] | None,
    ) -> ToolResult:
        await self._telemetry.record_tool_invocation(
            conversation_id=self._context.conversation_id,
            tool_name=result.tool_name,
            risk=result.risk,
            status=result.status,
            attempts=result.attempts,
            latency_ms=result.latency_ms,
            error_type=result.error_type,
            arguments=arguments,
        )
        logger.info(
            "tool=%s status=%s attempts=%d latency_ms=%.1f",
            result.tool_name,
            result.status,
            result.attempts,
            result.latency_ms,
        )
        return result


def _describe(error: ValidationError) -> str:
    """A compact, model-readable rendering of what failed validation."""
    parts = []
    for item in error.errors(include_url=False)[:5]:
        location = ".".join(str(piece) for piece in item["loc"]) or "(root)"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)
