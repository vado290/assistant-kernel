"""Add service instances only; never enable or consume an existing task."""
import argparse,asyncio,json
from pathlib import Path
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase

async def migrate(settings,apply=False):
    async with AssistantDatabase(settings).connection() as c:
        row=await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key='schema_version'")).fetchone()
        if row['value'] not in {'core5.v9','core5.v10'}:raise ValueError('Expected the actual core5.v9/v10 baseline')
        result={'source_schema':row['value'],'target_schema':'core5.v10','apply':apply,'services_started':0}
        if apply:
            await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v10.sql').read_text())
            columns={x['column_name'] for x in await (await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='assistant' AND table_name='service_instances'")).fetchall()}
            if not {'instance_id','project_id','unit_id','generation','definition_ref','observed_state','cleanup_status','state_json'}<=columns:raise ValueError('Service instance shape is incompatible')
            await c.commit()
        return result
async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--receipt',type=Path,required=True);args=parser.parse_args()
    result=await migrate(KernelSettings.from_environment(),args.apply);args.receipt.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':asyncio.run(main())
