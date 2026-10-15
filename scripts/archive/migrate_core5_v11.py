"""Additive schedule migration and guarded rollback. Never enable business schedules."""
import argparse,asyncio,json
from pathlib import Path
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase


async def migrate(settings,apply=False,rollback=False):
    async with AssistantDatabase(settings).connection() as c:
        current=(await(await c.execute("SELECT value FROM assistant.kernel_meta WHERE key='schema_version' FOR UPDATE")).fetchone())['value']
        if current not in {'core5.v10','core5.v11'}:raise ValueError('Expected core5.v10/v11 baseline')
        result={'source_schema':current,'target_schema':'core5.v10' if rollback else 'core5.v11','apply':apply,'schedules_enabled':0}
        if rollback:
            if current=='core5.v10':return result
            active=await(await c.execute("SELECT count(*) n FROM assistant.project_schedules WHERE state='enabled'")).fetchone()
            pending=await(await c.execute("SELECT count(*) n FROM assistant.schedule_ticks WHERE settlement_status='pending'")).fetchone()
            if active['n'] or pending['n']:raise PermissionError('Disable schedules and settle every occurrence before rollback')
            instances=await(await c.execute("SELECT count(*) n FROM assistant.service_instances WHERE cleanup_status<>'verified'")).fetchone()
            if instances['n']:raise PermissionError('Service process cleanup must be verified before rollback')
            if apply:
                await c.execute("UPDATE assistant.kernel_meta SET value='\"core5.v10\"'::jsonb WHERE key='schema_version'")
                await c.commit()
            result['ledger_retained']=True;return result
        if apply:
            await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v11.sql').read_text())
            for table,required in {'project_schedules':{'project_id','schedule_id','definition_ref','control_revision','next_fire_at','control_json'},
                'schedule_ticks':{'tick_id','scheduled_for_utc','run_id','disposition','settlement_status','notification_id'}}.items():
                columns={x['column_name'] for x in await(await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='assistant' AND table_name=%s",(table,))).fetchall()}
                if not required<=columns:raise ValueError('Existing scheduler table has incompatible shape: '+table)
            await c.commit()
        return result


async def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--rollback',action='store_true');p.add_argument('--receipt',type=Path,required=True);a=p.parse_args()
    result=await migrate(KernelSettings.from_environment(),a.apply,a.rollback)
    a.receipt.parent.mkdir(parents=True,exist_ok=True);a.receipt.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))

if __name__=='__main__':asyncio.run(main())
