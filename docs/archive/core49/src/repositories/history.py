from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from assistant_kernel.repositories.database import AssistantDatabase


def _limits(lookback_days: int, limit: int) -> tuple[int, int]:
    if not 1 <= lookback_days <= 3650:
        raise ValueError("lookback_days must be between 1 and 3650")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    return lookback_days, limit


def _encode_cursor(created_at: datetime, identity: str) -> str:
    raw = json.dumps({"created_at": created_at.isoformat(), "id": identity}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return datetime.fromisoformat(value["created_at"]), str(value["id"])
    except Exception as exc:
        raise ValueError("invalid history cursor") from exc


class HistoryRepository:
    def __init__(self, database: AssistantDatabase):
        self.database = database

    async def list_project_conversations(
        self,
        *,
        project_id: str | None,
        lookback_days: int = 365,
        limit: int = 30,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        lookback_days, limit = _limits(lookback_days, limit)
        before_at, before_id = _decode_cursor(cursor)
        async with self.database.connection() as connection:
            rows = await (
                await connection.execute(
                    """
                    WITH scoped AS (
                        SELECT m.conversation_id, min(m.created_at) AS first_message_at,
                               max(m.created_at) AS last_message_at, count(*) AS message_count
                        FROM assistant.messages m
                        WHERE m.project_id_snapshot IS NOT DISTINCT FROM %s
                          AND m.created_at >= now() - (%s * interval '1 day')
                        GROUP BY m.conversation_id
                    )
                    SELECT s.*,c.frontend,c.user_id,c.channel_conversation_id,c.status,
                           latest.content AS latest_content,latest.message_id AS latest_message_id
                    FROM scoped s
                    JOIN assistant.conversations c ON c.conversation_id=s.conversation_id
                    LEFT JOIN LATERAL (
                        SELECT m.message_id,m.content FROM assistant.messages m
                        WHERE m.conversation_id=s.conversation_id
                          AND m.project_id_snapshot IS NOT DISTINCT FROM %s
                        ORDER BY m.created_at DESC,m.message_id DESC LIMIT 1
                    ) latest ON true
                    WHERE (%s::timestamptz IS NULL OR
                           (s.last_message_at,s.conversation_id) < (%s::timestamptz,%s::uuid))
                    ORDER BY s.last_message_at DESC,s.conversation_id DESC
                    LIMIT %s
                    """,
                    (project_id, lookback_days, project_id, before_at, before_at, before_id, limit + 1),
                )
            ).fetchall()
        has_more = len(rows) > limit
        items = list(rows[:limit])
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last["last_message_at"], str(last["conversation_id"]))
        return {"items": items, "next_cursor": next_cursor, "lookback_days": lookback_days}

    async def search_project_history(
        self,
        *,
        project_id: str | None,
        query: str,
        lookback_days: int = 365,
        limit: int = 30,
        cursor: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        lookback_days, limit = _limits(lookback_days, limit)
        if not query.strip():
            raise ValueError("query cannot be blank")
        before_at, before_id = _decode_cursor(cursor)
        pattern = f"%{query.strip()}%"
        async with self.database.connection() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT m.message_id,m.conversation_id,m.sequence_no,m.direction,
                           m.source_message_id,m.project_id_snapshot,m.agent_id_snapshot,
                           m.content,m.metadata,m.created_at,c.frontend,c.user_id,
                           c.channel_conversation_id
                    FROM assistant.messages m
                    JOIN assistant.conversations c ON c.conversation_id=m.conversation_id
                    WHERE m.project_id_snapshot IS NOT DISTINCT FROM %s
                      AND m.created_at >= now() - (%s * interval '1 day')
                      AND (%s::uuid IS NULL OR m.conversation_id=%s::uuid)
                      AND COALESCE(m.content->>'text','') ILIKE %s
                      AND (%s::timestamptz IS NULL OR
                           (m.created_at,m.message_id) < (%s::timestamptz,%s::uuid))
                    ORDER BY m.created_at DESC,m.message_id DESC
                    LIMIT %s
                    """,
                    (
                        project_id, lookback_days, conversation_id, conversation_id, pattern,
                        before_at, before_at, before_id, limit + 1,
                    ),
                )
            ).fetchall()
        has_more = len(rows) > limit
        items = list(rows[:limit])
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last["created_at"], str(last["message_id"]))
        return {"items": items, "next_cursor": next_cursor, "lookback_days": lookback_days, "query": query}

    async def get_project_messages(
        self,
        *,
        project_id: str | None,
        conversation_id: str,
        lookback_days: int = 365,
        limit: int = 50,
        before_sequence: int | None = None,
    ) -> dict[str, Any]:
        lookback_days, limit = _limits(lookback_days, limit)
        async with self.database.connection() as connection:
            rows = await (
                await connection.execute(
                    """
                    SELECT message_id,conversation_id,sequence_no,direction,external_message_id,
                           source_message_id,project_id_snapshot,agent_id_snapshot,content,metadata,created_at
                    FROM assistant.messages
                    WHERE conversation_id=%s
                      AND project_id_snapshot IS NOT DISTINCT FROM %s
                      AND created_at >= now() - (%s * interval '1 day')
                      AND (%s::bigint IS NULL OR sequence_no < %s::bigint)
                    ORDER BY sequence_no DESC LIMIT %s
                    """,
                    (conversation_id, project_id, lookback_days, before_sequence, before_sequence, limit + 1),
                )
            ).fetchall()
        has_more = len(rows) > limit
        items = list(reversed(rows[:limit]))
        next_before_sequence = int(rows[limit - 1]["sequence_no"]) if has_more else None
        return {
            "items": items,
            "next_before_sequence": next_before_sequence,
            "lookback_days": lookback_days,
        }

    async def get_message_original(
        self, *, project_id: str | None, message_id: str
    ) -> dict[str, Any] | None:
        async with self.database.connection() as connection:
            return await (
                await connection.execute(
                    """
                    SELECT m.*,c.frontend,c.user_id,c.channel_conversation_id
                    FROM assistant.messages m
                    JOIN assistant.conversations c ON c.conversation_id=m.conversation_id
                    WHERE m.message_id=%s AND m.project_id_snapshot IS NOT DISTINCT FROM %s
                    """,
                    (message_id, project_id),
                )
            ).fetchone()

