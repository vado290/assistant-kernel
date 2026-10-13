"""Real local tool/process/checkpoint evidence; synthetic approvals stay labeled."""
import asyncio,hashlib,importlib.metadata,json,socket,uuid
from pathlib import Path
import pytest
from assistant_kernel.services.business_models import BusinessModels
from assistant_kernel.services.business_runtime import adapter_ref
from assistant_kernel.services.business_tools import BusinessTools,BusinessEffects
from assistant_kernel.services.project_python import ProjectPython
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.question_groups import QuestionGroupService
from assistant_kernel.worker.main import execute_python
from test_core4 import core_settings
from test_v12_p4_units import unit

ENV=Path('/home/v/AI/assistant-data/runtime/validation-envs/p4-business-20261011')
SDKCODE="""import json,os
from assistant_kernel.runtime_client import RuntimeClient
client=RuntimeClient()
json.dump({'answer':42},open(os.environ['ASSISTANT_RESULT_PATH'],'w'))
"""
TOOLCODE="""from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
mcp=MCPServer('business-test')
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True,destructive_hint=False,open_world_hint=False))
def double(n:int)->int:
    return n*2
if __name__=='__main__':mcp.run(transport='stdio')
"""
GRAPHCODE="""import json,os
from typing import TypedDict
from langgraph.graph import StateGraph,START,END
from langgraph.types import interrupt
from assistant_kernel.runtime_client import RuntimeClient
client=RuntimeClient()
class State(TypedDict):
    n:int
    answer:int
def first(state):
    with client.cpu('before-compute',1):
        if client.private['loaded_agents']:client.model('reporter','Before the question',key='before-question-model')
    def effect():
        path=client.private['project_vault']+'/effect-count.txt'
        with open(path,'a') as f:f.write('once\\n')
        return 1
    client.effect('before-question',{'action':'append test effect count'},effect)
    return {'n':state['n']}
def question(state):
    answers=interrupt({'questions':[{'question_id':'format','question_text':'Choose the test format',
        'topic':'format','required':True,'options':[],'recommended_option_id':None,'recommendation_reason':'', 'source_refs':[]}]})
    if client.private['loaded_agents']:client.model('reporter','After the answer',key='after-answer-model')
    return {'answer':state['n'] if answers['format']=='compact' else 0}
def build(saver):
    graph=StateGraph(State);graph.add_node('first',first);graph.add_node('question',question)
    graph.add_edge(START,'first');graph.add_edge('first','question');graph.add_edge('question',END)
    return graph.compile(checkpointer=saver)
result=client.run_graph(build,json.load(open(os.environ['ASSISTANT_INPUT_PATH'])))
if result.get('status')!='needs_input':json.dump({'answer':result['answer']},open(os.environ['ASSISTANT_RESULT_PATH'],'w'))
"""


def transform(*,graph=False,agents=True,hard=False,tool_effect='read'):
    def edit(manifest,definition,source):
        entry=source('program/main.py',(GRAPHCODE if graph else SDKCODE).encode());tool=source('program/tool.py',TOOLCODE.encode())
        manifest['code_bundle']={'entrypoint_ref':entry,'module_refs':[tool],
            'bundle_ref':source('program/bundle.json',{'schema_version':'python-code-bundle.v1','files':[
                {'relative_path':'main.py','ref':entry},{'relative_path':'tool.py','ref':tool}]}),
            'closure_manifest_ref':source('program/closure.json',{'schema_version':'python-import-closure.v1',
                'stdlib_modules':['json','os','typing'],'dependency_modules':['assistant_kernel','langgraph','mcp']})}
        interpreter=ENV/'bin/python';site=str(ENV/'lib/python3.10/site-packages')
        versions={x.metadata['Name'].lower().replace('_','-'):x.version for x in importlib.metadata.distributions(path=[site])}
        manifest['environment']={'environment_ref':source('program/environment.json',{'schema_version':'python-environment-fingerprint.v1',
            'interpreter_path':str(interpreter),'python_minor':'3.10','site_packages':[site],'distribution_versions':versions}),
            'interpreter_ref':resource_ref(kind='source',identity='business-python',uri=str(interpreter),sha256=hashlib.sha256(interpreter.read_bytes()).hexdigest(),purpose='actual private validation interpreter'),
            'dependencies_kind':'declared_dependencies','dependency_lock_ref':source('program/dependencies.json',versions),'dependency_manifest_ref':None}
        manifest['resources']['timeout_seconds']=120
        definition['resource_policy_ref']=source('program/policy.json',{'schema_version':'runtime-resource-policy.v1','project_id':'demo',
            'max_model_calls':2,'max_total_model_seconds':120,'hard_input_tokens':1 if hard else None,'hard_output_tokens':None,'hard_cost_usd':None})
        role=source('program/role.md',b'You are the fixed report business Agent. Return only business output.')
        binding=source('program/model.json',{'schema_version':'business-model-binding.v1','adapter_kind':'codex_cli_harness',
            'adapter_implementation_ref':adapter_ref('codex_cli_harness'),'provider':'openai','model':'gpt-5.6-sol',
            'credential_refs':[],'timeout_seconds':60,'max_output_tokens':1000,'budget_scope':'project_and_runtime_work_item',
            'usage_reporting':'actual_or_unknown_not_zero'})
        client=source('program/mcp-client.json',{'command':str(interpreter),'args':[tool['uri']], 'implementation_refs':[tool],'tool_effects':{'double':tool_effect}})
        mcp={'server_name':'test-business','transport':'stdio','client_config_ref':client,'required':True,'required_tools':['double'],'credential_refs':[]}
        definition['business_agents']=[{'agent_id':'reporter','purpose':'fixed isolated report','role_ref':role,'skill_refs':[],
            'skill_bindings':[],'model_binding_ref':binding,'mcp_bindings':[mcp],'input_schema_ref':None,'output_schema_ref':None,
            'state_policy':'stateless','max_inflight_calls':1,'controls_project':False,'project_shared_files':True}] if agents else []
        if graph:definition['workflow']={'framework':'langgraph','definition_ref':source('program/graph.json',
            {'schema_version':'business-workflow-graph.v1','nodes':['first','question']}),'graph_id':'test-graph','namespace':'isolated-mainline',
            'state_owner':'business_program','checkpoint_policy':'project_namespaced','interruption_policy':'kernel_question_group','max_parallel_nodes':2}
        if graph:
            manifest['expected_effects']=['before-question']
            manifest['io']['write_targets']=['vault://demo/effect-count.txt']
            manifest['io']['business_check_refs']=[source('program/effect-check.json',{'schema_version':'python-business-check.v1',
                'source_kind':'file_text','target_uri':'vault://demo/effect-count.txt','json_pointer':'','operator':'equals',
                'expected':'once\n','effect_keys':['before-question']}),source('program/result-check.json',
                    {'schema_version':'python-business-check.v1','json_pointer':'/answer','operator':'equals','expected':42})]
    return edit


async def claimed_business(settings,**options):
    repo,units,registered,control,submission=await unit(settings,transform(**options))
    await units.control_finite('demo',control,authenticated_operator=True)
    await ProjectPython(settings,repo).submit('demo',submission)
    owner=str(uuid.uuid4());run=await repo.claim_run('business-fixture',owner,300,queue_kind='runtime')
    return repo,run,owner


async def test_fixed_assembly_real_mcp_and_bounded_model_ledger(core_settings,monkeypatch):
    repo,run,owner=await claimed_business(core_settings)
    from assistant_kernel.services.business_runtime import BusinessRuntime
    prepared=await ProjectPython(core_settings,repo).validate_dispatch(run)
    (prepared['attempt_root']/'input.json').write_text('{"n":42}')
    private=await BusinessRuntime(core_settings,repo).assemble(run,prepared)
    actual=json.loads(private.read_text());assert 'claim_token' not in actual and 'worker_instance_id' not in actual
    assert actual['context']['assembly_status']=='ready' and prepared['assembly_receipt_ref']
    tool=BusinessTools(core_settings,repo);request={'agent_id':'reporter','server_name':'test-business','tool':'double','arguments':{'n':21},'idempotency_key':'double'}
    response=await tool.call(run,request);assert response['content'][0]['text']=='42'
    assert (await tool.call(run,request))['content']==response['content']
    with pytest.raises(Exception):await tool.call(run,{**request,'arguments':{'n':'wrong'},'idempotency_key':'invalid'})
    with pytest.raises(PermissionError):await tool.call(run,{**request,'tool':'undeclared'})
    models=BusinessModels(core_settings,repo);called=[]
    async def call(*args):called.append(args);return 'actual adapter substitute',{},None,None
    monkeypatch.setattr(models,'call',call)
    first=await models.invoke(run,{'agent_id':'reporter','input':'one','idempotency_key':'one'})
    assert first['usage_status']=='unknown' and first['usage'] is None
    assert first==await models.invoke(run,{'agent_id':'reporter','input':'one','idempotency_key':'one'})
    await models.invoke(run,{'agent_id':'reporter','input':'two','idempotency_key':'two'})
    with pytest.raises(PermissionError,match='budget'):await models.invoke(run,{'agent_id':'reporter','input':'three','idempotency_key':'three'})
    assert len(called)==2
    await repo.finish_run(str(run['run_id']),owner,str(run['claim_token']),status='succeeded')


async def test_hard_budget_refused_before_process_launch(core_settings):
    repo,run,owner=await claimed_business(core_settings,hard=True)
    with pytest.raises(PermissionError,match='hard'):await execute_python(run,core_settings,repo,owner,asyncio.Event())
    current=await repo.get_run(str(run['run_id']))
    assert current['executor_json']['python_attempt']['process_identity'] is None


@pytest.mark.parametrize('restart_gap',[False,True,'pause_resume'])
async def test_real_graph_wait_answer_resume_reuses_thread_and_effect(core_settings,monkeypatch,restart_gap):
    import uvicorn,yaml
    from fastapi import FastAPI
    from assistant_kernel.api.business_runtime import build_runtime_router
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    settings=core_settings.model_copy(update={'api_port':port})
    repo,run,owner=await claimed_business(settings,graph=True,agents=True)
    async def isolated_adapter(self,*args):return 'synthetic adapter result',{'input_tokens':3,'output_tokens':1},None,None
    monkeypatch.setattr(BusinessModels,'call',isolated_adapter)
    # Registered synthetic interfaces; test answers do not represent human acceptance.
    config=settings.data_root/'config/channel-bindings.yaml';config.parent.mkdir(exist_ok=True)
    async with repo.database.connection() as c:
        source=await (await c.execute('SELECT m.* FROM assistant.messages m JOIN assistant.runs r ON r.trigger_message_id=m.message_id WHERE r.run_id=%s',
            (ProjectPython(settings,repo).fixed.read_manifest('demo',run['request_json']['manifest_ref'])['ownership']['creating_task_ref']['id'],))).fetchone()
    config.write_text(yaml.safe_dump({'bindings':[{'project_id':'demo','channel':source['channel'],'chat_id':chat,'user_id':source['user_id'],
        'agent_key':'project:demo:'+role} for chat,role in [('pm-test','manager'),('sup-test','supervisor')]]}))
    app=FastAPI();app.include_router(build_runtime_router(settings,repo));server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:await asyncio.sleep(.01)
        await execute_python(run,settings,repo,owner,asyncio.Event())
        waiting=await repo.get_run(str(run['run_id']));assert waiting['status']=='waiting_user' and waiting['claim_token'] is None
        if restart_gap=='pause_resume':
            from assistant_kernel.services.runtime_units import RuntimeUnits
            units=RuntimeUnits(settings,repo);definition=units.read_definition('demo',run['request_json']['runtime_unit_ref'])
            change={'runtime_unit_ref':run['request_json']['runtime_unit_ref'],'action':'pause','expected_control_revision':1,
                'idempotency_key':'pause-waiting','authorization_source_refs':[definition['approved_execution_scope_ref']],
                'target_definition_ref':None,'stop_policy':'drain','cause':'manual','tick_ref':None,'expected_instance_ref':None,'expires_at':None}
            await units.control_finite('demo',change,authenticated_operator=True)
            parent_id=units.documents.read('demo',definition['design_ref'])['task_id']
            async with repo.database.connection() as c:
                await c.execute('UPDATE assistant.runs SET parent_run_id=%s WHERE run_id=%s',(parent_id,run['run_id']));await c.commit()
        async with repo.database.connection() as c:
            canonical=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(waiting['executor_json']['runtime_wait']['group_id'],))).fetchone()
            projection=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(canonical['content_json']['projection_ids'][0],))).fetchone()
        questions=QuestionGroupService(settings,repo)
        from assistant_kernel.services.business_interactions import BusinessInteractions
        actual_resume=BusinessInteractions.resume
        if restart_gap is True:
            async def interrupted_callback(self,*args):return None
            monkeypatch.setattr(BusinessInteractions,'resume',interrupted_callback)
        accepted=await questions.record_text(projection=projection,channel=projection['channel'],chat_id=projection['chat_id'],user_id=projection['user_id'],
            external_event_id='synthetic-test-answer',text='compact')
        if restart_gap is True:
            monkeypatch.setattr(BusinessInteractions,'resume',actual_resume)
            recovered=await BusinessInteractions(settings,repo).recover_answered()
            assert len(recovered)==1
            assert await BusinessInteractions(settings,repo).recover_answered()==[]
            accepted['runtime_resume_run_id']=recovered[0]
        if restart_gap=='pause_resume':
            assert 'runtime_resume_run_id' not in accepted
            await units.control_finite('demo',{**change,'action':'resume','expected_control_revision':2,'idempotency_key':'resume-waiting'},authenticated_operator=True)
            recovered=await BusinessInteractions(settings,repo).recover_answered();assert len(recovered)==1
            accepted['runtime_resume_run_id']=recovered[0]
        resumed=await repo.get_run(accepted['runtime_resume_run_id'])
        assert resumed['request_json']['logical_execution_id']==run['request_json']['logical_execution_id']
        assert resumed['request_json']['manifest_ref']==run['request_json']['manifest_ref']
        assert resumed['request_json']['accumulated_budget_ref']
        if restart_gap=='pause_resume':
            assert resumed['request_json']['unit_control_revision']==3 and str(resumed['parent_run_id'])==parent_id
        second_owner=str(uuid.uuid4());second=await repo.claim_run('business-resume',second_owner,300,queue_kind='runtime')
        assert second['run_id']==resumed['run_id']
        await execute_python(second,settings,repo,second_owner,asyncio.Event())
        final=await repo.get_run(str(second['run_id']));assert final['status']=='succeeded' and final['result_json']=={'answer':42}
        assert final['executor_json']['python_attempt']['effect_status']=='verified'
        vault=ProjectPython(settings,repo).fixed.runtime.describe('demo').vault_root
        assert (vault/'effect-count.txt').read_text()=='once\n'
        async with repo.database.connection() as c:
            calls=await (await c.execute("SELECT metadata_json,input_tokens FROM assistant.model_invocations WHERE actor_role='business' ORDER BY started_at")).fetchall()
        assert len(calls)==2 and sum(x['input_tokens'] for x in calls)==6
        assert {x['metadata_json']['logical_execution_id'] for x in calls}=={run['request_json']['logical_execution_id']}
        assert await repo.claim_run('no-extra-resume',str(uuid.uuid4()),90,queue_kind='runtime') is None
    finally:
        server.should_exit=True;await serving;sock.close()
