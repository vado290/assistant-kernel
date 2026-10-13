"""Real isolated Prompt confirmation -> bounded Operate -> actual control -> completion."""
from copy import deepcopy
import pytest
from psycopg.types.json import Jsonb
from test_core4 import core_settings
from test_v12_scheduler import fixture
from test_v12_p3_new_work import setup,prompt_result,decision
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.runtime_documents import write_runtime_document
from assistant_kernel.services.phase_control_v12 import PhaseControlV12
from assistant_kernel.services.task_queue import ProjectTaskQueue
from assistant_kernel.services.published_agents import PublishedAgentService


@pytest.mark.parametrize('action',['update','pause','retire','run_now'])
async def test_confirmed_daily_action_needs_no_fake_engineering_approvals(core_settings,monkeypatch,action):
    repo,schedules,scheduler,origin,control,due=await fixture(core_settings)
    view=await schedules.status('demo','clock-test')
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='succeeded',request_json=request_json || %s WHERE run_id=%s",(Jsonb({'task_activation':'closed'}),origin))
        await ProjectTaskQueue(core_settings,repo)._meta(c,'demo')
        await ProjectTaskQueue(core_settings,repo)._patch_meta(c,'demo',{'active_task_id':None});await c.commit()
    with monkeypatch.context() as patch:
        original_adopt=PublishedAgentService.adopt
        def adopt_fixture(service,project_id,**kwargs):
            with service.connection() as c:
                prior=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(service.key(project_id),)).fetchone()
            kwargs['expected_previous_ref']=prior['value']['package_ref'] if prior else None
            return original_adopt(service,project_id,**kwargs)
        patch.setattr(PublishedAgentService,'adopt',adopt_fixture)
        repo,service,_,supervision,worker,context=await setup(core_settings,False)
    result=prompt_result(context)
    item={'schedule_id':'clock-test','definition_ref':view['registration']['definition_ref'],'action':action,'expected_control_revision':1,
        'changes':{'rule':{**view['definition']['rule'],'seconds':20}} if action=='update' else {}}
    result['prompt_draft'].update(proposed_work_mode='operate',schedule_actions=[item])
    submitted=await service.accept_prompt_result(control_id=str(supervision['run_id']),worker_instance_id=worker,
        claim_token=str(supervision['claim_token']),result=result,new_task=True)
    identity=service.documents.read('demo',submitted['proposal_ref'])['task_id']
    async def probe(**kwargs):
        return (service.prompts.write_blob('demo',identity,'capability-fixture',{'model_profile':{'model':'unit'}}),
            service.prompts.write_blob('demo',identity,'authority-fixture',{'roots':[]}))
    monkeypatch.setattr(service,'probe_pm_capabilities',probe)
    settled=await service.settle(control=supervision,worker_instance_id=worker,decision=decision(submitted['proposal_ref']))
    display=await repo.get_message(settled['prompt_preparation']['display_ref']['id'])
    task=await repo.get_run(identity)
    assert task['status']=='waiting_user' and not task['request_json'].get('requirements_approval')
    with pytest.raises(PermissionError):await schedules.control('demo',{**control,'action':action,'expected_control_revision':1},task_id=identity)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    await service.prompts.record_confirmation(display_message_id=str(display['message_id']),project_id='demo',channel='test',
        chat_id='consult-B',user_id='owner',external_event_id='synthetic-daily-confirmation',action='confirm',
        expected_package_sha256=display['content_json']['package_ref']['sha256'])
    task=await repo.get_run(identity)
    async with repo.database.connection() as c:
        _,draft=await service.prompts.validate_dispatch_locked(c,task)
        task=await(await c.execute("UPDATE assistant.runs SET status='running' WHERE run_id=%s RETURNING *",(identity,))).fetchone();await c.commit()
    assert draft['schedule_actions']==[item]
    assert not task['request_json'].get('requirements_approval') and not task['request_json'].get('design_approval')
    packages=PublishedAgentService(core_settings);package=packages.read_package('demo',task['request_json']['pending_runtime_package_ref'])
    ref=package['configurations']['project:demo:manager/operate'];config=packages.loader.load_snapshot('demo',ref['uri'],ref['sha256'])
    selected,_=packages.runtime_skills('demo',config,package,task)
    assert 'control_schedule' in selected.allowed_tools and 'register_runtime_unit' not in selected.allowed_tools
    assert 'submit_project_python' not in selected.allowed_tools and any(x['name']=='assistant-schedule-management' for x in selected.skill_bindings)
    with pytest.raises(PermissionError):write_runtime_document(core_settings,task,'runtime-unit-definition',{})
    target=None
    if action=='update':
        candidate={**view['definition'],**item['changes']}
        target=write_runtime_document(core_settings,task,'kernel-schedule-definition',candidate)
        with pytest.raises(PermissionError):write_runtime_document(core_settings,task,'kernel-schedule-definition',{**candidate,'action':'stop'})
    authority=task['request_json']['prompt_confirmation_message_id']
    command={**control,'action':action,'expected_control_revision':1,'idempotency_key':'actual-daily-control','target_definition_ref':target,
        'authorization_source_refs':[resource_ref(kind='message',identity=authority,uri='db://assistant/messages/'+authority)]}
    with pytest.raises(PermissionError):await schedules.control('demo',{**command,'action':'resume'},task_id=identity)
    receipt=await schedules.control('demo',command,task_id=identity)
    assert receipt==await schedules.control('demo',command,task_id=identity)
    with pytest.raises(PermissionError,match='already completed'):
        await schedules.control('demo',{**command,'idempotency_key':'another-key'},task_id=identity)
    async with repo.database.connection() as c:
        task=await(await c.execute("UPDATE assistant.runs SET status='waiting_supervisor' WHERE run_id=%s RETURNING *",(identity,))).fetchone()
        source=await repo.get_message(authority)
        completion=await PhaseControlV12(core_settings,repo).complete_locked(c,task=task,source=source);await c.commit()
    assert completion['schedule_operation_completed']
    task=await repo.get_run(identity)
    assert task['status']=='succeeded' and task['result_json']['engineering_acceptance']=='not_applicable'
    assert task['request_json']['schedule_action_receipts']['clock-test']


async def test_unregistered_schedule_cannot_open_daily_shortcut(core_settings):
    repo,schedules,scheduler,origin,control,due=await fixture(core_settings)
    from assistant_kernel.services.schedule_operations import validate_actions
    item={'schedule_id':'nonexistent','definition_ref':(await schedules.status('demo','clock-test'))['registration']['definition_ref'],
        'action':'pause','expected_control_revision':1,'changes':{}}
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError):await validate_actions(c,'demo',[item])
