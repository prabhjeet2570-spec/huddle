"""Per-conversation budgets."""

from __future__ import annotations

import pytest

from app.domain.exceptions import BudgetExceeded
from app.reliability.budget import Budget


def test_a_fresh_budget_permits_work():
    budget = Budget(max_tool_calls=5, max_tokens=100)

    assert budget.breach() is None
    assert budget.tool_calls_remaining == 5
    budget.check()


def test_the_tool_call_budget_trips_on_the_limit():
    budget = Budget(max_tool_calls=3, max_tokens=1000)

    for _ in range(3):
        assert budget.breach() is None
        budget.charge_tool_call()

    breach = budget.breach()
    assert isinstance(breach, BudgetExceeded)
    assert breach.resource == "tool call"
    assert budget.tool_calls_remaining == 0
    with pytest.raises(BudgetExceeded):
        budget.check()


def test_the_token_budget_trips_independently():
    budget = Budget(max_tool_calls=100, max_tokens=500)

    budget.charge_tokens(300, 150)
    assert budget.breach() is None

    budget.charge_tokens(40, 20)
    breach = budget.breach()
    assert isinstance(breach, BudgetExceeded)
    assert breach.resource == "token"
    assert breach.used == 510


def test_a_budget_resumes_from_prior_conversation_usage():
    """Budgets are per conversation, not per turn, so they must carry over."""
    budget = Budget(max_tool_calls=10, max_tokens=1000, tool_calls_used=9)

    assert budget.breach() is None
    budget.charge_tool_call()
    assert isinstance(budget.breach(), BudgetExceeded)
