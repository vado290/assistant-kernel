import asyncio,uuid
import pytest
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.runtime_admission import RuntimeAdmission,RuntimeCapacityUnavailable
from assistant_kernel.services.document_runtime import resource_ref
from test_core4 import core_settings
from test_v12_p4_admission import config,script,claim,start_model
from test_v12_p3_prompt_protocol import preparation


async def test_accepted_project_limits_leave_other_projects_and_global_capacity_available(core_settings):
    await config(core_settings,max_finite_runs=4,max_managed_model_invocations=4)
    await preparation(core_settings,project_policy={'resource_limits':{'max_finite_runs':1,'max_managed_model_invocations':1}})
    repo=CoreRepository(core_settings)
    first=await script(repo);second=await script(repo);other=await script(repo,project='other')
    outcomes=await asyncio.gather(claim(repo),claim(repo))
    assert {x['run_id'] for x in outcomes if x}=={first['run_id'],other['run_id']}
    assert (await repo.get_run(str(second['run_id'])))['status']=='queued'
    await start_model(repo,uuid.uuid4().hex)
    with pytest.raises(RuntimeCapacityUnavailable,match='Project'):await start_model(repo,uuid.uuid4().hex)


async def test_project_policy_cannot_expand_global_capacity(core_settings):
    await config(core_settings,max_finite_runs=2)
    with pytest.raises(ValueError,match='widen'):await preparation(core_settings,project_policy={'resource_limits':{'max_finite_runs':3}})


async def test_parallel_cpu_steps_reuse_parent_slots_without_double_global_reservation(core_settings):
    await config(core_settings,max_finite_runs=1,max_cpu_slots=2)
    repo=CoreRepository(core_settings);await script(repo,logical_execution_id='same-work')
    run=await claim(repo);service=RuntimeAdmission(core_settings)
    owner=resource_ref(kind='receipt',identity=str(run['run_id']),uri='db://assistant/runs/'+str(run['run_id']),purpose='actual finite parent')
    req={'owner_ref':owner,'logical_execution_id':'same-work','resource_kind':'cpu_work_item','cpu_slots':2,
        'model_invocations':0,'expected_instance_generation':None,'idempotency_key':'one'}
    outcomes=await asyncio.gather(service.cpu_step(repo,run,req),service.cpu_step(repo,run,{**req,'idempotency_key':'two'}))
    assert sorted(x['status'] for x in outcomes)==['queued','reserved']
    winner=next(x for x in outcomes if x['status']=='reserved')['reservation_id'].split(':')[-1]
    await service.cpu_step(repo,run,{**req,'idempotency_key':winner},release=True)
    assert (await service.cpu_step(repo,run,{**req,'idempotency_key':'three'}))['status']=='reserved'
    await script(repo)
    assert await claim(repo) is None
    current=await repo.get_run(str(run['run_id']))
    assert current['request_json']['capacity_reservation']['units']['cpu_slots']==2
