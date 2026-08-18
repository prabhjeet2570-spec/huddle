"""The deterministic confirmation matcher.

It gates irreversible actions, so it must fail closed. A reply it cannot read
confidently is ambiguous, and the user gets asked again.
"""

from __future__ import annotations

import pytest

from app.agent.confirmation import ConfirmationVerdict, classify_confirmation


@pytest.mark.parametrize(
    "message",
    [
        "yes",
        "Yes",
        "YES",
        "yep",
        "confirm",
        "Confirm it",
        "go ahead",
        "book it",
        "ok",
        "okay",
        "sure",
        "sounds good",
        "yes please",
        "please do",
        "that works",
        "proceed",
    ],
)
def test_affirmatives_are_recognised(message):
    assert classify_confirmation(message) is ConfirmationVerdict.AFFIRM


@pytest.mark.parametrize(
    "message",
    [
        "no",
        "No",
        "nope",
        "cancel",
        "cancel that",
        "stop",
        "not yet",
        "never mind",
        "no thanks",
        "don't",
        "abort",
        "change it",
    ],
)
def test_declines_are_recognised(message):
    assert classify_confirmation(message) is ConfirmationVerdict.DECLINE


@pytest.mark.parametrize(
    "message",
    [
        "",
        "maybe",
        "hmm",
        "what time was that again",
        # The important one: a compound instruction is not a confirmation,
        # even though it starts with "yes".
        "yes but move it to room B and make it an hour later",
        "yes if room A is still free otherwise pick another room",
        "I do not want to confirm that",
    ],
)
def test_anything_unclear_fails_closed(message):
    assert classify_confirmation(message) is ConfirmationVerdict.AMBIGUOUS
