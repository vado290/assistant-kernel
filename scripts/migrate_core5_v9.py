"""Incremental Unit registry preparation; does not register or enable any Unit."""
import argparse,asyncio,json
from pathlib import Path
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase


async def migrate(settings,apply=False):
    db=AssistantDatabase(settings)
    async with db.connection() as c:
        row=await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key='schema_version'")).fetchone()
        if row['value'] not in {'core5.v8','core5.v9'}:raise ValueError('Unit migration requires the actual core5.v8/v9 baseline')
        result={'source_schema':row['value'],'target_schema':'core5.v9','apply':apply,'units_registered':0,'units_enabled':0}
        if apply:
            await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v9.sql').read_text())
            fields={x['column_name'] for x in await (await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='assistant' AND table_name='project_runtime_units'")).fetchall()}
            if not {'project_id','unit_id','effective_definition_ref','desired_state','control_revision','enable_authorization_ref','control_json'}<=fields:
                raise ValueError('Unit registry shape is incompatible')
            await c.commit()
        return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args();result=await migrate(KernelSettings.from_environment(),args.apply)
    args.receipt.parent.mkdir(parents=True,exist_ok=True);args.receipt.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':asyncio.run(main())
