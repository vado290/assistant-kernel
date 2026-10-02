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
    """Evaluates context occupancy once after each completed turn.

    input_tokens already includes cached input; cached_input_tokens is a subset and
    must not be added again. Rotation is strict greater-than the configured soft limit.
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
        hard = ratio > self.hard_ratio
        rotate = ratio > self.soft_ratio
        if hard:
            reason = "hard context threshold exceeded"
        elif rotate:
            reason = "soft context threshold exceeded"
        else:
            reason = "context remains below or equal to the soft threshold"
        return ContextBudgetDecision(
            ratio=ratio,
            should_summarize_and_rotate=rotate,
            hard_limit_reached=hard,
            reason=reason,
        )
