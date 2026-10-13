"""Fixed operations, actual DB/process; explicit synthetic user/activation fixtures."""
import asyncio,uuid
from datetime import datetime,timezone
from copy import deepcopy
import pytest
from psycopg.types.json import Jsonb
from assistant_kernel.services.operations import ProjectOperations
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.worker.main import execute_python
from test_core4 import core_settings
from test_v12_p4_units import unit


async def operation_fixture(settings):
    repo,units,registered,control,submission=await unit(settings,runbook=True)
    await units.control_finite('demo',control,authenticated_operator=True)
    definition=units.read_definition('demo',control['runtime_unit_ref']);documents=units.documents
    design=documents.read('demo',definition['design_ref']);task=await repo.get_run(design['task_id']);state=deepcopy(task['request_json']);accepted=state['accepted_documents']
    auth=str(uuid.uuid4())
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.messages(message_id,project_id,channel,chat_id,user_id,direction,message_type,content_json,delivery_status) VALUES(%s,'demo','discord','fixture','owner','inbound','answer',%s,'received')",
            (auth,Jsonb({'decision':'approve','text':'Synthetic isolated document approval; not human acceptance'})))
        await c.commit()
    for name in ('requirements','design'):
        ref=accepted['task_contract' if name=='requirements' else 'solution_design']
        state[name+'_approval']={'message_id':auth,'approved_document_sha256':ref['sha256']}
    def doc(kind,payload):return documents.write_scoped_revision(project_id='demo',scope='project',object_id='operation-fixture',document_kind=kind,
        document_schema=kind+'.v1',payload=payload,status='settled',actor_type='kernel',actor_id='fixture',reason='Synthetic isolated operation baseline',source_refs=[])
    # Use existing typed Review check as a clearly synthetic activation fixture.
    review=documents.read('demo',accepted['review_report'])['payload']
    manifest=documents.read('demo',submission['manifest_ref'])['payload']
    activation=doc('activation-report',{'publication_ref':accepted['publication'],'actual_version_refs':[control['runtime_unit_ref']],
        'environment_ref':manifest['environment']['environment_ref'],'action_receipt_refs':[],
        'per_item_checks':review['per_item_checks'],'unverified':[]})
    accepted['activation']=activation
    acceptance=resource_ref(kind='message',identity=auth,uri='db://assistant/messages/'+auth,purpose='synthetic acceptance fixture, not human acceptance')
    baseline=doc('operational-baseline',{'creating_task_ref':resource_ref(kind='receipt',identity=str(task['run_id']),uri='db://assistant/runs/'+str(task['run_id']),purpose='actual isolated source Task'),
        'task_contract_ref':accepted['task_contract'],'solution_design_ref':accepted['solution_design'],'publication_ref':accepted['publication'],
        'activation_ref':activation,'acceptance_record_ref':acceptance,'actual_version_refs':[control['runtime_unit_ref']],
        'runbook_actions':design['payload']['runbook_actions'],'verified_at':datetime.now(timezone.utc).isoformat()})
    state.update(acceptance_record_ref=acceptance,operational_baseline_ref=baseline)
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='succeeded',request_json=%s WHERE run_id=%s",(Jsonb(state),task['run_id']))
        await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES('project-runtime:demo',%s) ON CONFLICT(key) DO UPDATE SET value=assistant.kernel_meta.value || EXCLUDED.value",
            (Jsonb({'operational_baseline_ref':baseline}),));await c.commit()
    operation=await repo.create_run(project_id='demo',agent_key='project:demo:manager',run_type='chat',trigger_message_id=auth,request={
        'protocol_revision':'1.2','current_stage':'operate','work_mode':'operate','task_activation':'active'})
    return repo,ProjectOperations(settings,repo),operation,control


async def test_operate_reuses_fixed_submit_and_preserves_action_identity(core_settings):
    repo,operations,task,control=await operation_fixture(core_settings)
    first=await operations.run_action('demo',str(task['run_id']),'count',{'n':42},'one')
    second=await operations.run_action('demo',str(task['run_id']),'count',{'n':42},'two')
    assert first['run_id']==second['run_id'] and first['request_json']['python_protocol']=='fixed-manifest.v1'
    assert str(first['parent_run_id'])==str(task['run_id'])
    owner=str(uuid.uuid4());run=await repo.claim_run('fixed-operate',owner,300,queue_kind='runtime')
    await execute_python(run,core_settings,repo,owner,asyncio.Event())
    assert (await repo.get_run(str(run['run_id'])))['result_json']=={'answer':42}


async def test_operate_rejects_unaccepted_baseline_and_disabled_unit(core_settings):
    repo,operations,task,control=await operation_fixture(core_settings)
    from assistant_kernel.services.runtime_units import RuntimeUnits
    await RuntimeUnits(core_settings,repo).control_finite('demo',{**control,'action':'pause','expected_control_revision':1,'idempotency_key':'pause'},authenticated_operator=True)
    with pytest.raises(PermissionError,match='authorized'):await operations.run_action('demo',str(task['run_id']),'count',{'n':42},'invalid')
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='waiting_user' WHERE request_json ? 'acceptance_record_ref'");await c.commit()
    with pytest.raises(PermissionError,match='completed'):await operations._baseline('demo')
