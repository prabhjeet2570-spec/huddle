"""Deterministic failure injection.

Chaos testing that uses randomness cannot be asserted on. This injector is
keyed by ``(scenario, call site)`` and fires on an exact call count, so a test
can say "fail the third attempt of the notify step" and get precisely that,
every run, in CI.

The injector is inert unless ``HUDDLE_FAILURE_INJECTION_ENABLED`` is set, so
there is no path by which it can arm itself in production.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum

from app.config import settings
from app.domain.exceptions import (
    MalformedToolResponse,
    ToolTimeout,
    UpstreamUnavailable,
)


class Scenario(StrEnum):
    """The failure modes the suite exercises against every step."""

    TOOL_TIMEOUT = "tool_timeout"
    TOOL_SERVER_ERROR = "tool_server_error"
    MALFORMED_RESPONSE = "malformed_response"
    SLOT_TAKEN_BEFORE_CONFIRM = "slot_taken_before_confirm"
    NOTIFICATION_FAILURE = "notification_failure"
    CALENDAR_FAILURE = "calendar_failure"
    COMPENSATION_FAILURE = "compensation_failure"


@dataclass
class _Rule:
    scenario: Scenario
    target: str
    #: Which invocations of ``target`` should fail. ``None`` means all of them.
    on_calls: tuple[int, ...] | None
    remaining: int | None
    seen: int = 0


@dataclass
class FailureInjector:
    _rules: dict[str, _Rule] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def arm(
        self,
        scenario: Scenario,
        target: str,
        *,
        on_calls: tuple[int, ...] | None = None,
        times: int | None = None,
    ) -> None:
        """Schedule ``scenario`` to fire at ``target``.

        ``on_calls`` selects 1-indexed invocations; ``times`` caps how many
        failures fire in total. Passing neither fails every invocation.
        """
        with self._lock:
            self._rules[target] = _Rule(scenario, target, on_calls, times)

    def disarm(self, target: str | None = None) -> None:
        with self._lock:
            if target is None:
                self._rules.clear()
            else:
                self._rules.pop(target, None)

    def is_armed(self, target: str) -> bool:
        return target in self._rules

    def maybe_fail(self, target: str) -> None:
        """Raise the armed error if this invocation of ``target`` is selected."""
        if not settings.failure_injection_enabled:
            return

        with self._lock:
            rule = self._rules.get(target)
            if rule is None:
                return
            rule.seen += 1
            if rule.on_calls is not None and rule.seen not in rule.on_calls:
                return
            if rule.remaining is not None:
                if rule.remaining <= 0:
                    return
                rule.remaining -= 1
            scenario = rule.scenario

        raise _ERRORS[scenario](f"injected {scenario} at {target}")

    def should_fire(self, scenario: Scenario, target: str) -> bool:
        """Non-raising variant, for scenarios simulated by side effect."""
        if not settings.failure_injection_enabled:
            return False
        with self._lock:
            rule = self._rules.get(target)
            if rule is None or rule.scenario is not scenario:
                return False
            rule.seen += 1
            if rule.on_calls is not None and rule.seen not in rule.on_calls:
                return False
            if rule.remaining is not None:
                if rule.remaining <= 0:
                    return False
                rule.remaining -= 1
            return True


_ERRORS: dict[Scenario, type[Exception]] = {
    Scenario.TOOL_TIMEOUT: ToolTimeout,
    Scenario.TOOL_SERVER_ERROR: UpstreamUnavailable,
    Scenario.MALFORMED_RESPONSE: MalformedToolResponse,
    Scenario.NOTIFICATION_FAILURE: UpstreamUnavailable,
    Scenario.CALENDAR_FAILURE: UpstreamUnavailable,
    Scenario.COMPENSATION_FAILURE: UpstreamUnavailable,
    Scenario.SLOT_TAKEN_BEFORE_CONFIRM: UpstreamUnavailable,
}

injector = FailureInjector()


@contextmanager
def injected(
    scenario: Scenario,
    target: str,
    *,
    on_calls: tuple[int, ...] | None = None,
    times: int | None = None,
) -> Iterator[FailureInjector]:
    """Arm a scenario for the duration of a block, then disarm it."""
    injector.arm(scenario, target, on_calls=on_calls, times=times)
    try:
        yield injector
    finally:
        injector.disarm(target)
