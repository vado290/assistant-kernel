"""End-to-end business tests with real PostgreSQL and fake model/Feishu transport."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from test_stage_integration import settings, work
from assistant_kernel.api.v2 import build_v2_router
from assistant_kernel.harness import AgentTurnResult
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.repositories.feedback import FeedbackRepository
from assistant_kernel.repositories.interactions import InteractionRepository
from assistant_kernel.repositories.stages import StageRepository
from assistant_kernel.services.context_budget import TurnUsage
from assistant_kernel.services.feishu import FeishuSender, handle_event, ingest_event, render_messages, verify_callback
from assistant_kernel.services.optimization import start_optimization
from assistant_kernel.services.project_build import ProjectBuildProcessor
from assistant_kernel.services.stage_protocol import register_stage_intent
from assistant_kernel.storage import ProjectRuntimeManager

pytestmark = pytest.mark.skipif(os.getenv('KERNEL_STAGE_INTEGRATION') != '1', reason='isolated PostgreSQL opt-in')


class ToolHarness:
    name='codex_cli'
    def __init__(self, settings, modes):self.settings=settings;self.modes=list(modes);self.calls=[]
    async def run_turn(self, request):
        self.calls.append(request)
        mode=self.modes.pop(0)
        if mode in {'input','confirmation','fail'}:
            kind='input' if mode in {'input','fail'} else 'confirmation'
            questions=([{'key':'time','text':'问题原文：每天几点发送？','required':True},
                        {'key':'size','text':'问题原文：正文需要多长？','required':True}] if kind=='input' else
                       [{'key':'confirm-document','text':'请确认这份完整文档后进入下一阶段。','required':True}])
            intent={'kind':kind,'document':'完整文档\n需求一：保留来源。\n需求二：按用户时间发送。',
                    'questions':questions,'next_action':'根据用户回答继续工作'}
            await register_stage_intent(self.settings,**request.stage_tool_context,intent=intent)
            if mode=='fail':raise RuntimeError('simulated CLI failure after registering a draft')
        if mode=='implement':
            (request.cwd/'script.py').write_text('print("improved")\n')
        text='VERDICT: APPROVE' if mode=='review' else '阶段已完成并记录产物。'
        return AgentTurnResult(physical_session_id=str(uuid.uuid4()),usage=TurnUsage(input_tokens=100),events=[],final_text=text)


async def start(settings, modes, delivery=True):
    repo=ControlPlaneRepository(settings)
    run=await repo.start_project_build(project_id='p',request_data={'goal':'日报','user_id':'owner',
        'delivery':{'channel':'feishu','app_id':'app','chat_id':'chat'} if delivery else None},idempotency_key=str(uuid.uuid4()))
    harness=ToolHarness(settings,modes);processor=ProjectBuildProcessor(settings,repo,harness)
    return repo,run,harness,processor


async def reply(settings, aid, qid, value, key=None):
    return await InteractionRepository(settings).respond(approval_id=aid,answer={'action':'answer','answers':[{'question_id':qid,'value':value}]},
        decided_by='owner',idempotency_key=key or str(uuid.uuid4()))


async def approve(settings,aid,**extra):
    return await InteractionRepository(settings).respond(approval_id=aid,answer={'action':'approve',**extra},decided_by='owner',idempotency_key=str(uuid.uuid4()))


async def test_multiple_questions_reverse_partial_and_immutable_originals(settings):
    repo,run,harness,p=await start(settings,['input','input','confirmation','confirmation'])
    _,waiting=await work(repo,p);aid=waiting['approval_id'];store=InteractionRepository(settings)
    first=await store.get(aid);q1,q2=first['questions']
    assert (await reply(settings,aid,str(q2['question_id']),'短一些',key='q2'))['status']=='waiting_user'
    assert (await reply(settings,aid,str(q2['question_id']),'短一些',key='q2'))['duplicate']
    with pytest.raises(ValueError,match='cannot approve'):
        await approve(settings,aid)
    with pytest.raises(ValueError,match='distinct question'):
        await reply(settings,aid,str(uuid.uuid4()),'wrong')
    result=await reply(settings,aid,str(q1['question_id']),False)
    assert result['next_stage']=='requirements'  # false is an answer, not an absent value.
    with pytest.raises(ValueError,match='conflicting'):
        await reply(settings,aid,str(q1['question_id']),'changed')
    async with repo.database.connection() as c:
        with pytest.raises(Exception,match='immutable'):
            await c.execute("UPDATE assistant.interaction_questions SET question_text='changed' WHERE question_id=%s",(q1['question_id'],))
    # A fresh processor can continue without a suspended Python call.
    fresh=ProjectBuildProcessor(settings,repo,harness)
    _,second=await work(repo,fresh)
    assert '每天几点发送' in harness.calls[-1].prompt and '短一些' in harness.calls[-1].prompt
    newer=await store.get(second['approval_id'])
    assert newer['questions'][0]['supersedes_question_id']==q1['question_id']
    assert newer['questions'][0]['question_id']!=q1['question_id']
    await reply(settings,second['approval_id'],str(newer['questions'][0]['question_id']),'9点')
    await reply(settings,second['approval_id'],str(newer['questions'][1]['question_id']),'500字')
    _,third=await work(repo,fresh)
    assert (await store.get(third['approval_id']))['payload']['interaction_kind']=='confirmation'
    result=await approve(settings,third['approval_id'],edited_payload={'feedback':'同意但改成10点'})
    assert result['next_stage']=='requirements'
    _,fourth=await work(repo,fresh)
    assert '同意但改成10点' in harness.calls[-1].prompt
    assert (await approve(settings,fourth['approval_id']))['next_stage']=='implementation'
    assert len({r.physical_session_id for r in harness.calls if r.physical_session_id})==0


async def test_failed_agent_draft_is_not_sent_and_retry_has_fresh_intent(settings):
    repo,run,harness,p=await start(settings,['fail','confirmation'])
    job=await repo.claim_job('test',str(uuid.uuid4()),60,job_types=['stage_run'])
    with pytest.raises(RuntimeError,match='simulated CLI failure'):await p.process(job)
    async with repo.database.connection() as c:
        assert (await (await c.execute('SELECT count(*) n FROM assistant.approval_requests')).fetchone())['n']==0
        assert (await (await c.execute('SELECT count(*) n FROM assistant.outbox_events')).fetchone())['n']==0
        assert (await (await c.execute("SELECT count(*) n FROM assistant.stage_tool_requests WHERE status='draft'")).fetchone())['n']==1
    await repo.retry_job(str(job['job_id']),str(job['worker_instance_id']),{'type':'temporary'})
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.control_jobs SET available_at=now() WHERE job_id=%s",(job['job_id'],));await c.commit()
    _,waiting=await work(repo,p)
    assert waiting['status']=='waiting_user'
    async with repo.database.connection() as c:
        statuses=await (await c.execute('SELECT status,count(*) n FROM assistant.stage_tool_requests GROUP BY status')).fetchall()
        assert {r['status']:r['n'] for r in statuses}=={'draft':1,'published':1}


async def test_staged_tools_cannot_use_expired_worker_and_conflict(settings):
    repo,run,harness,p=await start(settings,[])
    job=await repo.claim_job('test',str(uuid.uuid4()),60,job_types=['stage_run'])
    stages=StageRepository(settings);ctx=await stages.context(job);await stages.begin(ctx,job)
    args={'job_id':str(job['job_id']),'worker_instance_id':str(job['worker_instance_id']),
          'node_run_id':str(ctx['node_run_id']),'turn_key':str(uuid.uuid4())}
    intent={'kind':'input','document':'draft','next_action':'continue','questions':[{'key':'one','text':'请说明范围','required':True}]}
    one=await register_stage_intent(settings,**args,intent=intent)
    assert (await register_stage_intent(settings,**args,intent=intent))['request_id']==one['request_id']
    with pytest.raises(ValueError,match='conflicting'):
        await register_stage_intent(settings,**args,intent={**intent,'document':'changed'})
    with pytest.raises(ValueError,match='leased'):
        await register_stage_intent(settings,**{**args,'worker_instance_id':str(uuid.uuid4())},intent=intent)


async def test_feishu_full_questions_receipts_reverse_replies_and_card_validation(settings):
    repo,run,harness,p=await start(settings,['input','confirmation'])
    _,waiting=await work(repo,p)
    sent=[]
    def remote(request):
        if '/auth/' in request.url.path:return httpx.Response(200,json={'code':0,'tenant_access_token':'fake'})
        body=json.loads(request.content);sent.append(body)
        return httpx.Response(200,json={'code':0,'data':{'message_id':'m'+str(len(sent))}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote),base_url='https://open.feishu.cn') as client:
        event=(await repo.claim_outbox('sender',str(uuid.uuid4()),90,1,channel='feishu'))[0]
        sender=FeishuSender(settings,{'app_id':'app','app_secret':'fake'},client)
        await sender.deliver(event)
        assert any('问题原文：每天几点发送' in json.loads(s['content']).__str__() for s in sent)
        assert any('正文需要多长' in json.loads(s['content']).__str__() for s in sent)
        async with repo.database.connection() as c:
            rows=await (await c.execute('SELECT * FROM assistant.channel_deliveries WHERE question_id IS NOT NULL ORDER BY part')).fetchall()
            assert len(rows)==2
            assert (await (await c.execute('SELECT status FROM assistant.outbox_events WHERE outbox_event_id=%s',(event['outbox_event_id'],))).fetchone())['status']=='delivered'
        def incoming(mid,text,eid):
            return {'header':{'app_id':'app','event_id':eid},'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'owner'}},
                'message':{'chat_id':'chat','message_id':eid,'parent_id':mid,'content':json.dumps({'text':text})}}}
        ambiguous=incoming(None,'可以','ambiguous')
        result=await handle_event(settings,'app','ambiguous',ambiguous)
        assert result['status']=='needs_clarification'
        for i,row in enumerate(reversed(rows)):
            payload=incoming(row['external_message_id'],'答案'+str(i),'e'+str(i))
            assert (await ingest_event(settings,payload))['status']=='pending'
            assert (await ingest_event(settings,payload))['duplicate']
            await handle_event(settings,'app','e'+str(i),payload)
        _,waiting=await work(repo,p)
        # Deliver pending clarification notification and the new confirmation.
        events=await repo.claim_outbox('sender',str(uuid.uuid4()),90,10,channel='feishu')
        for event in events:await sender.deliver(event)
        async with repo.database.connection() as c:
            row=await (await c.execute('SELECT * FROM assistant.channel_deliveries WHERE approval_id=%s AND question_id IS NOT NULL',(waiting['approval_id'],))).fetchone()
        card={'event':{'operator':{'open_id':'owner'},'context':{'open_chat_id':'chat','open_message_id':row['external_message_id']},
                       'action':{'value':{'approval_id':waiting['approval_id'],'question_id':str(row['question_id']),'action':'approve'}}}}
        bad=json.loads(json.dumps(card));bad['event']['action']['value']['question_id']=str(uuid.uuid4())
        with pytest.raises(PermissionError):await handle_event(settings,'app','bad-card',bad)
        assert (await handle_event(settings,'app','card',card))['result']['next_stage']=='implementation'
        assert (await handle_event(settings,'app','card',card))['result']['duplicate']


async def test_sender_reuses_receipts_after_ack_crash(settings):
    repo,run,harness,p=await start(settings,['confirmation']);await work(repo,p)
    calls=[]
    def remote(request):
        if '/auth/' in request.url.path:return httpx.Response(200,json={'code':0,'tenant_access_token':'fake'})
        calls.append(json.loads(request.content));return httpx.Response(200,json={'code':0,'data':{'message_id':str(len(calls))}})
    event=(await repo.claim_outbox('sender',str(uuid.uuid4()),90,1,channel='feishu'))[0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote),base_url='https://open.feishu.cn') as client:
        sender=FeishuSender(settings,{'app_id':'app','app_secret':'fake'},client)
        async def crash(*args):raise RuntimeError('crash before ack')
        sender.repository.acknowledge_outbox=crash
        with pytest.raises(RuntimeError):await sender.deliver(event)
        count=len(calls)
        await FeishuSender(settings,{'app_id':'app','app_secret':'fake'},client).deliver(event)
        assert len(calls)==count


async def test_optimization_has_separate_sessions_candidate_and_feedback_trace(settings):
    repo=ControlPlaneRepository(settings);await repo.ensure_default_agent('p')
    original=ProjectRuntimeManager(settings.data_root).resolve_vault_file('p','script.py');original.write_text('print("original")\n')
    feedback=await FeedbackRepository(settings).submit('p',{'text':'改进脚本','expected_behavior':'保留旧行为','user_id':'owner','impact':'medium'},'f')
    request={'goal':'改进脚本','user_id':'owner','feedback_ids':[feedback['feedback_id']],'target_paths':['script.py']}
    run=await start_optimization(settings,'p',request,'optimization')
    assert await start_optimization(settings,'p',request,'optimization')==run
    harness=ToolHarness(settings,['confirmation','implement','review']);p=ProjectBuildProcessor(settings,repo,harness)
    _,waiting=await work(repo,p)
    assert (await approve(settings,waiting['approval_id']))['next_stage']=='implementation'
    await work(repo,p);_,waiting=await work(repo,p)
    outcome=await approve(settings,waiting['approval_id'])
    assert outcome['status']=='succeeded' and outcome['publication_status']=='candidate_accepted_not_published'
    candidate=original.parent/'runs'/run['task_id']/'candidate/script.py'
    assert 'improved' in candidate.read_text() and 'original' in original.read_text()
    assert harness.calls[0].cwd!=harness.calls[1].cwd
    assert all(r.physical_session_id is None for r in harness.calls)
    assert feedback['feedback_id'] in harness.calls[0].prompt
    async with repo.database.connection() as c:
        policy=await (await c.execute("SELECT tool_policy FROM assistant.agents WHERE agent_id='project:p:implementation'")).fetchone()
        assert policy['tool_policy']['sandbox']=='workspace-write'
        assert (await (await c.execute("SELECT count(*) n FROM assistant.logical_sessions WHERE project_id='p'")).fetchone())['n']==3
        row=await (await c.execute("SELECT proposal FROM assistant.change_requests WHERE target_type='optimization'")).fetchone()
        assert row['proposal']['implementation_artifact']['candidate_files']


async def test_optimization_baseline_change_and_prompt_retry_pinning(settings):
    repo=ControlPlaneRepository(settings);await repo.ensure_default_agent('p')
    original=ProjectRuntimeManager(settings.data_root).resolve_vault_file('p','script.py');original.write_text('print(1)')
    run=await start_optimization(settings,'p',{'goal':'improve','user_id':'owner','target_paths':['script.py']},'opt')
    harness=ToolHarness(settings,['confirmation']);p=ProjectBuildProcessor(settings,repo,harness)
    _,waiting=await work(repo,p);await approve(settings,waiting['approval_id'])
    original.write_text('print(2)')
    job=await repo.claim_job('test',str(uuid.uuid4()),60,job_types=['stage_run'])
    with pytest.raises(ValueError,match='baseline changed'):await p.process(job)
    assert len(harness.calls)==1


async def test_api_question_envelope_and_missing_feishu_config(settings):
    repo,run,harness,p=await start(settings,['input']);_,waiting=await work(repo,p)
    app=FastAPI();app.include_router(build_v2_router(settings,repo,lambda:None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.get('/v2/interactions/'+waiting['approval_id']);assert r.status_code==200
        assert r.json()['questions'][0]['question_text']=='问题原文：每天几点发送？'
        assert not (await c.get('/v2/channels/feishu/status')).json()['configured']
        qid=r.json()['questions'][0]['question_id']
        response=await c.post('/v2/interactions/'+waiting['approval_id']+'/answers',headers={'X-Idempotency-Key':'api'},
            json={'decided_by':'owner','answer':{'answers':[{'question_id':qid,'value':'九点'}]}})
        assert response.status_code==202 and response.json()['status']=='waiting_user'
    with pytest.raises(RuntimeError):verify_callback({}, {})
    with pytest.raises(PermissionError):verify_callback({'verification_token':'expected'},{'token':'wrong'})


async def test_real_stdio_mcp_registers_only_bound_stage_intent(settings):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    repo,run,harness,p=await start(settings,[])
    job=await repo.claim_job('test',str(uuid.uuid4()),60,job_types=['stage_run'])
    stages=StageRepository(settings);ctx=await stages.context(job);await stages.begin(ctx,job)
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src'),
         'POSTGRES_DB':settings.postgres_database,'ASSISTANT_DATA_ROOT':str(settings.data_root),
         'KERNEL_STAGE_JOB_ID':str(job['job_id']),'KERNEL_STAGE_WORKER_ID':str(job['worker_instance_id']),
         'KERNEL_STAGE_NODE_ID':str(ctx['node_run_id']),'KERNEL_STAGE_TURN_KEY':str(uuid.uuid4())}
    params=StdioServerParameters(command=sys.executable,args=['-m','assistant_kernel.mcp_server.stage_tools'],env=env)
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as client:
            await client.initialize()
            names={t.name for t in (await client.list_tools()).tools}
            assert names=={'ask_user','submit_stage_for_confirmation'}
            result=await client.call_tool('ask_user',{'questions':[{'key':'time','text':'请确认时间','required':True}],
                'draft_markdown':'完整草稿','next_action':'继续需求分析'})
            assert not result.is_error,result
    async with repo.database.connection() as c:
        request=await (await c.execute('SELECT * FROM assistant.stage_tool_requests')).fetchone()
        assert request['request_json']['questions'][0]['text']=='请确认时间'
        assert request['status']=='draft'
        assert (await (await c.execute('SELECT count(*) n FROM assistant.outbox_events')).fetchone())['n']==0


async def test_concurrent_last_answers_enqueue_once_and_wrong_actor_cannot_answer(settings):
    repo,run,harness,p=await start(settings,['input']);_,waiting=await work(repo,p)
    aid=waiting['approval_id'];questions=(await InteractionRepository(settings).get(aid))['questions']
    with pytest.raises(PermissionError):
        await InteractionRepository(settings).respond(approval_id=aid,
            answer={'answers':[{'question_id':str(questions[0]['question_id']),'value':'wrong'}]},
            decided_by='another-user',idempotency_key='wrong')
    results=await asyncio.gather(*[reply(settings,aid,str(q['question_id']),i,key=str(i)) for i,q in enumerate(questions)])
    assert sum(r.get('job_id') is not None for r in results)==1
    async with repo.database.connection() as c:
        assert (await (await c.execute("SELECT count(*) n FROM assistant.control_jobs WHERE status='queued'")).fetchone())['n']==1


async def test_expired_last_sender_lease_becomes_dead_letter(settings):
    repo,run,harness,p=await start(settings,['confirmation']);await work(repo,p)
    event=(await repo.claim_outbox('sender',str(uuid.uuid4()),90,1,channel='feishu'))[0]
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.outbox_events SET attempts=max_attempts,lease_until=now()-interval '1 second' WHERE outbox_event_id=%s",(event['outbox_event_id'],));await c.commit()
    assert not await repo.claim_outbox('sender',str(uuid.uuid4()),90,1,channel='feishu')
    async with repo.database.connection() as c:
        assert (await (await c.execute('SELECT status FROM assistant.outbox_events WHERE outbox_event_id=%s',(event['outbox_event_id'],))).fetchone())['status']=='dead_letter'
