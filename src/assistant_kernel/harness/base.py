from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol
import asyncio

from assistant_kernel.services.context_budget import TurnUsage


@dataclass(frozen=True)
class AgentTurnRequest:
    project_id: str
    prompt: str
    cwd: Path
    physical_session_id: str | None = None
    ephemeral: bool = False
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    context_policy: str = "model_default"
    model_context_window_tokens: int | None = None
    model_auto_compact_token_limit: int | None = None
    sandbox: str = "read-only"
    permission_profile: str | None = None
    permission_profile_sha256: str | None = None
    timeout_seconds: int | None = None
    invocation_id: str | None = None
    cancel_event: asyncio.Event | None = None
    scoped_tools: tuple[str, ...] = ()
    scope_context: dict[str, str] = field(default_factory=dict)
    extra_environment: dict[str, str] = field(default_factory=dict)
    writable_roots: tuple[Path, ...] = ()
    mcp_servers: tuple[dict, ...] = ()
    multi_agent_enabled: bool = False
    max_concurrent_subagents: int = 3
    process_started_callback: Callable[[int, int | None], Awaitable[None]] | None = None


@dataclass(frozen=True)
class AgentTurnResult:
    physical_session_id: str
    usage: TurnUsage
    events: list[dict]
    final_text: str
    archive_uri: str | None = None
    archive_sha256: str | None = None
    provider: str | None = None
    model: str | None = None
    metadata: dict = field(default_factory=dict)


class HarnessAdapter(Protocol):
    name: str

    async def run_turn(self, request: AgentTurnRequest) -> AgentTurnResult: ...
