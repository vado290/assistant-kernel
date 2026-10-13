"""Actual isolated PG races and subprocesses; no remote model calls."""
import asyncio,json,uuid
from pathlib import Path
import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.runtime_admission import RuntimeCapacityUnavailable
from assistant_kernel.services.system_config import SystemConfigService
from assistant_kernel.worker.main import execute_python
from assistant_kernel.storage import ProjectRuntimeManager
from test_core4 import core_settings,prepare_agent
from test_v12_foundations import example_config


def test_io_with_cpu_pool_reserves_cpu_work_and_validates_the_combined_budget():
    from assistant_kernel.services.runtime_admission import finite_demand,LEGACY_PYTHON
    resources={'mode':'io','cpu_pool_size':2,'numeric_threads':1,'cpu_budget':2,'declared_cpu_slots':2}
    assert finite_demand({'runtime_resources':resources},LEGACY_PYTHON)['cpu_work_items']==1
    with pytest.raises(ValueError,match='exceed the declared budget'):
        finite_demand({'runtime_resources':{**resources,'numeric_threads':2}},LEGACY_PYTHON)


def test_pure_io_does_not_reserve_a_cpu_work_item():
    from assistant_kernel.services.runtime_admission import finite_demand,LEGACY_PYTHON
    assert finite_demand({'runtime_resources':{'mode':'io','cpu_pool_size':0,'cpu_budget':1}},LEGACY_PYTHON)['cpu_work_items']==0


async def config(settings,**limits):
    value=example_config();value['resource_limits'].update(limits)
    service=SystemConfigService(settings)
    await service.accept(candidate=value,expected_revision='uninitialized',reason='Isolated admission fixture',source_kind='external_file',source_refs=[],write_input=True)
    return service


async def script(repo,project='demo',**request):
    return await repo.create_run(project_id=project,agent_key='project:'+project+':python',run_type='python',request={'source_uri':'vault://'+project+'/probe.py',**request})


async def claim(repo):return await repo.claim_run('capacity-test',str(uuid.uuid4()),90,queue_kind='runtime')


async def test_last_finite_slot_is_atomic_and_unaccepted_physical_file_is_ignored(core_settings):
    service=await config(core_settings,max_finite_runs=1);repo=CoreRepository(core_settings)
    for _ in range(6):await script(repo)
    physical=json.loads(service.path.read_text());physical['resource_limits']['max_finite_runs']=100;service.path.write_text(json.dumps(physical))
    outcomes=await asyncio.gather(*(claim(repo) for _ in range(6)))
    actual=[x for x in outcomes if x];assert len(actual)==1
    reservation=actual[0]['request_json']['capacity_reservation']
    assert reservation['resolved_config_ref']==(await service.state())['desired_ref']
    assert reservation['units']['finite_runs']==1 and reservation['units']['cpu_slots']==2


async def test_cpu_pool_thread_budget_rejected_before_queue(core_settings):
    repo=CoreRepository(core_settings)
    with pytest.raises(ValueError,match='exceed the declared budget'):
        await script(repo,runtime_resources={'mode':'cpu','cpu_pool_size':2,'numeric_threads':2,'cpu_budget':2})
    async with repo.database.connection() as c:
        assert not await (await c.execute('SELECT 1 FROM assistant.runs')).fetchone()


async def test_same_keys_serialize_opposite_order_and_other_keys_run(core_settings):
    repo=CoreRepository(core_settings)
    first=await script(repo,shared_write=True,resource_keys=['file:b','file:a'])
    second=await script(repo,shared_write=True,resource_keys=['file:a','file:b'])
    independent=await script(repo,shared_write=True,resource_keys=['file:c'])
    one,two=await asyncio.wait_for(asyncio.gather(claim(repo),claim(repo)),5)
    assert {one['run_id'],two['run_id']}=={first['run_id'],independent['run_id']}
    assert (await repo.get_run(str(second['run_id'])))['status']=='queued'
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='reconciling' WHERE run_id=%s",(first['run_id'],));await c.commit()
    assert await claim(repo) is None


async def test_legacy_unknown_shared_target_blocks_declared_writer(core_settings):
    repo=CoreRepository(core_settings);await script(repo,shared_write=True);assert await claim(repo)
    await script(repo,shared_write=True,resource_keys=['file:a'])
    assert await claim(repo) is None


async def test_parent_directory_and_file_targets_conflict_without_blocking_unrelated_paths(core_settings):
    repo=CoreRepository(core_settings)
    await script(repo,shared_write=True,resource_keys=['path:/project/shared'])
    assert await claim(repo)
    for _ in range(34):await script(repo,shared_write=True,resource_keys=['path:/project/shared/data.json'])
    other=await script(repo,shared_write=True,resource_keys=['path:/project/other/data.json'])
    assert (await claim(repo))['run_id']==other['run_id']


async def test_less_loaded_project_is_selected_before_older_same_project_work(core_settings):
    repo=CoreRepository(core_settings);await script(repo,project='one');assert await claim(repo)
    older=await script(repo,project='one');other=await script(repo,project='two')
    assert (await claim(repo))['run_id']==other['run_id']
    assert (await repo.get_run(str(older['run_id'])))['status']=='queued'


async def test_unknown_cleanup_keeps_finite_slot_even_after_manual_terminal_state(core_settings):
    await config(core_settings,max_finite_runs=1);repo=CoreRepository(core_settings)
    first=await script(repo);assert await claim(repo);await script(repo)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='failed',executor_json=%s WHERE run_id=%s",
            (Jsonb({'python_attempt':{'process_identity':{'pid':123456},'cleanup':{'status':'unknown'}}}),first['run_id']));await c.commit()
    assert await claim(repo) is None


async def start_model(repo,identity,**kwargs):
    return await repo.start_model_invocation(invocation_id=identity,project_id='demo',run_id=kwargs.get('run_id'),session_id=None,
        actor_role='runtime_business',harness='fixture',provider='fixture',model='fixture',reasoning_effort=None,auth_mode=None)


async def test_model_last_slot_unknown_settlement_and_duplicate_identity(core_settings):
    await config(core_settings,max_managed_model_invocations=1);repo=CoreRepository(core_settings)
    ids=[uuid.uuid4().hex for _ in range(6)]
    outcomes=await asyncio.gather(*(start_model(repo,x) for x in ids),return_exceptions=True)
    assert sum(x is None for x in outcomes)==1 and sum(isinstance(x,RuntimeCapacityUnavailable) for x in outcomes)==5
    winner=ids[outcomes.index(None)]
    with pytest.raises(PermissionError,match='second model call'):await start_model(repo,winner)
    await repo.finish_model_invocation(invocation_id=winner,status='failed',metadata={'outcome_status':'unknown'})
    with pytest.raises(RuntimeCapacityUnavailable):await start_model(repo,uuid.uuid4().hex)


async def test_model_capacity_wait_does_not_consume_retry_or_create_failed_turn(core_settings):
    repo=CoreRepository(core_settings);row=await script(repo);owner=str(uuid.uuid4())
    current=await repo.claim_run('capacity',owner,90,queue_kind='runtime')
    assert await repo.defer_model_capacity(str(row['run_id']),owner,str(current['claim_token']),'occupied')
    after=await repo.get_run(str(row['run_id']))
    assert after['status']=='queued' and after['attempt']==0 and not after['worker_instance_id']
    assert not after['request_json'].get('failure_settlement_key')
    assert not await repo.defer_model_capacity(str(row['run_id']),owner,str(current['claim_token']),'old lease')


async def test_four_actual_independent_processes_overlap_and_cannot_read_management_environment(core_settings,monkeypatch):
    prepare_agent(core_settings);await config(core_settings);repo=CoreRepository(core_settings)
    monkeypatch.setenv('KERNEL_API_TOKEN','synthetic-not-a-real-secret');monkeypatch.setenv('POSTGRES_PASSWORD','synthetic-not-a-real-secret')
    monkeypatch.setenv('MCP_BEARER_TOKEN','synthetic-not-a-real-secret');monkeypatch.setenv('PYTHONPATH','/synthetic/old-libraries')
    body="""import os,time,json
start=time.time();time.sleep(.5)
json.dump({'start':start,'end':time.time(),'leaked':{k:os.getenv(k) for k in ('KERNEL_API_TOKEN','POSTGRES_PASSWORD','MCP_BEARER_TOKEN','PYTHONPATH')},'threads':os.getenv('OPENBLAS_NUM_THREADS')},open(os.environ['ASSISTANT_RESULT_PATH'],'w'))
"""
    rows=[]
    for project in ('one','one','two','two'):
        root=ProjectRuntimeManager(core_settings.data_root).ensure(project).vault_root;(root/'probe.py').write_text(body)
        rows.append(await script(repo,project=project))
    owners=[str(uuid.uuid4()) for _ in rows]
    claimed=await asyncio.gather(*(repo.claim_run('real-process',owner,90,queue_kind='runtime') for owner in owners))
    assert all(claimed)
    await asyncio.gather(*(execute_python(row,core_settings,repo,owner,asyncio.Event()) for row,owner in zip(claimed,owners)))
    results=[(await repo.get_run(str(row['run_id'])))['result_json'] for row in rows]
    assert max(x['start'] for x in results)<min(x['end'] for x in results)
    assert all(all(value is None for value in x['leaked'].values()) and x['threads']=='1' for x in results)
