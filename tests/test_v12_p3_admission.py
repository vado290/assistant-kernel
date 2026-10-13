from dataclasses import replace
import uuid

from psycopg.types.json import Jsonb
import pytest
import yaml

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.published_agents import PublishedAgentService
from assistant_kernel.services.template_bundle import TemplateBundle
from test_core4 import core_settings,prepare_agent,_questionnaire_waiting,_questionnaire_action
from test_v12_foundations import source


def complete_package(settings,*,existing=()):
    prepare_agent(settings);bundle=TemplateBundle().preserve(settings.data_root)
    loader=AgentFileLoader(settings);base=loader.load('demo','project:demo:manager');configs={}
    for phase in ('requirements','design','build','audit','operate'):
        actor,_=bundle.assemble(base,actor='pm',phase=phase,conditions=set(),condition_source_refs=[],local_role_append='',local_role_source_refs=[])
        configs[actor.agent_key+'/'+phase]=actor
    supervisor_base=replace(base,agent_id='supervisor',agent_key='project:demo:supervisor',kind='supervisor',phase_mode=None)
    supervisor,_=bundle.assemble(supervisor_base,actor='supervisor',phase=None,conditions=set(),condition_source_refs=[],local_role_append='',local_role_source_refs=[])
    configs[supervisor.agent_key+'/default']=supervisor
    service=PublishedAgentService(settings)
    ref=service.prepare('demo',configurations=configs,template_ref=bundle.ref('profiles.json'),source_refs=[source()],
        adoption_scope={'project_ids':['demo'],'existing_task_ids':list(existing),'new_tasks':True,'next_turn_binding_confirmation':True},
        project_policy={'layout_version':1,'task_protocol':'single-agent.v2','phase_protocol':'five-stage.v1','supervisor':{'enabled':True}})
    service.adopt('demo',package_ref=ref,expected_previous_ref=None,approval_refs=[source()])
    return service,ref


async def test_complete_protocol_intake_queues_supervisor_before_any_pm_turn(core_settings):
    service,ref=complete_package(core_settings);repo=CoreRepository(core_settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='admission',
        user_id='owner',external_event_id='new-source',external_message_id=None,content={'text':'Clarify the offline report'})
    task=incoming['run'];assert task['status']=='waiting_supervisor'
    assert task['request_json']['protocol_revision']=='1.2' and task['executor_json']['task_runtime_package_ref']==ref
    assert 'confirmed_turn_package_ref' not in task['request_json']
    claimed=await repo.claim_run('admission-test',str(uuid.uuid4()),90)
    assert claimed['agent_key']=='project:demo:supervisor'
    assert str(claimed['request_json']['task_id'])==str(task['run_id'])
    from assistant_kernel.services.supervisor_v12 import SupervisorV12
    context=await SupervisorV12(core_settings,repo).prepare_context(claimed)
    snapshot=SupervisorV12(core_settings,repo).documents.read('demo',context['snapshot_ref'])
    assert snapshot['payload']['project_assessment']['status']=='facts_only'
    assert snapshot['payload']['active_task']['task_id']==str(task['run_id'])
    assert context['runtime_package_ref']==ref and context['consultation_only'] is False
    visible=SupervisorV12(core_settings,repo).model_context(claimed,context,incoming['message'])
    assert visible['user_inputs'][0]['source']['text']=='Clarify the offline report'
    assert visible['current_facts']==snapshot['payload']
    assert visible['meta']['control_run_id']==str(claimed['run_id'])


async def test_v12_processor_uses_captured_facts_without_legacy_snapshot(core_settings,monkeypatch):
    from test_v12_p3_assessment import captured
    from assistant_kernel.services.supervisor_processor import SupervisorProcessor
    repo,service,task,control,worker,context,decision=await captured(core_settings)
    processor=SupervisorProcessor(core_settings,repo)
    config=processor.supervisor.configuration('demo')
    config.update(enabled=True,result_protocol_revision='1.2')
    async def configured(*args):return config
    async def legacy(*args,**kwargs):raise AssertionError('v1.2 must not build the legacy snapshot')
    seen=[]
    async def evaluate(**kwargs):
        seen.append(kwargs['snapshot'])
        return {**decision,'protocol_revision':'1.2'}
    monkeypatch.setattr(processor.supervisor,'invocation_configuration',configured)
    monkeypatch.setattr(processor.supervisor,'build_snapshot',legacy)
    monkeypatch.setattr(processor.supervisor,'run_consultation',evaluate)
    settled=await processor.process(control,worker)
    assert settled['outcome']=='settled' and not settled['pm_dispatched']
    assert seen[0]['user_inputs'][0]['source']['text']=='Prepare an offline report'
    assert seen[0]['prompt_compilation_context']['phase_state']['current_mode']=='requirements'


async def test_existing_question_is_untouched_until_actual_submit_then_enters_new_supervision(core_settings):
    root=core_settings.data_root/'vaults/demo';root.mkdir(parents=True,exist_ok=True)
    (root/'project.yaml').write_text(yaml.safe_dump({'layout_version':1,'task_protocol':'single-agent.v2'}))
    repo,run,message=await _questionnaire_waiting(core_settings)
    original=await repo.get_message(str(message['message_id']))
    service,ref=complete_package(core_settings,existing=[str(run['run_id'])])
    waiting=await repo.get_run(str(run['run_id']))
    assert waiting['status']=='waiting_user' and waiting['waiting_question_message_id']==original['message_id']
    assert (await repo.get_message(str(original['message_id'])))['content_json']==original['content_json']
    # The old questionnaire remains answerable with its original IDs/draft.
    current=await _questionnaire_action(repo,message,'choice',value='0')
    current=await _questionnaire_action(repo,current['message'],'next')
    current=await _questionnaire_action(repo,current['message'],'text',value='local environment')
    current=await _questionnaire_action(repo,current['message'],'next')
    submitted=await _questionnaire_action(repo,current['message'],'submit',event='actual-legacy-submit')
    upgraded=await repo.get_run(str(run['run_id']))
    assert upgraded['status']=='waiting_supervisor' and upgraded['request_json']['protocol_revision']=='1.2'
    assert upgraded['request_json']['answer']['answers']=={'scope':'最小版本','environment':'local environment'}
    assert 'confirmed_turn_package_ref' not in upgraded['request_json']
    assert upgraded['executor_json'].get('task_runtime_package_ref') is None
    assert upgraded['request_json']['pending_runtime_package_ref']==ref


async def test_unadopted_actor_keeps_its_preserved_supervisor_configuration(core_settings):
    repo=CoreRepository(core_settings);prepare_agent(core_settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='old-actor',
        user_id='owner',external_event_id='old-actor',external_message_id=None,content={'text':'old request'})
    service,ref=complete_package(core_settings)
    old=service.load('demo','project:demo:supervisor',task_id=str(incoming['run']['run_id']))
    assert old.agent_key=='project:demo:supervisor' and old.foundation_revision is None
    (core_settings.data_root/'vaults/demo/role.md').write_text('unaccepted future physical role')
    assert service.load('demo',old.agent_key,task_id=str(incoming['run']['run_id'])).role_text==old.role_text
