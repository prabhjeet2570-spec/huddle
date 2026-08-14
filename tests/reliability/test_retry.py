"""Retry policy: what gets retried, what does not, and what stops."""

from __future__ import annotations

import asyncio

import pytest

from app.domain.exceptions import (
    MalformedToolResponse,
    RoomNotFound,
    ToolTimeout,
    UpstreamUnavailable,
)
from app.reliability.retry import (
    RetriesExhausted,
    RetryPolicy,
    call_with_retry,
    is_retryable,
)

FAST = RetryPolicy(
    max_attempts=3,
    timeout_seconds=0.5,
    base_seconds=0.0,
    multiplier=2.0,
    max_seconds=0.0,
    jitter=0.0,
)


async def _noop_sleep(_seconds: float) -> None:
    return None


# --- Classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ToolTimeout("slow"), True),
        (UpstreamUnavailable("502"), True),
        (MalformedToolResponse("garbage"), True),
        (TimeoutError(), True),
        (ConnectionError(), True),
        (RoomNotFound("Z"), False),
        (ValueError("bad argument"), False),
        (KeyError("missing"), False),
    ],
)
def test_classification_of_failures(error, expected):
    assert is_retryable(error) is expected


class _ProviderError(Exception):
    """Shaped like an LLM SDK error: carries an HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"provider returned {status_code}")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, True),  # rate limited: back off and come back
        (500, True),
        (502, True),
        (503, True),
        (408, True),
        (401, False),  # bad key: will be a 401 forever
        (403, False),
        (400, False),  # malformed request
        (404, False),
        (422, False),
    ],
)
def test_provider_errors_are_classified_by_http_status(status, expected):
    assert is_retryable(_ProviderError(status)) is expected


def test_status_classification_beats_the_builtin_tuple():
    """Some SDKs subclass ConnectionError for errors that are not transient."""

    class SdkConnectionError(ConnectionError):
        status_code = 401

    assert is_retryable(SdkConnectionError()) is False


# --- Behaviour -------------------------------------------------------------


async def test_a_retryable_failure_is_retried_until_it_succeeds():
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise UpstreamUnavailable("503")
        return "ok"

    result, outcome = await call_with_retry(
        "flaky", flaky, policy=FAST, sleep=_noop_sleep
    )

    assert result == "ok"
    assert outcome.attempts == 3
    assert attempts == 3


async def test_a_terminal_failure_is_not_retried():
    """The whole point of classification: a bad room name never improves."""
    attempts = 0

    async def doomed() -> str:
        nonlocal attempts
        attempts += 1
        raise RoomNotFound("Z")

    with pytest.raises(RoomNotFound):
        await call_with_retry("doomed", doomed, policy=FAST, sleep=_noop_sleep)

    assert attempts == 1


async def test_retries_are_bounded_and_then_raise():
    attempts = 0

    async def always_failing() -> str:
        nonlocal attempts
        attempts += 1
        raise UpstreamUnavailable("503")

    with pytest.raises(RetriesExhausted) as raised:
        await call_with_retry(
            "always_failing", always_failing, policy=FAST, sleep=_noop_sleep
        )

    assert attempts == FAST.max_attempts
    assert raised.value.attempts == FAST.max_attempts
    assert isinstance(raised.value.last_error, UpstreamUnavailable)


async def test_a_hanging_call_is_cut_off_by_the_timeout():
    async def hangs() -> str:
        await asyncio.sleep(10)
        return "never"

    policy = RetryPolicy(
        max_attempts=2,
        timeout_seconds=0.05,
        base_seconds=0.0,
        multiplier=1.0,
        max_seconds=0.0,
        jitter=0.0,
    )

    with pytest.raises(RetriesExhausted) as raised:
        await call_with_retry("hangs", hangs, policy=policy, sleep=_noop_sleep)

    assert isinstance(raised.value.last_error, ToolTimeout)


async def test_a_call_that_succeeds_first_time_reports_one_attempt():
    async def instant() -> int:
        return 42

    result, outcome = await call_with_retry(
        "instant", instant, policy=FAST, sleep=_noop_sleep
    )

    assert result == 42
    assert outcome.attempts == 1
    assert outcome.retried is False


# --- Backoff ---------------------------------------------------------------


def test_backoff_grows_exponentially_and_is_capped():
    policy = RetryPolicy(
        max_attempts=8,
        timeout_seconds=1.0,
        base_seconds=0.1,
        multiplier=2.0,
        max_seconds=1.0,
        jitter=0.0,
    )

    delays = [policy.delay_for(attempt) for attempt in range(1, 8)]

    assert delays[:4] == [0.1, 0.2, 0.4, 0.8]
    assert all(delay <= policy.max_seconds for delay in delays)
    assert delays[-1] == policy.max_seconds


def test_jitter_stays_within_its_band():
    policy = RetryPolicy(
        max_attempts=5,
        timeout_seconds=1.0,
        base_seconds=1.0,
        multiplier=1.0,
        max_seconds=10.0,
        jitter=0.1,
    )

    samples = [policy.delay_for(1) for _ in range(200)]

    assert all(0.9 <= sample <= 1.1 for sample in samples)
    assert len(set(samples)) > 1  # actually jittering, not a constant
