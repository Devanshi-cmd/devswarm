from devswarm.llm.pricing import TokenUsage
from devswarm.orchestrator.budget import Budget, BudgetLimits


def test_fresh_budget_is_not_exceeded() -> None:
    assert Budget().exceeded() is None


def test_cost_limit() -> None:
    budget = Budget(BudgetLimits(max_usd=1.0))
    budget.charge(TokenUsage(), 0.99)
    assert budget.exceeded() is None
    budget.charge(TokenUsage(), 0.01)
    assert "cost budget" in (budget.exceeded() or "")


def test_token_limit_ignores_cache_reads() -> None:
    budget = Budget(BudgetLimits(max_tokens=100))
    budget.charge(TokenUsage(cache_read_input_tokens=1_000_000), 0.0)
    assert budget.exceeded() is None
    budget.charge(TokenUsage(input_tokens=50, output_tokens=25, cache_creation_input_tokens=25), 0)
    assert "token budget" in (budget.exceeded() or "")


def test_time_limit() -> None:
    now = [0.0]
    budget = Budget(BudgetLimits(max_seconds=10), clock=lambda: now[0])
    now[0] = 9.9
    assert budget.exceeded() is None
    now[0] = 10.0
    assert "time budget" in (budget.exceeded() or "")
