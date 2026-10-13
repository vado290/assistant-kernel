"""Explicit isolated migration; original questions/answers are never consumed."""
from copy import deepcopy
import pytest
from assistant_kernel.services.question_groups import QuestionGroupService,projection_ref
from test_core4 import core_settings,_questionnaire_waiting,_questionnaire_action
from test_v12_p3_admission import complete_package


async def test_legacy_question_identity_draft_and_native_session_survive_explicit_bridge(core_settings):
    repo,task,message=await _questionnaire_waiting(core_settings)
    edited=await _questionnaire_action(repo,message,'choice',value='0',event='legacy-draft-choice')
    original=deepcopy(edited['message']);identity=str(message['message_id'])
    complete_package(core_settings,existing=[str(task['run_id'])])
    service=QuestionGroupService(core_settings,repo)
    before=await repo.get_run(str(task['run_id']))
    routes=[{'channel':'discord','chat_id':'200','user_id':'300','interface_role':'pm','is_origin':True},
            {'channel':'discord','chat_id':'201','user_id':'300','interface_role':'supervisor','is_origin':False}]
    async with repo.database.connection() as c:
        owner=await (await c.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task['run_id'],))).fetchone()
        group=await service.bridge_legacy_locked(c,owner=owner,message_id=identity,routes=routes);await c.commit()
    after=await repo.get_run(str(task['run_id']))
    assert after['status']==before['status']=='waiting_user'
    assert after['waiting_question_message_id']==before['waiting_question_message_id'] and after['session_id']==before['session_id']
    assert after['request_json']==before['request_json']
    wrapper=group['content_json'];assert wrapper['legacy_messages'][0]['content_json']==original['content_json']
    assert wrapper['group']['draft']['answers']==original['content_json']['questionnaire']['answers']
    assert wrapper['group']['accepted_answers']==[] and identity in wrapper['projection_ids']
    async with repo.database.connection() as c:
        repeated=await service.bridge_legacy_locked(c,owner=after,message_id=identity,routes=routes)
        assert repeated['message_id']==group['message_id']
    restored=await repo.get_message(identity)
    assert projection_ref(restored['content_json'])['id']==str(group['message_id'])
    assert [q['question_text'] for q in restored['content_json']['questions']]==[q['question_text'] for q in original['content_json']['questions']]
    for index,(action,value) in enumerate([('next',None),('text','local environment'),('next',None),('submit',None)]):
        result=await _questionnaire_action(repo,restored,action,value=value,event='bridged-'+str(index))
        restored=result['message']
    accepted=await repo.get_run(str(task['run_id']))
    assert accepted['status']=='waiting_supervisor' and 'confirmed_turn_package_ref' not in accepted['request_json']
    canonical=await repo.get_message(str(group['message_id']))
    assert canonical['content_json']['group']['status']=='answered'
    assert len(canonical['content_json']['group']['accepted_answers'])==2


async def test_unadopted_legacy_question_cannot_be_migrated(core_settings):
    repo,task,message=await _questionnaire_waiting(core_settings)
    service=QuestionGroupService(core_settings,repo)
    async with repo.database.connection() as c:
        owner=await (await c.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task['run_id'],))).fetchone()
        with pytest.raises(PermissionError):await service.bridge_legacy_locked(c,owner=owner,message_id=str(message['message_id']),routes=[])
