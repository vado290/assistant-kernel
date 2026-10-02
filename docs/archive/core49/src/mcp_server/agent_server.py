from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from assistant_kernel.mcp_tools import load_agent_mcp_context
from assistant_kernel.mcp_tools import capabilities, history, interactions, projects, workspace


def build_server() -> MCPServer:
    context = load_agent_mcp_context()
    server = MCPServer(
        name="kernel-agent-tools",
        instructions=(
            "Use only the tools exposed for this invocation. Project identity and scope are "
            "already bound; tool arguments cannot switch projects or expand permissions."
        ),
    )
    workspace.register(server, context)
    history.register(server, context)
    projects.register(server, context)
    capabilities.register(server, context)
    interactions.register(server, context)
    return server


def run() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    run()

