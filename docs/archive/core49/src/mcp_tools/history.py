from __future__ import annotations

from assistant_kernel.mcp_tools.context import AgentMcpContext
from assistant_kernel.repositories.history import HistoryRepository


def register(server, context: AgentMcpContext) -> None:
    repository = HistoryRepository(context.database)

    if context.allows("list_project_conversations"):
        @server.tool()
        async def list_project_conversations(
            lookback_days: int | None = None, limit: int | None = None, cursor: str | None = None
        ) -> dict:
            defaults = context.defaults("list_project_conversations")
            days = int(lookback_days or defaults.get("lookback_days", 365))
            count = int(limit or defaults.get("limit", 30))
            async with context.audit("list_project_conversations", {"lookback_days": days, "limit": count, "cursor": cursor}):
                return await repository.list_project_conversations(
                    project_id=context.project_id, lookback_days=days, limit=count, cursor=cursor
                )

    if context.allows("search_project_history"):
        @server.tool()
        async def search_project_history(
            query: str,
            lookback_days: int | None = None,
            conversation_id: str | None = None,
            limit: int | None = None,
            cursor: str | None = None,
        ) -> dict:
            defaults = context.defaults("search_project_history")
            days = int(lookback_days or defaults.get("lookback_days", 365))
            count = int(limit or defaults.get("limit", 30))
            async with context.audit("search_project_history", {"query": query, "lookback_days": days, "conversation_id": conversation_id, "limit": count, "cursor": cursor}):
                return await repository.search_project_history(
                    project_id=context.project_id, query=query, lookback_days=days,
                    conversation_id=conversation_id, limit=count, cursor=cursor,
                )

    if context.allows("get_project_messages"):
        @server.tool()
        async def get_project_messages(
            conversation_id: str,
            lookback_days: int | None = None,
            before_sequence: int | None = None,
            limit: int | None = None,
        ) -> dict:
            defaults = context.defaults("get_project_messages")
            days = int(lookback_days or defaults.get("lookback_days", 365))
            count = int(limit or defaults.get("limit", 50))
            async with context.audit("get_project_messages", {"conversation_id": conversation_id, "lookback_days": days, "before_sequence": before_sequence, "limit": count}):
                return await repository.get_project_messages(
                    project_id=context.project_id, conversation_id=conversation_id,
                    lookback_days=days, before_sequence=before_sequence, limit=count,
                )

    if context.allows("get_message_original"):
        @server.tool()
        async def get_message_original(message_id: str) -> dict:
            async with context.audit("get_message_original", {"message_id": message_id}):
                row = await repository.get_message_original(project_id=context.project_id, message_id=message_id)
                if row is None:
                    raise FileNotFoundError("message is not visible in the effective project scope")
                return row

