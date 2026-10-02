from __future__ import annotations

from assistant_kernel.mcp_tools.context import AgentMcpContext
from assistant_kernel.services.stage_protocol import register_stage_intent


def register(server, context: AgentMcpContext) -> None:
    async def submit(intent: dict) -> dict:
        document = context.document
        if not all((document.job_id, document.worker_instance_id, document.node_run_id, document.turn_key)):
            raise PermissionError("stage interaction tools require a bound stage job")
        return await register_stage_intent(
            context.settings,
            job_id=document.job_id,
            worker_instance_id=document.worker_instance_id,
            node_run_id=document.node_run_id,
            turn_key=document.turn_key,
            intent=intent,
        )

    if context.allows("ask_user"):
        @server.tool()
        async def ask_user(questions: list[dict], draft_markdown: str, next_action: str) -> dict:
            async with context.audit("ask_user", {"questions": questions, "next_action": next_action}):
                return await submit({"kind": "input", "questions": questions, "document": draft_markdown, "next_action": next_action})

    if context.allows("submit_stage_for_confirmation"):
        @server.tool()
        async def submit_stage_for_confirmation(document_markdown: str, question_text: str) -> dict:
            async with context.audit("submit_stage_for_confirmation", {"question_text": question_text}):
                return await submit(
                    {
                        "kind": "confirmation",
                        "document": document_markdown,
                        "questions": [{"key": "confirm-document", "text": question_text, "required": True}],
                        "next_action": "Wait for explicit confirmation of this exact document.",
                    }
                )

