"""A saga runner: forward steps, and compensations in reverse order.

A booking is not one write. It holds a room, confirms it, notifies the
attendees and syncs a calendar, and every one of those can fail independently.
Without compensation, a calendar failure leaves a confirmed room nobody knows
about; a notify failure leaves attendees uninformed of a meeting that exists.

The rule this runner enforces is: if step *k* fails, compensate steps
*k-1 … 1* in reverse, then report the original failure. Compensations are
required to be idempotent, because the runner may be re-driven after a crash
and because a compensation that fails is retried.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.domain.enums import SagaStatus, StepPhase, StepStatus
from app.domain.exceptions import HuddleError


class SagaRecorder(Protocol):
    """Durable log of everything the saga did. See ``SagaEventModel``."""

    async def step(
        self,
        step: str,
        phase: StepPhase,
        status: StepStatus,
        detail: str | None = None,
    ) -> None: ...


@dataclass(slots=True)
class SagaStep:
    name: str
    #: Runs the forward action. Receives the mutable saga context.
    execute: Callable[[dict[str, Any]], Awaitable[Any]]
    #: Undoes it. MUST be idempotent: returning ``False`` means "nothing to
    #: undo", which the runner records as a NOOP rather than a failure.
    compensate: Callable[[dict[str, Any]], Awaitable[bool]] | None = None
    #: Steps flagged critical abort the saga on failure (the default). A
    #: non-critical step that fails is compensated but does not roll back the
    #: whole booking.
    critical: bool = True


class SagaFailed(HuddleError):
    """The saga aborted. ``compensated`` says whether cleanup succeeded.

    Never retryable at this level. The individual steps already retried under
    their own policies, and re-running a saga whose compensations have fired
    would redo work that was deliberately undone.
    """

    retryable = False

    def __init__(
        self,
        saga_name: str,
        failed_step: str,
        cause: BaseException,
        compensated: bool,
        compensation_errors: list[str],
    ) -> None:
        self.saga_name = saga_name
        self.failed_step = failed_step
        self.cause = cause
        self.compensated = compensated
        self.compensation_errors = compensation_errors
        state = "rolled back cleanly" if compensated else "ROLLBACK INCOMPLETE"
        super().__init__(
            f"{saga_name} failed at step '{failed_step}' ({state}): "
            f"{type(cause).__name__}: {cause}"
        )


@dataclass(slots=True)
class SagaResult:
    status: SagaStatus
    context: dict[str, Any]
    completed_steps: list[str] = field(default_factory=list)
    compensated_steps: list[str] = field(default_factory=list)


class NullRecorder:
    async def step(
        self,
        step: str,
        phase: StepPhase,
        status: StepStatus,
        detail: str | None = None,
    ) -> None:
        return None


async def run_saga(
    name: str,
    steps: list[SagaStep],
    context: dict[str, Any],
    recorder: SagaRecorder | None = None,
) -> SagaResult:
    """Execute ``steps`` in order, unwinding on the first critical failure."""
    log = recorder or NullRecorder()
    completed: list[SagaStep] = []

    for step in steps:
        await log.step(step.name, StepPhase.EXECUTE, StepStatus.STARTED)
        try:
            result = await step.execute(context)
        except Exception as error:
            await log.step(
                step.name,
                StepPhase.EXECUTE,
                StepStatus.FAILED,
                f"{type(error).__name__}: {error}",
            )
            if not step.critical:
                continue
            compensated, errors = await _compensate(completed, context, log)
            raise SagaFailed(name, step.name, error, compensated, errors) from error

        context[f"{step.name}_result"] = result
        completed.append(step)
        await log.step(step.name, StepPhase.EXECUTE, StepStatus.SUCCEEDED)

    return SagaResult(
        status=SagaStatus.COMPLETED,
        context=context,
        completed_steps=[step.name for step in completed],
    )


async def _compensate(
    completed: list[SagaStep],
    context: dict[str, Any],
    log: SagaRecorder,
) -> tuple[bool, list[str]]:
    """Undo ``completed`` in reverse. Never raises; reports what it could not do."""
    errors: list[str] = []

    for step in reversed(completed):
        if step.compensate is None:
            await log.step(
                step.name,
                StepPhase.COMPENSATE,
                StepStatus.NOOP,
                "step declares no compensation",
            )
            continue

        await log.step(step.name, StepPhase.COMPENSATE, StepStatus.STARTED)
        try:
            undone = await step.compensate(context)
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            errors.append(f"{step.name}: {detail}")
            await log.step(step.name, StepPhase.COMPENSATE, StepStatus.FAILED, detail)
            # Keep unwinding. One stuck compensation must not strand the rest.
            continue

        await log.step(
            step.name,
            StepPhase.COMPENSATE,
            StepStatus.SUCCEEDED if undone else StepStatus.NOOP,
            None if undone else "nothing to undo (already compensated)",
        )

    return not errors, errors
