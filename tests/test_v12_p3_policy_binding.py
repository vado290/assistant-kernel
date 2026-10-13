from dataclasses import replace
import uuid
import pytest
import yaml
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.published_agents import PublishedAgentService
from test_core4 import core_settings
from test_v12_foundations import source
from test_v12_foundations import seed
from test_v12_packages import candidate


@pytest.mark.parametrize('revision',['1.2',None])
async def test_research_audit_uses_its_confirmed_admission_instead_of_legacy_engineering_gate(tmp_path,monkeypatch,revision):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from assistant_kernel.config import KernelSettings
    from assistant_kernel.services.run_processor import RunProcessor
    settings=KernelSettings.from_environment().model_copy(update={'data_root':tmp_path})
    repo=SimpleNamespace(get_message=AsyncMock(return_value={'content_json':{}}),pin_turn_inputs=AsyncMock(return_value=[]))
    processor=RunProcessor(settings,repo,SimpleNamespace())
    monkeypatch.setattr(processor,'_project_task_protocol',lambda *args:'single-agent.v2')
    monkeypatch.setattr(processor.task_documents,'read',lambda *args:{'payload':{'delivery_obligations':['research']}})
    def fixed_phase(*args):raise RuntimeError('reached fixed Audit configuration and package admission')
    monkeypatch.setattr(processor,'_work_mode_bundle',fixed_phase)
    run={'run_id':'test','run_type':'chat','project_id':'demo','agent_key':'project:demo:manager',
        'claim_token':'test-claim','trigger_message_id':'source','request_json':{
            'protocol_revision':revision,'task_protocol':'single-agent.v2','phase_protocol':'five-stage.v1',
            'current_stage':'audit','accepted_documents':{'task_contract':{'sha256':'approved'}},
            'requirements_approval':{'approved_document_sha256':'approved'}}}
    if revision=='1.2':
        with pytest.raises(RuntimeError,match='fixed Audit configuration'):
            await processor.process(run,'worker')
    else:
        with pytest.raises(PermissionError,match='solution_design'):
            await processor.process(run,'worker')


async def test_confirmed_v12_phase_uses_fixed_config_without_a_second_legacy_mode_bundle(core_settings):
    from test_v12_p3_prompt_protocol import preparation,confirm
    from assistant_kernel.services.run_processor import RunProcessor
    repo,compiler,task,display,draft=await preparation(core_settings)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    await confirm(compiler,display,event='fixed-config-mode')
    task=await repo.get_run(str(task['run_id']))
    processor=RunProcessor(core_settings,repo,None)
    mode=processor._work_mode_bundle('demo','requirements',task)
    config=compiler.read_blob('demo',task['request_json']['confirmed_turn_package_ref'])['config_ref']
    assert mode['phase_profile']['config_ref']=={'uri':config['uri'],'sha256':config['sha256']}
    assert mode['phase_profile']['file_agreement']=='project_shared'
    with pytest.raises(PermissionError):processor._work_mode_bundle('demo','design',task)


async def test_old_waiting_task_policy_and_confirmed_package_survive_new_head(core_settings):
    bundle,config,_=candidate(core_settings)
    root=core_settings.data_root/'vaults/demo'
    old_policy={'layout_version':1, 'task_protocol':'single-agent.v2',
        'multi_agent':{'enabled':False}}
    (root/'project.yaml').write_text(yaml.safe_dump(old_policy))
    service=PublishedAgentService(core_settings);repo=CoreRepository(core_settings)
    old=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',
        chat_id='policy-pin',user_id='owner',external_event_id='old-policy',external_message_id=None,content={'text':'old waiting Task'})
    original=service.loader.load('demo','project:demo:manager');uri,sha=service.loader.snapshot('demo',original)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='waiting_user',executor_json=%s WHERE run_id=%s",
            (Jsonb({'config_snapshot_uri':uri,'config_bundle_hash':sha}),old['run']['run_id']));await c.commit()
    scope={'project_ids':['demo'],'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True}
    first=service.prepare('demo',configurations={'project:demo:manager/requirements':config},
        template_ref=bundle.ref('profiles.json'),adoption_scope=scope,source_refs=[source()],
        project_policy={**old_policy,'multi_agent':{'enabled':True}})
    service.adopt('demo',package_ref=first,expected_previous_ref=None,approval_refs=[source()])
    assert service.project_policy('demo')['multi_agent']['enabled'] is True
    assert service.project_policy('demo',task_id=str(old['run']['run_id']))==old_policy
    assert service.load('demo',original.agent_key,task_id=str(old['run']['run_id']),phase='requirements').foundation_revision is None
    # The confirmed-turn compiler may explicitly bind this accepted package.
    async with repo.database.connection() as c:
        await service.pin_task_package_locked(c,'demo',str(old['run']['run_id']),first,expected_previous_ref=None)
        await c.commit()
    assert service.project_policy('demo',task_id=str(old['run']['run_id']))['multi_agent']['enabled'] is True
    altered=replace(config,role_text=config.role_text+'\nFuture accepted role',config_sha256='b'*64)
    next_ref=service.prepare('demo',configurations={'project:demo:manager/requirements':altered},
        template_ref=bundle.ref('profiles.json'),adoption_scope=scope,source_refs=[source()],
        project_policy={**old_policy,'multi_agent':{'enabled':False}})
    service.adopt('demo',package_ref=next_ref,expected_previous_ref=first,approval_refs=[source()])
    assert service.project_policy('demo',task_id=str(old['run']['run_id']))['multi_agent']['enabled'] is True
    assert service.load('demo',original.agent_key,task_id=str(old['run']['run_id']),phase='requirements').role_text==config.role_text
    with pytest.raises(ValueError,match='stale'):
        async with repo.database.connection() as c:
            await service.pin_task_package_locked(c,'demo',str(old['run']['run_id']),next_ref,expected_previous_ref=None)


async def test_eligible_next_package_is_fixed_for_supervision_without_rebinding_pm(core_settings):
    from test_v12_p3_admission import complete_package
    from assistant_kernel.services.goal_supervisor import GoalSupervisorService
    from assistant_kernel.services.supervisor_v12 import SupervisorV12
    await seed(core_settings)
    service,old=complete_package(core_settings);repo=CoreRepository(core_settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',
        chat_id='candidate-control',user_id='owner',external_event_id='candidate-control',external_message_id=None,content={'text':'Prepare bounded next work'})
    task=incoming['run'];original=service.read_package('demo',old)
    assert (await repo.get_run(str(task['run_id'])))['executor_json']['task_runtime_package_ref']==old
    configs={}
    for selector,ref in original['configurations'].items():
        config=service.loader.load_snapshot('demo',ref['uri'],ref['sha256'])
        if selector.endswith(':supervisor/default'):config=replace(config,role_text=config.role_text+'\nAccepted updated Supervisor rule',config_sha256='c'*64)
        configs[selector]=config
    new=service.prepare('demo',configurations=configs,template_ref=original['template_ref'],source_refs=[source()],
        project_policy=original['project_policy'],adoption_scope={'project_ids':['demo'],'existing_task_ids':[str(task['run_id'])],
            'new_tasks':True,'next_turn_binding_confirmation':True})
    service.adopt('demo',package_ref=new,expected_previous_ref=old,approval_refs=[source()])
    worker=str(uuid.uuid4());control=await repo.claim_run('candidate-supervisor',worker,120)
    selected=await GoalSupervisorService(core_settings,repo).invocation_configuration('demo',control)
    assert selected['supervisor_package_ref']==new
    context=await SupervisorV12(core_settings,repo).prepare_context(control)
    assert context['runtime_package_ref']==new
    unchanged=await repo.get_run(str(task['run_id']))
    assert unchanged['executor_json']['task_runtime_package_ref']==old
    assert not unchanged['request_json'].get('prompt_confirmation_ref')
