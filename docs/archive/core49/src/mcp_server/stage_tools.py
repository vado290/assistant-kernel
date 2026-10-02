"""Per-invocation stdio MCP. The caller cannot select another job or worker."""
from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.stage_protocol import register_stage_intent


server = MCPServer(name="kernel-stage-tools", instructions="Register one final intent, then end your turn. These tools never wait for the user or approve on their behalf.")


async def submit(intent):
    return await register_stage_intent(KernelSettings.from_environment(),
        job_id=os.environ["KERNEL_STAGE_JOB_ID"], worker_instance_id=os.environ["KERNEL_STAGE_WORKER_ID"],
        node_run_id=os.environ["KERNEL_STAGE_NODE_ID"], turn_key=os.environ["KERNEL_STAGE_TURN_KEY"], intent=intent)


@server.tool()
async def ask_user(questions: list[dict], draft_markdown: str, next_action: str) -> dict:
    """Ask 1-8 questions with immutable full text. Each has key, text, required. Save the complete draft, register, then end the turn; do not wait inside the tool."""
    return await submit({"kind": "input", "questions": questions, "document": draft_markdown, "next_action": next_action})


@server.tool()
async def submit_stage_for_confirmation(document_markdown: str, question_text: str) -> dict:
    """Submit the complete requirements/design/optimization proposal for human approval. Kernel binds its version/hash; this tool does not approve or start the next stage."""
    return await submit({"kind": "confirmation", "document": document_markdown,
                         "questions": [{"key": "confirm-document", "text": question_text, "required": True}],
                         "next_action": "Wait for explicit confirmation of this exact document."})


if __name__ == "__main__":
    server.run(transport="stdio")
