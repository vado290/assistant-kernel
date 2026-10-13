from __future__ import annotations

from pydantic import BaseModel, Field


class TurnUsage(BaseModel):
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)


class ContextBudgetDecision(BaseModel):
    ratio: float
    should_summarize_and_rotate: bool
    hard_limit_reached: bool
    reason: str


class ContextBudgetManager:
    """Compatibility diagnostic only; native Codex compact owns context capacity.

    Cached input is already included in input_tokens. The legacy response fields
    remain readable, but never authorize a Kernel summary or session rotation.
    """

    def __init__(self, soft_ratio: float = 0.70, hard_ratio: float = 0.85):
        if not 0 < soft_ratio < hard_ratio < 1:
            raise ValueError("context ratios must satisfy 0 < soft < hard < 1")
        self.soft_ratio = soft_ratio
        self.hard_ratio = hard_ratio

    def evaluate(self, usage: TurnUsage, context_window_tokens: int) -> ContextBudgetDecision:
        if context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive")
        ratio = usage.input_tokens / context_window_tokens
        return ContextBudgetDecision(
            ratio=ratio,
            should_summarize_and_rotate=False,
            hard_limit_reached=False,
            reason="occupancy diagnostic only; Codex native compact owns context capacity",
        )
