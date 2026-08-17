"""Per-conversation budgets on tool calls and tokens.

An LLM that cannot converge does not stop on its own: it keeps calling tools,
each call costs money and touches shared state, and the user sees nothing. The
budget is the floor under that. Breaching it halts the conversation and
escalates rather than degrading quietly.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.domain.exceptions import BudgetExceeded


@dataclass(slots=True)
class Budget:
    """Accounting for one conversation. Mutated as the turn proceeds."""

    max_tool_calls: int
    max_tokens: int
    tool_calls_used: int = 0
    tokens_used: int = 0

    @classmethod
    def from_settings(cls, tool_calls_used: int = 0, tokens_used: int = 0) -> Budget:
        return cls(
            max_tool_calls=settings.max_tool_calls_per_conversation,
            max_tokens=settings.max_tokens_per_conversation,
            tool_calls_used=tool_calls_used,
            tokens_used=tokens_used,
        )

    @property
    def tool_calls_remaining(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls_used)

    @property
    def tokens_remaining(self) -> int:
        return max(0, self.max_tokens - self.tokens_used)

    def breach(self) -> BudgetExceeded | None:
        """Return the breach, if any, without raising. Used by the graph."""
        if self.tool_calls_used >= self.max_tool_calls:
            return BudgetExceeded(
                "tool call", self.tool_calls_used, self.max_tool_calls
            )
        if self.tokens_used >= self.max_tokens:
            return BudgetExceeded("token", self.tokens_used, self.max_tokens)
        return None

    def check(self) -> None:
        breach = self.breach()
        if breach is not None:
            raise breach

    def charge_tool_call(self, count: int = 1) -> None:
        self.tool_calls_used += count

    def charge_tokens(self, prompt: int, completion: int) -> None:
        self.tokens_used += prompt + completion
