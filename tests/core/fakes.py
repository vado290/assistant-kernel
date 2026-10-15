"""Test doubles for the rebuilt core: a scripted model harness."""
from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

from assistant_kernel.harness.base import AgentTurnRequest, AgentTurnResult
from assistant_kernel.harness.context_budget import TurnUsage


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
        self.session_keys: dict[str, str] = {}

    def script(self, key: str, *outputs: Any) -> "FakeHarness":
        self.scripts[key].extend(outputs)
        return self

    def key_for(self, request: AgentTurnRequest) -> str:
        if "script_key" in request.scope_context:
            return request.scope_context["script_key"]
        if request.physical_session_id in self.session_keys:   # a resumed session keeps its role
            return self.session_keys[request.physical_session_id]
        return "pm" if request.prompt.startswith("# 项目 PM") else "supervisor"

    async def run_turn(self, request: AgentTurnRequest) -> AgentTurnResult:
        self.calls.append(request)
        key = self.key_for(request)
        if not self.scripts[key]:
            raise AssertionError(f"no scripted model output left for {key!r}")
        output = self.scripts[key].popleft()
        if isinstance(output, BaseException):
            raise output
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        session_id = request.physical_session_id or f"fake-session-{len(self.calls)}"
        self.session_keys[session_id] = key
        return AgentTurnResult(
            physical_session_id=session_id,
            usage=TurnUsage(input_tokens=100, output_tokens=50),
            events=[],
            final_text=text,
            provider="fake",
            model="fake",
        )
