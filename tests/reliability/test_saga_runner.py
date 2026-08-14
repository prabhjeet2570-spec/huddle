"""The saga runner in isolation, without a database."""

from __future__ import annotations

import pytest

from app.domain.enums import SagaStatus, StepPhase, StepStatus
from app.reliability.saga import SagaFailed, SagaStep, run_saga


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def step(self, step, phase, status, detail=None) -> None:
        self.events.append((step, phase, status))

    def terminal(self, phase: StepPhase) -> list[str]:
        return [
            step
            for step, event_phase, status in self.events
            if event_phase == phase
            and status in (StepStatus.SUCCEEDED, StepStatus.NOOP)
        ]


def _step(name: str, log: list[str], fail: bool = False, undone: bool = True):
    async def execute(_context):
        log.append(f"do:{name}")
        if fail:
            raise RuntimeError(f"{name} exploded")
        return name

    async def compensate(_context):
        log.append(f"undo:{name}")
        return undone

    return SagaStep(name=name, execute=execute, compensate=compensate)


async def test_all_steps_run_in_order_on_success():
    log: list[str] = []
    result = await run_saga(
        "book",
        [_step("one", log), _step("two", log), _step("three", log)],
        {},
        Recorder(),
    )

    assert log == ["do:one", "do:two", "do:three"]
    assert result.status is SagaStatus.COMPLETED
    assert result.completed_steps == ["one", "two", "three"]


async def test_failure_compensates_completed_steps_in_reverse_order():
    log: list[str] = []
    recorder = Recorder()

    with pytest.raises(SagaFailed) as raised:
        await run_saga(
            "book",
            [
                _step("one", log),
                _step("two", log),
                _step("three", log, fail=True),
                _step("four", log),
            ],
            {},
            recorder,
        )

    assert log == ["do:one", "do:two", "do:three", "undo:two", "undo:one"]
    assert raised.value.failed_step == "three"
    assert raised.value.compensated is True
    # "four" never ran, so it is never compensated.
    assert recorder.terminal(StepPhase.COMPENSATE) == ["two", "one"]


async def test_a_failure_on_the_first_step_compensates_nothing():
    log: list[str] = []

    with pytest.raises(SagaFailed) as raised:
        await run_saga("book", [_step("one", log, fail=True)], {}, Recorder())

    assert log == ["do:one"]
    assert raised.value.compensated is True


async def test_a_compensation_returning_false_is_recorded_as_a_noop():
    """Idempotency is visible in the log, not just implied."""
    log: list[str] = []
    recorder = Recorder()

    with pytest.raises(SagaFailed):
        await run_saga(
            "book",
            [_step("one", log, undone=False), _step("two", log, fail=True)],
            {},
            recorder,
        )

    assert ("one", StepPhase.COMPENSATE, StepStatus.NOOP) in recorder.events


async def test_a_failing_compensation_does_not_strand_the_rest_of_the_unwind():
    log: list[str] = []

    async def bad_compensate(_context):
        log.append("undo:two-failed")
        raise RuntimeError("compensation exploded")

    async def execute_two(_context):
        log.append("do:two")
        return "two"

    with pytest.raises(SagaFailed) as raised:
        await run_saga(
            "book",
            [
                _step("one", log),
                SagaStep("two", execute_two, bad_compensate),
                _step("three", log, fail=True),
            ],
            {},
            Recorder(),
        )

    # "one" is still undone even though "two" could not be.
    assert "undo:one" in log
    assert raised.value.compensated is False
    assert raised.value.compensation_errors


async def test_a_non_critical_step_failure_does_not_roll_back_the_saga():
    log: list[str] = []

    async def optional(_context):
        log.append("do:optional")
        raise RuntimeError("nice to have")

    result = await run_saga(
        "book",
        [
            _step("one", log),
            SagaStep("optional", optional, None, critical=False),
            _step("two", log),
        ],
        {},
        Recorder(),
    )

    assert result.status is SagaStatus.COMPLETED
    assert "undo:one" not in log
