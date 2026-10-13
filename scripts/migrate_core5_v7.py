"""Controlled schema switch; no business Run or waiting interaction is rewritten."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase


async def migrate(downgrade: bool = False) -> dict:
    database = AssistantDatabase(KernelSettings.from_environment())
    async with database.connection() as connection:
        previous_schema = (await (await connection.execute(
            "SELECT value FROM assistant.kernel_meta WHERE key='schema_version'"
        )).fetchone())["value"]
        if previous_schema not in {"core5.v6", "core5.v7"}:
            raise RuntimeError(f"unsupported schema switch: {previous_schema}")
        blockers = list(await (await connection.execute(
            "SELECT run_id,status FROM assistant.runs WHERE status IN ('running','reconciling')"
        )).fetchall())
        if blockers:
            raise RuntimeError(f"schema switch refused: {[(str(r['run_id']),r['status']) for r in blockers]}")
        if downgrade:
            pending = list(await (await connection.execute(
                "SELECT message_id,run_id FROM assistant.messages WHERE content_json->>'task_input_status'='pending'"
            )).fetchall())
            if pending:
                raise RuntimeError(f"downgrade refused; pending inputs retained: {[str(r['message_id']) for r in pending]}")
        else:
            old_stages = list(await (await connection.execute(
                "SELECT run_id FROM assistant.runs WHERE run_type='stage' AND status NOT IN ('succeeded','failed','cancelled','dead_letter')"
            )).fetchall())
            if old_stages:
                raise RuntimeError(f"retired Stage Runs need explicit resolution: {[str(r['run_id']) for r in old_stages]}")
        waiting_sql = """SELECT run_id,md5(jsonb_build_object('status',status,'interaction_seq',interaction_seq,
            'question',waiting_question_message_id,'request',request_json,'result',result_json)::text) AS signature
            FROM assistant.runs WHERE status='waiting_user' ORDER BY run_id"""
        before = list(await (await connection.execute(waiting_sql)).fetchall())
        await connection.commit()
        if downgrade:
            await connection.execute("""DROP INDEX IF EXISTS assistant.runs_one_active_mutex;
                CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_execution_project ON assistant.runs(project_id)
                WHERE status IN ('running','reconciling') AND assistant.run_lane(run_type,agent_key,executor_json->>'scheduling_lane')='execution';
                UPDATE assistant.kernel_meta SET value='"core5.v6"'::jsonb,updated_at=now() WHERE key='schema_version';""")
            await connection.commit()
        else:
            await connection.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v7.sql').read_text())
        after = list(await (await connection.execute(waiting_sql)).fetchall())
        if before != after:
            raise RuntimeError("waiting interactions changed; keep services stopped and inspect backup")
        return {'previous_schema':previous_schema, 'schema_version':'core5.v6' if downgrade else 'core5.v7',
                'waiting_preserved':len(after), 'waiting_signatures':[{**r,'run_id':str(r['run_id'])} for r in after]}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--downgrade',action='store_true')
    parser.add_argument('--receipt')
    args=parser.parse_args()
    result=asyncio.run(migrate(args.downgrade))
    if args.receipt:
        Path(args.receipt).write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
