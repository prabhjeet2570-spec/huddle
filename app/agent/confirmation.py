"""Deciding whether a user message confirms a pending action.

This is deliberately not an LLM call. The confirmation gate exists precisely
because the model's judgement is not trusted for irreversible actions, so
routing the decision back through the model would defeat it. A deterministic
matcher is auditable, free, instant, and cannot be talked into a "yes".

The cost of that choice is coverage: an unusual phrasing reads as ambiguous
and the user is simply asked again. Failing closed is the correct direction
for an action nobody can undo.
"""

from __future__ import annotations

import re
from enum import StrEnum


class ConfirmationVerdict(StrEnum):
    AFFIRM = "affirm"
    DECLINE = "decline"
    AMBIGUOUS = "ambiguous"


_AFFIRM = {
    "yes",
    "y",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "correct",
    "right",
    "agreed",
    "affirmative",
    "please",
    "do it",
    "go ahead",
    "book it",
    "book that",
    "go for it",
    "sounds good",
    "looks good",
    "that works",
    "perfect",
    "great",
    "lets do it",
    "let's do it",
    "confirm it",
    "yes please",
    "please do",
    "yes confirm",
    "confirm booking",
    "make it",
    "proceed",
    "approve",
    "approved",
}

_DECLINE = {
    "no",
    "n",
    "nope",
    "nah",
    "cancel",
    "cancel that",
    "stop",
    "dont",
    "don't",
    "do not",
    "abort",
    "never mind",
    "nevermind",
    "wait",
    "not yet",
    "hold on",
    "no thanks",
    "forget it",
    "negative",
    "change it",
}

_NEGATION = re.compile(r"\b(not|don'?t|do not|never|no longer)\b")


def _normalize(text: str) -> str:
    cleaned = re.sub(r"[^\w\s']", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def classify_confirmation(message: str) -> ConfirmationVerdict:
    """Classify a reply to "please confirm". Fails closed on ambiguity."""
    text = _normalize(message)
    if not text:
        return ConfirmationVerdict.AMBIGUOUS

    if text in _DECLINE:
        return ConfirmationVerdict.DECLINE
    if text in _AFFIRM:
        return ConfirmationVerdict.AFFIRM

    words = text.split()
    # Only short replies are treated as answers to the question. A long
    # message is a new instruction that happens to contain "yes", and running
    # an irreversible action off it would be wrong.
    if len(words) > 6:
        return ConfirmationVerdict.AMBIGUOUS

    leading = " ".join(words[:3])
    for phrase in _DECLINE:
        if leading.startswith(phrase):
            return ConfirmationVerdict.DECLINE

    if _NEGATION.search(text):
        return ConfirmationVerdict.AMBIGUOUS

    for phrase in _AFFIRM:
        if leading.startswith(phrase):
            return ConfirmationVerdict.AFFIRM

    return ConfirmationVerdict.AMBIGUOUS
