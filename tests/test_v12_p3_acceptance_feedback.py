from copy import deepcopy
import uuid

import pytest
import yaml
from psycopg.types.json import Jsonb

from assistant_kernel.services.contracts import contracts,ContractError
from assistant_kernel.services.question_groups import QuestionGroupService
from test_core4 import core_settings
from test_v12_p3_phase_control import baseline


async def subjective(settings):
    repo,prompts,task,phases,write=await baseline(settings)
    refs,docs=phases.baseline(task);req=deepcopy(docs['task_contract']['payload']);req['acceptance_criteria'][0]['requires_user_feedback']=True
    current=write('task-contract','task-contract.v1',req)
    design=deepcopy(docs['solution_design']['payload']);design.update(task_contract_ref=current,task_contract_sha256=current['sha256'])
    des=write('solution-design','solution-design.v1',design)
    impl=deepcopy(docs['implementation_report']['payload']);impl.update(task_contract_ref=current,solution_design_ref=des)
    implementation=write('implementation-report','implementation-report.v1',impl)
    review=deepcopy(docs['review_report']['payload']);review['fixed_input_refs']=[current,des,implementation]
    reviewed=write('review-report','review-report.v1',review)
    state=task['request_json'];state['accepted_documents'].update(task_contract=current,solution_design=des,implementation_report=implementation,review_report=reviewed)
    state.update(validation_requirements_sha256=current['sha256'],validation_design_sha256=des['sha256'],validation_implementation_sha256=implementation['sha256'])
    state.update(phases.admit_review(task))
    source=await repo.get_message(str(task['trigger_message_id']))
    (settings.data_root/'config').mkdir(exist_ok=True)
    (settings.data_root/'config/channel-bindings.yaml').write_text(yaml.safe_dump({'bindings':[
        {'project_id':'demo','channel':'test','chat_id':source['chat_id'],'agent_key':'project:demo:manager'},
        {'project_id':'demo','channel':'test','chat_id':'feedback-supervisor','agent_key':'project:demo:supervisor'}]}))
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=%s WHERE run_id=%s',(Jsonb(state),task['run_id']));await c.commit()
    return repo,prompts,task,phases,write,source


async def test_subjective_feedback_waits_for_real_specific_answer_after_technical_pass(core_settings):
    repo,prompts,task,phases,_,source=await subjective(core_settings)
    async with repo.database.connection() as c:
        pending=await phases.complete_locked(c,task=task,source=source);await c.commit()
    assert pending['status']=='waiting_user_feedback' and (await repo.get_run(str(task['run_id'])))['status']=='waiting_user'
    canonical=await repo.get_message(pending['group_id']);wrapper=canonical['content_json'];group=wrapper['group'];qid=group['questions'][0]['question_id']
    projection=await repo.get_message(wrapper['projection_ids'][0]);questions=QuestionGroupService(core_settings,repo)
    with pytest.raises(PermissionError):
        await questions.record_action(message_id=str(projection['message_id']),project_id='demo',channel='test',chat_id=projection['chat_id'],
            user_id='another',external_event_id='not-owner-feedback',revision=0,page=0,action='explicit_text',value={qid:'满足'},
            content_version=group['content_version'],original_text='满足')
    await questions.record_action(message_id=str(projection['message_id']),project_id='demo',channel='test',chat_id=projection['chat_id'],
        user_id='owner',external_event_id='actual-final-feedback',revision=0,page=0,action='explicit_text',value={qid:'满足'},
        content_version=group['content_version'],original_text=qid+': 满足')
    current=await repo.get_run(str(task['run_id']))
    async with repo.database.connection() as c:
        completed=await phases.complete_locked(c,task=current,source=source);await c.commit()
    assert completed['task_completed']
    receipt=prompts.read_blob('demo',completed['acceptance_record_ref'])
    assert receipt['user_feedback']['status']=='accepted' and len(receipt['user_feedback']['feedback_refs'])==1


async def test_failed_technical_check_never_asks_satisfaction_to_replace_it(core_settings):
    repo,_,task,phases,write,source=await subjective(core_settings)
    _,docs=phases.baseline(task);review=deepcopy(docs['review_report']['payload']);review['per_item_checks'][0]['result']='failed'
    task['request_json']['accepted_documents']['review_report']=write('review-report','review-report.v1',review)
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='A1'):await phases.complete_locked(c,task=task,source=source)
        rows=await (await c.execute("SELECT count(*) AS n FROM assistant.messages WHERE message_type='question_group'")).fetchone()
    assert rows['n']==0


async def test_feedback_from_previous_delivery_version_cannot_accept_new_delivery(core_settings):
    repo,prompts,task,phases,write,source=await subjective(core_settings)
    async with repo.database.connection() as c:
        pending=await phases.complete_locked(c,task=task,source=source);await c.commit()
    original=await repo.get_message(pending['group_id']);group=original['content_json']['group'];projection=await repo.get_message(original['content_json']['projection_ids'][0])
    qid=group['questions'][0]['question_id']
    await QuestionGroupService(core_settings,repo).record_action(message_id=str(projection['message_id']),project_id='demo',channel='test',
        chat_id=projection['chat_id'],user_id='owner',external_event_id='previous-delivery-feedback',revision=0,page=0,action='explicit_text',
        value={qid:'满足'},content_version=group['content_version'],original_text=qid+': 满足')
    task=await repo.get_run(str(task['run_id']));_,docs=phases.baseline(task)
    new_delivery=prompts.write_blob('demo',str(task['run_id']),'changed-delivery',{'lines':['changed']})
    implementation=deepcopy(docs['implementation_report']['payload']);implementation['actual_changes']=[new_delivery]
    impl_ref=write('implementation-report','implementation-report.v1',implementation)
    review=deepcopy(docs['review_report']['payload']);review['fixed_input_refs'][-1]=impl_ref;review['deliverable_refs']=[new_delivery]
    reviewed=write('review-report','review-report.v1',review)
    state=task['request_json'];state['accepted_documents'].update(implementation_report=impl_ref,review_report=reviewed)
    state.update(validation_implementation_sha256=impl_ref['sha256']);state.update(phases.admit_review(task))
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET request_json=%s WHERE run_id=%s',(Jsonb(state),task['run_id']))
        pending2=await phases.complete_locked(c,task=task,source=source);await c.commit()
    assert pending2['status']=='waiting_user_feedback' and pending2['group_id']!=pending['group_id']
    assert (await repo.get_message(pending['group_id']))['content_json']['group']['accepted_answers']


def test_new_criterion_requires_flag_while_compatibility_criterion_keeps_original_fields():
    old={'criterion_id':'A','requirement_ids':['R'],'method':'explicit test','expected_result':'pass','required':True,'applicability':'artifact'}
    contracts().validate('Criterion',old)
    with pytest.raises(ContractError):contracts().validate('V12_Criterion',old)
    contracts().validate('V12_Criterion',{**old,'requires_user_feedback':False})


async def test_negative_feedback_returns_rework_without_closing_or_reasking_satisfaction(core_settings):
    repo,_,task,phases,_,source=await subjective(core_settings)
    async with repo.database.connection() as c:
        pending=await phases.complete_locked(c,task=task,source=source);await c.commit()
    canonical=await repo.get_message(pending['group_id']);group=canonical['content_json']['group']
    projection=await repo.get_message(canonical['content_json']['projection_ids'][0]);qid=group['questions'][0]['question_id']
    await QuestionGroupService(core_settings,repo).record_action(message_id=str(projection['message_id']),project_id='demo',channel='test',
        chat_id=projection['chat_id'],user_id='owner',external_event_id='actual-reject-feedback',revision=0,page=0,
        action='explicit_text',value={qid:'需修改'},content_version=group['content_version'],original_text=qid+': 需修改')
    current=await repo.get_run(str(task['run_id']))
    async with repo.database.connection() as c:
        result=await phases.complete_locked(c,task=current,source=source);await c.commit()
        count=await (await c.execute("SELECT count(*) AS n FROM assistant.messages WHERE message_type='question_group'")).fetchone()
    assert result['status']=='rework_required' and count['n']==1
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_supervisor'
