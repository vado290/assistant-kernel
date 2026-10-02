from __future__ import annotations

from assistant_kernel.mcp_tools.context import AgentMcpContext
from assistant_kernel.repositories import ControlPlaneRepository


def register(server, context: AgentMcpContext) -> None:
    if context.allows("list_projects"):
        @server.tool()
        async def list_projects() -> dict:
            async with context.audit("list_projects", {}):
                repository = ControlPlaneRepository(context.settings)
                return {"projects": await repository.list_projects()}

