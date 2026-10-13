from assistant_kernel.services.context_budget import ContextBudgetManager, TurnUsage


def test_does_not_rotate_at_exactly_seventy_percent() -> None:
    decision = ContextBudgetManager().evaluate(TurnUsage(input_tokens=70), 100)
    assert decision.should_summarize_and_rotate is False


def test_occupancy_is_only_diagnostic_without_double_counting_cached_tokens() -> None:
    decision = ContextBudgetManager().evaluate(
        TurnUsage(input_tokens=71, cached_input_tokens=40), 100
    )
    assert decision.should_summarize_and_rotate is False
    assert decision.ratio == 0.71
