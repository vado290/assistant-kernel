"""Test doubles for the rebuilt core: a scripted model harness."""
from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

from assistant_kernel.harness.base import AgentTurnRequest, AgentTurnResult
from assistant_kernel.services.context_budget import TurnUsage


class FakeHarness:
    """Return scripted final outputs instead of calling a model.

    Scripts are keyed by an arbitrary string the executor puts into
    ``request.scope_context['script_key']`` (e.g. ``"pm:requirements"`` or
    ``"supervisor:evaluate"``) and consumed in order. Every request is kept in
    ``calls`` so tests can assert on what the model would have seen.
    """

    name = "fake"

    def __init__(self) -> None:
        self.scripts: dict[str, deque[Any]] = defaultdict(deque)
        self.calls: list[AgentTurnRequest] = []

    def script(self, key: str, *outputs: Any) -> "FakeHarness":
        self.scripts[key].extend(outputs)
        return self

    async def run_turn(self, request: AgentTurnRequest) -> AgentTurnResult:
        self.calls.append(request)
        key = request.scope_context.get("script_key", "")
        if not self.scripts[key]:
            raise AssertionError(f"no scripted model output left for {key!r}")
        output = self.scripts[key].popleft()
        if isinstance(output, BaseException):
            raise output
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return AgentTurnResult(
            physical_session_id=request.physical_session_id or f"fake-session-{len(self.calls)}",
            usage=TurnUsage(input_tokens=100, output_tokens=50),
            events=[],
            final_text=text,
            provider="fake",
            model="fake",
        )
