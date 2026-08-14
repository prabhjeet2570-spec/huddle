"""Timeout and bounded retry with exponential backoff.

The policy classifies before it retries. ``HuddleError.retryable`` is the
single source of truth: a timeout or a 5xx is worth another attempt, while a
validation error or a missing room will fail identically forever and is
returned immediately. Retrying a terminal failure burns the conversation's
budget and delays the escalation that the user actually needs.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config import settings
from app.domain.exceptions import HuddleError, ToolTimeout

#: Exceptions that are retryable even though they are not ``HuddleError``.
#: Everything else is treated as terminal: an unrecognised exception is a bug,
#: and hammering a bug three times only makes the logs worse.
_RETRYABLE_BUILTINS: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
)


#: HTTP statuses worth another attempt. Everything else a provider returns is
#: a statement about the request, not about the provider's health: a 401 will
#: still be a 401, and a 400 will still be malformed.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})


def http_status_of(error: BaseException) -> int | None:
    """Best-effort status extraction across provider SDK exception shapes."""
    for candidate in (
        getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(error, "code", None),
    ):
        if isinstance(candidate, int):
            return candidate
    return None


def is_retryable(error: BaseException) -> bool:
    if isinstance(error, HuddleError):
        return error.retryable

    # Provider SDK errors are classified by status rather than by type, so a
    # new exception class from an SDK upgrade does not silently become
    # retryable. Checked before the builtin tuple because some SDKs subclass
    # ConnectionError for errors that are anything but transient.
    status = http_status_of(error)
    if status is not None:
        return status in _RETRYABLE_STATUSES or status >= 500

    return isinstance(error, _RETRYABLE_BUILTINS)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    timeout_seconds: float
    base_seconds: float
    multiplier: float
    max_seconds: float
    jitter: float

    @classmethod
    def from_settings(cls) -> RetryPolicy:
        return cls(
            max_attempts=settings.tool_max_attempts,
            timeout_seconds=settings.tool_timeout_seconds,
            base_seconds=settings.tool_backoff_base_seconds,
            multiplier=settings.tool_backoff_multiplier,
            max_seconds=settings.tool_backoff_max_seconds,
            jitter=settings.tool_backoff_jitter,
        )

    def delay_for(self, attempt: int) -> float:
        """Backoff before ``attempt`` + 1, in seconds. ``attempt`` is 1-indexed."""
        raw = self.base_seconds * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.max_seconds)
        if self.jitter <= 0:
            return capped
        # Jitter spreads retries so N agents failing together do not all come
        # back at the same instant and fail together again.
        return capped * (1 + random.uniform(-self.jitter, self.jitter))


@dataclass(slots=True)
class RetryOutcome:
    attempts: int
    errors: list[BaseException]

    @property
    def retried(self) -> bool:
        return self.attempts > 1


class RetriesExhausted(HuddleError):
    """Every attempt failed against a retryable error."""

    retryable = False

    def __init__(self, target: str, attempts: int, last: BaseException) -> None:
        self.target = target
        self.attempts = attempts
        self.last_error = last
        super().__init__(
            f"{target} failed after {attempts} attempts. "
            f"Last error: {type(last).__name__}: {last}"
        )


async def call_with_retry[T](
    target: str,
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    on_attempt: Callable[[int, BaseException | None], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[T, RetryOutcome]:
    """Run ``operation`` under a deadline, retrying only retryable failures.

    Raises the original exception for terminal failures, and
    :class:`RetriesExhausted` when a retryable failure survives every attempt.
    """
    policy = policy or RetryPolicy.from_settings()
    errors: list[BaseException] = []

    for attempt in range(1, policy.max_attempts + 1):
        try:
            result = await asyncio.wait_for(operation(), timeout=policy.timeout_seconds)
        except TimeoutError as error:
            wrapped = ToolTimeout(
                f"{target} exceeded its {policy.timeout_seconds}s deadline"
            )
            wrapped.__cause__ = error
            errors.append(wrapped)
            if on_attempt is not None:
                on_attempt(attempt, wrapped)
            if attempt == policy.max_attempts:
                raise RetriesExhausted(target, attempt, wrapped) from wrapped
            await sleep(policy.delay_for(attempt))
        except BaseException as error:  # classified immediately below
            errors.append(error)
            if on_attempt is not None:
                on_attempt(attempt, error)
            if not is_retryable(error):
                # Terminal. Surface it now; the caller turns it into an
                # actionable message rather than a retry storm.
                raise
            if attempt == policy.max_attempts:
                raise RetriesExhausted(target, attempt, error) from error
            await sleep(policy.delay_for(attempt))
        else:
            if on_attempt is not None:
                on_attempt(attempt, None)
            return result, RetryOutcome(attempts=attempt, errors=errors)

    raise AssertionError("unreachable: retry loop must return or raise")
