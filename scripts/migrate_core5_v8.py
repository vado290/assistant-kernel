"""Dry-run by default; controlled change-ledger backfill, preserving all old messages."""
from __future__ import annotations
import argparse,asyncio,json
from pathlib import Path
from psycopg.types.json import Jsonb
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.repositories.change_ledger import append, LEDGER_KEY

WAIT_SQL="""SELECT run_id,md5(jsonb_build_object('status',status,'question',waiting_question_message_id,
 'interaction_seq',interaction_seq,'request',request_json,'result',result_json)::text) AS signature
 FROM assistant.runs WHERE status IN ('waiting_user','waiting_supervisor','blocked') ORDER BY run_id"""

async def migrate(settings=None, *, apply=False):
    settings=settings or KernelSettings.from_environment();db=AssistantDatabase(settings)
    async with db.connection() as c:
        await c.execute("SELECT pg_advisory_xact_lock(hashtextextended('kernel-schema-migration',0))")
        version=(await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key='schema_version'")).fetchone())["value"]
        if version not in {'core5.v7','core5.v8'}:raise ValueError('unsupported migration source Schema')
        waits=list(await (await c.execute(WAIT_SQL)).fetchall())
        active=list(await (await c.execute("SELECT run_id FROM assistant.runs WHERE status IN ('running','reconciling')")).fetchall())
        legacy=list(await (await c.execute("""SELECT message_id,created_at,content_json FROM assistant.messages
            WHERE direction='system' AND message_type='project_change'
            AND content_json->>'schema_version'='project-change.v1' ORDER BY created_at,message_id""")).fetchall())
        result={'source_schema':version,'target_schema':'core5.v8','apply':apply,
                'legacy_change_count':len(legacy),'waiting_preserved':len(waits),'active_blockers':len(active)}
        if not apply:return result
        if active:raise ValueError('active/reconciling execution prevents this controlled migration')
        if version=='core5.v7':
            # Do not silently accept an unexpected partially-created table.
            if (await (await c.execute("SELECT to_regclass('assistant.project_file_changes') AS name")).fetchone())["name"]:
                raise ValueError('unexpected pre-existing change table; inspect before migration')
            await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v8.sql').read_text())
            for item in legacy:
                stored=await append(c,dict(item['content_json']),legacy_message_id=item['message_id'],legacy_created_at=item['created_at'])
                if stored!=item['content_json']:raise ValueError('historical change content differs after conversion')
            await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s)",
                            (LEDGER_KEY,Jsonb({'write_enabled':True,'backfill_complete':True,'legacy_count':len(legacy)})))
            await c.execute("UPDATE assistant.kernel_meta SET value=%s,updated_at=now() WHERE key='schema_version'",(Jsonb('core5.v8'),))
        columns={r['column_name'] for r in await (await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='assistant' AND table_name='project_file_changes'")).fetchall()}
        if not {'change_id','project_id','record_json','legacy_message_id','map_applied_generation'}<=columns:
            raise ValueError('change ledger columns are incompatible')
        missing=await (await c.execute("""SELECT count(*) AS n FROM assistant.messages m
            WHERE m.message_type='project_change' AND m.content_json->>'schema_version'='project-change.v1'
            AND NOT EXISTS(SELECT 1 FROM assistant.project_file_changes f WHERE f.legacy_message_id=m.message_id AND f.record_json=m.content_json)""")).fetchone()
        if missing['n']:raise ValueError('legacy change coverage is incomplete')
        if waits!=list(await (await c.execute(WAIT_SQL)).fetchall()):raise ValueError('waiting interactions changed')
        await c.commit();result['verified']=True
        return result


async def rollback_before_exposure(before, settings=None, *, apply=False):
    """Narrow rollback before any new reader/writer is exposed.

    A general downgrade after new business writes is deliberately unsupported:
    preserve those facts and use the compatible release/recovery procedure.
    Call only with all Kernel writers stopped and a verified backup in hand.
    """
    from scripts.inspect_upgrade_state import inspect, preserved
    settings=settings or KernelSettings.from_environment()
    async with AssistantDatabase(settings).connection() as c:
        await c.execute("SELECT pg_advisory_xact_lock(hashtextextended('kernel-schema-migration',0))")
        await c.execute("LOCK TABLE assistant.messages,assistant.runs,assistant.sessions,"
                        "assistant.model_invocations,assistant.project_grants,assistant.kernel_meta "
                        "IN SHARE ROW EXCLUSIVE MODE")
        version=(await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key='schema_version'")).fetchone())["value"]
        if version!='core5.v8' or before.get('database_schema')!='core5.v7':
            raise ValueError('rollback requires the exact v7 to v8 transition')
        current=await inspect(settings)
        if current['blockers'] or not preserved(before,current):
            raise ValueError('new or unresolved business facts prevent rollback')
        if await (await c.execute("SELECT 1 FROM assistant.kernel_meta WHERE key='system-config:v1'")).fetchone():
            raise ValueError('configuration has been accepted; keep its compatible reader')
        await c.execute("LOCK TABLE assistant.project_file_changes IN SHARE ROW EXCLUSIVE MODE")
        new=await (await c.execute("SELECT 1 FROM assistant.project_file_changes f WHERE "
            "legacy_message_id IS NULL OR NOT EXISTS (SELECT 1 FROM assistant.messages m "
            "WHERE m.message_id=f.legacy_message_id AND m.content_json=f.record_json) LIMIT 1")).fetchone()
        if new:raise ValueError('new ledger facts prevent rollback')
        if apply:
            # All removed rows are exact replicas of preserved legacy messages.
            # No user input, answer, approval, execution or history is removed.
            await c.execute('DROP TABLE assistant.project_file_changes')
            await c.execute('DELETE FROM assistant.kernel_meta WHERE key=%s',(LEDGER_KEY,))
            await c.execute("UPDATE assistant.kernel_meta SET value=%s,updated_at=now() WHERE key='schema_version'",(Jsonb('core5.v7'),))
            await c.commit()
        return {'rollback_allowed':True,'applied':apply,'target_schema':'core5.v7',
                'scope':'before_exposure_only','business_facts_preserved':True}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--dry-run',action='store_true');p.add_argument('--receipt',type=Path);p.add_argument('--rollback-before-state',type=Path);a=p.parse_args()
    if a.apply and a.dry_run:p.error('choose apply or dry-run')
    result=asyncio.run(rollback_before_exposure(json.loads(a.rollback_before_state.read_text('utf-8-sig')),apply=a.apply) if a.rollback_before_state else migrate(apply=a.apply))
    if a.receipt:a.receipt.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
