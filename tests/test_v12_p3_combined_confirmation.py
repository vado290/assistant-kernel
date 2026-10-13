from copy import deepcopy
import uuid

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.prompt_protocol import PromptProtocol
from test_core4 import core_settings
from test_v12_foundations import requirement
from test_v12_p3_admission import complete_package
from test_v12_p3_new_work import prompt_result


async def prepared(settings):
    _,package_ref=complete_package(settings);repo=CoreRepository(settings);compiler=PromptProtocol(settings,repo)
    received=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='combined',
        user_id='owner',external_event_id='combined-source',external_message_id=None,content={'text':'另做离线分析报告 B'})
    task=received['run'];source=resource_ref(kind='message',identity=str(received['message']['message_id']),
        uri='db://assistant/messages/'+str(received['message']['message_id']),purpose='actual isolated original input')
    payload=requirement();payload['requirements'][0]['source_refs']=[source]
    ref=compiler.documents.write_scoped_revision(project_id='demo',scope='task',object_id='task-contract',
        task_id=str(task['run_id']),run_id=str(task['run_id']),document_kind='task-contract',document_schema='task-contract.v1',
        payload=payload,status='submitted',actor_type='pm',actor_id=task['agent_key'],reason='isolated requirements report',source_refs=[source])
    async with repo.database.connection() as c:
        old_card=await repo._create_outbound_locked(c,project_id='demo',session_id=None,run_id=str(task['run_id']),channel='test',
            chat_id='combined',user_id='owner',message_type='approval',content={'text':'approve this requirements document','report_ref':ref},
            reply_to_message_id=received['message']['message_id'],idempotency_key='old-requirement-card')
        task=await (await c.execute("UPDATE assistant.runs SET status='waiting_user',waiting_question_message_id=%s,request_json=request_json || %s WHERE run_id=%s RETURNING *",
            (old_card['message_id'],Jsonb({'state_revision':1,'approval_kind':'requirements','accepted_documents':{'task_contract':ref}}),task['run_id']))).fetchone()
        head=await compiler.input_head_locked(c,task)
        cap=compiler.write_blob('demo',str(task['run_id']),'preflight-fixture',{'actual_model_verified':False})
        authority=compiler.write_blob('demo',str(task['run_id']),'authority-fixture',{'roots':[]})
        draft=prompt_result({'user_message_refs':[source]})['prompt_draft'];draft['proposed_work_mode']='design'
        proposal={'owner_ref':resource_ref(kind='receipt',identity=str(task['run_id']),uri='db://assistant/runs/'+str(task['run_id']),purpose='Task'),
            'user_message_refs':[source],'snapshot_ref':head,'basis_state_revision':1,'basis_context_epoch':0,
            'file_authority_ref':authority,'capability_profile_ref':cap,'draft':draft,'blocking_question_ids':[],
            'child_result_refs':[],'clarity':'ready_for_confirmation','scope_change':'none','confirmation_source_candidate_refs':[]}
        proposal_ref=compiler.documents.write_scoped_revision(project_id='demo',scope='task',object_id='prompt',task_id=str(task['run_id']),
            run_id=str(task['run_id']),document_kind='prompt-proposal',document_schema='prompt-proposal.v1',payload=proposal,
            status='proposed',actor_type='kernel',actor_id='kernel',reason='isolated combined confirmation',source_refs=[source])
        preparation=await compiler.prepare_locked(c,task=task,proposal_ref=proposal_ref,runtime_package_ref=package_ref,input_head_ref=head,capability_ref=cap)
        await c.commit()
    display=await repo.get_message(preparation['display_ref']['id'])
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    return repo,compiler,task,display,ref,old_card


async def confirm(compiler,display,event):
    return await compiler.record_confirmation(display_message_id=str(display['message_id']),project_id='demo',channel='test',chat_id='combined',
        user_id='owner',external_event_id=event,action='confirm',expected_package_sha256=display['content_json']['package_ref']['sha256'])


async def test_one_specific_event_approves_document_and_exact_next_prompt_separately(core_settings):
    repo,compiler,task,display,ref,old=await prepared(core_settings)
    package=compiler.read_blob('demo',display['content_json']['package_ref'])
    assert package['approval_subjects']=={'task_contract':ref}
    assert ref['sha256'] in display['content_json']['text'] and '报告' in display['content_json']['text']
    assert (await repo.get_message(str(old['message_id'])))['content_json']['action_status']=='superseded'
    receipt=await confirm(compiler,display,'both-subjects')
    updated=await repo.get_run(str(task['run_id']))
    assert updated['status']=='queued' and updated['request_json']['current_stage']=='design'
    assert updated['request_json']['requirements_approval']['approved_document_sha256']==ref['sha256']
    event=await repo.get_message(receipt['user_event_ref']['id'])
    assert event['content_json']['document_approvals'][0]['document_ref']==ref
    assert event['content_json']['confirmation_receipt']['prompt_ref']==package['prompt_ref']
    async with repo.database.connection() as c:
        dispatched,draft=await compiler.validate_dispatch_locked(c,updated)
    assert dispatched==package and draft['proposed_work_mode']=='design'
    assert (await confirm(compiler,display,'both-subjects'))['status']=='duplicate'


async def test_changed_document_subject_cannot_commit_either_approval(core_settings):
    repo,compiler,task,display,ref,_=await prepared(core_settings)
    body=compiler.documents.read('demo',ref)['payload'];body['goal']='changed goal'
    changed=compiler.documents.write_scoped_revision(project_id='demo',scope='task',object_id='task-contract',
        task_id=str(task['run_id']),run_id=str(task['run_id']),document_kind='task-contract',document_schema='task-contract.v1',
        payload=body,status='submitted',actor_type='pm',actor_id=task['agent_key'],reason='actual changed isolated document',source_refs=[])
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
            (Jsonb({'accepted_documents':{'task_contract':changed}}),task['run_id']));await c.commit()
    with pytest.raises(PermissionError):await confirm(compiler,display,'stale-combined')
    updated=await repo.get_run(str(task['run_id']))
    assert not updated['request_json'].get('requirements_approval') and not updated['request_json'].get('confirmed_turn_package_ref')
    async with repo.database.connection() as c:
        event=await (await c.execute("SELECT content_json FROM assistant.messages WHERE external_event_id='stale-combined'")).fetchone()
    assert event['content_json']['acceptance']['status']=='rejected' and not event['content_json'].get('document_approvals')
