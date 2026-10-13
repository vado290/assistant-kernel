"""Shared finite/service owner checks; business records remain on their actual owner."""
from copy import deepcopy
from psycopg.types.json import Jsonb
from assistant_kernel.services.document_runtime import resource_ref

def execution_ref(run):
    identity=str(run['run_id']);service=run.get('service_instance_id')
    return resource_ref(kind='receipt',identity=identity,uri='db://assistant/'+('service_instances/' if service else 'runs/')+identity,
        purpose='actual service instance' if service else 'actual finite execution')

async def active(repository,run,connection=None,*,lock=False):
    if connection is None:
        async with repository.database.connection() as c:return await active(repository,run,c,lock=lock)
    if not run.get('service_instance_id'):
        return bool(await (await connection.execute("SELECT 1 FROM assistant.runs WHERE run_id=%s AND status='running' AND worker_instance_id=%s AND claim_token=%s AND cancel_requested_at IS NULL AND lease_until>now()"+(' FOR UPDATE' if lock else ''),
            (run['run_id'],run['worker_instance_id'],run['claim_token']))).fetchone())
    row=await (await connection.execute('SELECT * FROM assistant.service_instances WHERE instance_id=%s'+(' FOR UPDATE' if lock else ''),
        (run['service_instance_id'],))).fetchone()
    if not row or row['project_id']!=run['project_id'] or row['generation']!=run['attempt'] or row['observed_state'] not in {'starting','ready','degraded'}:return False
    logical=run['request_json']['logical_execution_id'];item=row['state_json'].get('work_control',{}).get('items',{}).get(logical)
    return bool(item and item['state']=='active')

async def effect_owner(connection,run):
    logical=run['request_json']['logical_execution_id']
    if run.get('service_instance_id'):
        row=await (await connection.execute('SELECT * FROM assistant.service_instances WHERE instance_id=%s FOR UPDATE',(run['service_instance_id'],))).fetchone()
        if not row or row['project_id']!=run['project_id'] or logical not in row['state_json']['work_control']['items']:raise PermissionError('Service work owner is unavailable')
        state=deepcopy(row['state_json']);work=state['work_records'][logical]
        return row,state,work.setdefault('runtime_effects',{})
    root=run['request_json'].get('runtime_root_run_id') or str(run['run_id'])
    row=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(root,))).fetchone()
    if not row or row['project_id']!=run['project_id'] or row['request_json']['logical_execution_id']!=logical:raise PermissionError('Effect belongs to another logical owner')
    state=deepcopy(row['executor_json']);return row,state,state.setdefault('runtime_effects',{})

async def save_effect_owner(connection,run,row,state):
    if run.get('service_instance_id'):
        await connection.execute('UPDATE assistant.service_instances SET state_json=%s,updated_at=now() WHERE instance_id=%s',(Jsonb(state),row['instance_id']))
    else:await connection.execute('UPDATE assistant.runs SET executor_json=%s,updated_at=now() WHERE run_id=%s',(Jsonb(state),row['run_id']))
