import pytest
from assistant_kernel.services.runtime_handoffs import RuntimeHandoffs
from assistant_kernel.services.prompt_protocol import PromptProtocol
from test_core4 import core_settings
from test_v12_p4_units import unit


async def prepared(settings):
    def checked(manifest,definition,source):
        manifest['io']['business_check_refs']=[source('program/answer-check.json',{'schema_version':'python-business-check.v1',
            'json_pointer':'/answer','operator':'equals','expected':42})]
    repo,units,row,control,submission=await unit(settings,transform=checked,handoff_authorized=True)
    definition=units.read_definition('demo',control['runtime_unit_ref'])
    task_id=units.documents.read('demo',definition['design_ref'])['task_id'];task=await repo.get_run(task_id)
    turn=PromptProtocol(settings,repo).write_blob('demo',task_id,'synthetic-turn',{'fixture':True})
    handoff={'definition_ref':control['runtime_unit_ref'],'manifest_ref':submission['manifest_ref'],'requested_action':'enable',
        'trial_refs':[],'review_refs':[task['request_json']['accepted_documents']['review_report']],
        'activation_refs':[],'missing_approval_subject_refs':[],'unverified':[]}
    return repo,task,turn,handoff


async def test_handoff_actual_enable_and_ownership_share_pm_transaction(core_settings):
    repo,task,turn,handoff=await prepared(core_settings);service=RuntimeHandoffs(core_settings,repo)
    async with repo.database.connection() as c:
        await c.execute('SELECT 1 FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task['run_id'],))
        receipt=await service.apply_locked(c,task=task,handoffs=[handoff],turn_ref=turn)
        assert receipt
        await c.rollback()
    async with repo.database.connection() as c:
        row=await (await c.execute('SELECT * FROM assistant.project_runtime_units')).fetchone()
        assert row['desired_state']=='stopped' and row['control_revision']==0
        receipt=await service.apply_locked(c,task=task,handoffs=[handoff],turn_ref=turn);await c.commit()
    async with repo.database.connection() as c:
        row=await (await c.execute('SELECT * FROM assistant.project_runtime_units')).fetchone()
        assert row['desired_state']=='running' and row['control_revision']==1
        duplicate=await service.apply_locked(c,task=task,handoffs=[handoff],turn_ref=turn);await c.commit()
    assert duplicate==receipt


async def test_handoff_cannot_enable_using_a_file_grant_or_undo_pause(core_settings):
    repo,task,turn,handoff=await prepared(core_settings);service=RuntimeHandoffs(core_settings,repo)
    invalid={**task,'request_json':{**task['request_json'],'design_approval':None}}
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='approval'):await service.apply_locked(c,task=invalid,handoffs=[handoff],turn_ref=turn)
        await c.rollback()
        await service.apply_locked(c,task=task,handoffs=[handoff],turn_ref=turn);await c.commit()
    from assistant_kernel.services.runtime_units import RuntimeUnits
    definition=service.units.read_definition('demo',handoff['definition_ref'])
    await RuntimeUnits(core_settings,repo).control_finite('demo',{'runtime_unit_ref':handoff['definition_ref'],'action':'pause',
        'expected_control_revision':1,'idempotency_key':'actual-pause','authorization_source_refs':[definition['approved_execution_scope_ref']],
        'target_definition_ref':None,'stop_policy':'drain','cause':'manual','tick_ref':None,'expected_instance_ref':None,'expires_at':None},authenticated_operator=True)
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='undo'):await service.apply_locked(c,task=task,handoffs=[handoff],turn_ref=turn)


async def test_runtime_writer_and_actual_activation_cannot_accept_self_report_only(core_settings):
    import asyncio,uuid
    from assistant_kernel.services.runtime_documents import write_runtime_document
    from assistant_kernel.services.project_python import ProjectPython
    from assistant_kernel.services.document_runtime import resource_ref
    from assistant_kernel.worker.main import execute_python
    repo,task,turn,handoff=await prepared(core_settings);service=RuntimeHandoffs(core_settings,repo)
    async with repo.database.connection() as c:
        ownership=await service.apply_locked(c,task=task,handoffs=[handoff],turn_ref=turn);await c.commit()
    admission=await ProjectPython(core_settings,repo).submit('demo',{'manifest_ref':handoff['manifest_ref'],'inputs':{'n':42},
        'execution_kind':'formal','runtime_unit_ref':handoff['definition_ref'],'logical_execution_id':None,'resume_owner':None,'idempotency_key':'activation-run'})
    manifest=service.documents.read('demo',handoff['manifest_ref'])['payload']
    report={'publication_ref':task['request_json']['accepted_documents']['publication'],
        'actual_version_refs':[handoff['definition_ref']],'environment_ref':manifest['environment']['environment_ref'],
        'action_receipt_refs':[admission['run_ref']],'per_item_checks':[{'check_id':'A2-run','criterion_ids':['A2'],'result':'passed',
            'actual_result':42,'evidence_refs':[admission['run_ref']],'reason':None}],'unverified':[]}
    ref=write_runtime_document(core_settings,task,'activation-report',report)
    ready={**handoff,'requested_action':'none','activation_refs':[ref]}
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='actual'):await service.accept_activation_locked(c,task=task,handoffs=[ready])
    worker=str(uuid.uuid4());run=await repo.claim_run('activation-test',worker,120,queue_kind='runtime')
    await execute_python(run,core_settings,repo,worker,asyncio.Event())
    async with repo.database.connection() as c:
        assert await service.accept_activation_locked(c,task=task,handoffs=[ready])==ref
    wrong={**task,'request_json':{**task['request_json'],'current_stage':'build'}}
    with pytest.raises(PermissionError,match='phase'):write_runtime_document(core_settings,wrong,'activation-report',report)
