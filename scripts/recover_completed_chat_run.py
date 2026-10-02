"""Finalize a reconciled Chat Run from its completed Codex trace."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository


def final_agent_text(trace: Path) -> str:
    result = ""
    for line in trace.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        payload = item.get("item") if item.get("type") == "item.completed" else None
        if isinstance(payload, dict) and payload.get("type") == "agent_message":
            result = str(payload.get("text") or "").strip()
    if not result:
        raise ValueError("completed trace has no final agent message")
    return result


async def run(args: argparse.Namespace) -> None:
    settings = KernelSettings.from_environment()
    repository = CoreRepository(settings)
    summary = final_agent_text(Path(args.trace))
    async with repository.database.connection() as connection:
        row = await (await connection.execute(
            """
            SELECT r.*,m.channel,m.chat_id,m.user_id
            FROM assistant.runs r JOIN assistant.messages m
              ON m.message_id=r.trigger_message_id
            WHERE r.run_id=%s AND r.status='reconciling' FOR UPDATE OF r
            """,
            (args.run_id,),
        )).fetchone()
        if row is None:
            raise ValueError("Run is not waiting for reconciliation")
        pid = int(row.get("process_pid") or 0)
        if pid and Path(f"/proc/{pid}").exists():
            raise RuntimeError("Run process is still alive")
        prior = await (await connection.execute(
            """
            SELECT message_id FROM assistant.messages
            WHERE project_id=%s AND channel=%s AND chat_id=%s
              AND direction='outbound' AND message_type='reply'
              AND reply_to_message_id=%s
            ORDER BY created_at DESC,message_id DESC LIMIT 1
            """,
            (row["project_id"], row["channel"], row["chat_id"], row["trigger_message_id"]),
        )).fetchone()
        if prior is None:
            raise ValueError("recovery requires the prior failure reply")
        message_id = str(uuid.uuid4())
        reply = await (await connection.execute(
            """
            INSERT INTO assistant.messages(
                message_id,project_id,run_id,channel,chat_id,user_id,direction,
                message_type,reply_to_message_id,content_json,delivery_status,idempotency_key
            ) VALUES (%s,%s,%s,%s,%s,%s,'outbound','reply',%s,%s,'pending',%s)
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
            DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key RETURNING *
            """,
            (
                message_id,row["project_id"],args.run_id,row["channel"],row["chat_id"],
                row["user_id"],prior["message_id"],Jsonb({"text": summary}),
                f"recovery-completion:{args.run_id}",
            ),
        )).fetchone()
        result = {
            "outcome": "completed", "summary": summary,
            "recovered_from_trace": str(Path(args.trace)),
            "next_run_id": args.next_run_id,
        }
        await connection.execute(
            """
            UPDATE assistant.runs
            SET status='succeeded',result_json=%s,ended_at=now(),updated_at=now(),
                lease_until=NULL,worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,
                process_pid=NULL,process_start_ticks=NULL,
                error_json=COALESCE(error_json,'{}'::jsonb) || %s
            WHERE run_id=%s AND status='reconciling'
            """,
            (
                Jsonb(result), Jsonb({
                    "reconciliation_note": "completed model result recovered and follow-up queued"
                }), args.run_id,
            ),
        )
        await connection.commit()
    print(json.dumps({
        "run_id": args.run_id, "status": "succeeded",
        "reply_message_id": str(reply["message_id"]),
        "next_run_id": args.next_run_id,
    }))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--next-run-id")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
