"""Existing queue/controller integration; no model or real-human acceptance claim."""
from copy import deepcopy
import pytest
from psycopg.types.json import Jsonb
from assistant_kernel.services.question_groups import QuestionGroupService
from assistant_kernel.services.task_queue import ProjectTaskQueue
from test_core4 import core_settings
from test_v12_p3_new_work import setup,prompt_result,decision


@pytest.mark.parametrize('inflight',[False,True])
async def test_confirmed_priority_switch_waits_boundary_and_restores_unanswered_task(core_settings,monkeypatch,inflight):
    repo,service,a,control,worker,context=await setup(core_settings,True)
    queue=ProjectTaskQueue(core_settings,repo);groups=QuestionGroupService(core_settings,repo)
    session=await repo.ensure_session('demo',a['agent_key'],'test','main-A:task:'+str(a['run_id']))
    async with repo.database.connection() as c:
        group=await groups.create_locked(c,owner=a,source_message_id=str(a['trigger_message_id']),
            questions=[{'question_id':'scope','question_text':'Please specify scope','topic':'scope','required':True,
                'options':[],'recommended_option_id':None,'recommendation_reason':'','source_refs':[]}],
            routes=[{'channel':'test','chat_id':'main-A','user_id':'owner','interface_role':'pm','is_origin':True},
                    {'channel':'test','chat_id':'main-A-supervisor','user_id':'owner','interface_role':'supervisor','is_origin':False}])
        body=deepcopy(group['content_json']);body['group']['draft'].update(revision=1,answers={'scope':'unsubmitted draft'})
        await c.execute('UPDATE assistant.messages SET content_json=%s WHERE message_id=%s',(Jsonb(body),group['message_id']))
        await c.execute("UPDATE assistant.sessions SET native_session_id='native-A' WHERE session_id=%s",(session['session_id'],))
        await c.execute('UPDATE assistant.runs SET waiting_question_message_id=%s,session_id=%s,status=%s WHERE run_id=%s',
            (body['projection_ids'][0],session['session_id'],'running' if inflight else 'waiting_user',a['run_id']));await c.commit()
    proposed=await service.accept_prompt_result(control_id=str(control['run_id']),worker_instance_id=worker,
        claim_token=str(control['claim_token']),result=prompt_result(context),new_task=True,priority='urgent')
    b_id=service.documents.read('demo',proposed['proposal_ref'])['task_id']
    async def probe(**kwargs):
        return (service.prompts.write_blob('demo',b_id,'test-capability',{'model_profile':{'model':'test'}}),
                service.prompts.write_blob('demo',b_id,'test-authority',{'roots':[]}))
    monkeypatch.setattr(service,'probe_pm_capabilities',probe)
    prepared=await service.settle(control=control,worker_instance_id=worker,decision=decision(proposed['proposal_ref']))
    display=await repo.get_message(prepared['prompt_preparation']['display_ref']['id'])
    assert '挂起' in display['content_json']['text'] and '恢复' in display['content_json']['text']
    assert (await queue.activate_next('demo'))['outcome']=='already_active'
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.messages SET delivery_status='sent' WHERE message_id=%s",(display['message_id'],));await c.commit()
    await service.prompts.record_confirmation(display_message_id=str(display['message_id']),project_id='demo',channel='test',
        chat_id='consult-B',user_id='owner',external_event_id='confirm-priority',action='confirm',
        expected_package_sha256=display['content_json']['package_ref']['sha256'])
    if inflight:
        assert (await queue.activate_next('demo'))['outcome']=='already_active'
        assert (await repo.get_run(str(a['run_id'])))['status']=='running'
        async with repo.database.connection() as c:
            await c.execute("UPDATE assistant.runs SET status='waiting_user' WHERE run_id=%s",(a['run_id'],));await c.commit()
    switched=await queue.activate_next('demo')
    assert switched['outcome']=='activated' and str(switched['task']['run_id'])==b_id
    held=await repo.get_run(str(a['run_id']))
    assert held['status']=='blocked' and held['request_json']['task_activation']=='suspended'
    assert (await repo.get_message(str(group['message_id'])))['content_json']==body
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.runs SET status='succeeded',request_json=request_json || %s WHERE run_id=%s",(Jsonb({'task_activation':'closed'}),b_id));await c.commit()
    resumed=await queue.activate_next('demo')
    assert resumed['outcome']=='resumed' and str(resumed['task']['run_id'])==str(a['run_id'])
    restored=await repo.get_run(str(a['run_id']))
    assert restored['status']=='waiting_user' and restored['session_id']==session['session_id']
    assert (await repo.get_session(str(session['session_id'])))['native_session_id']=='native-A'
    assert (await repo.get_message(str(group['message_id'])))['content_json']==body
