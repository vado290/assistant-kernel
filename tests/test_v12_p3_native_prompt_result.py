"""Real isolated DB/file admission tests; native fixtures are not model proof."""
import hashlib,json,uuid
import pytest
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.invocation_context import write_invocation_context
from assistant_kernel.services.invocation_config import InvocationConfiguration
from test_core4 import core_settings
from test_v12_p3_new_work import setup,prompt_result
from test_v12_p3_new_work import decision
from test_v12_foundations import seed


async def native_fixture(settings):
    await seed(settings)
    repo,service,main,control,worker,context=await setup(settings,False)
    parent=str(uuid.uuid4());child=str(uuid.uuid4());name='/root/prompt_assembly';invocation='native-parent-test'
    selected=service.prompts.packages.load('demo','project:demo:supervisor')
    uri,digest=service.prompts.packages.loader.snapshot('demo',selected)
    config=resource_ref(kind='source',identity='fixture-supervisor-config',uri=uri,sha256=digest,purpose='actual fixture configuration')
    scope=service.prompts.write_blob('demo',str(control['run_id']),'native-test-scope',{'roots':[]})
    _,_,resolved=await InvocationConfiguration(settings).resolve(selected,project_id='demo',run_id=str(control['run_id']),invocation_id=invocation)
    manifest=write_invocation_context(settings,project_id='demo',run_id=str(control['run_id']),invocation_id=invocation,
        config_ref=config,resolved_config_ref=resolved,scope_ref=scope,source_message_ids=[],accepted_documents={},
        skill_refs=[],public_context={},semantic_context=None)
    assembly=service.prompts.write_blob('demo',str(control['run_id']),'native-test-assembly',{'context_manifest_ref':manifest})
    await repo.start_model_invocation(invocation_id=invocation,project_id='demo',run_id=str(control['run_id']),session_id=None,
        actor_role='goal_supervisor',harness='native-fixture',provider='test',model='test',reasoning_effort='low',auth_mode='test',
        metadata={'assembly_ref':assembly,'fixture':True})
    await repo.finish_model_invocation(invocation_id=invocation,status='succeeded',native_session_id=parent)
    directory=service.documents.runtime.describe('demo').codex_home/'sessions/2026/10/09';directory.mkdir(parents=True)
    def write(path,rows):path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
    write(directory/('parent-'+parent+'.jsonl'),[
        {'type':'session_meta','payload':{'id':parent,'source':'exec'}},
        {'type':'response_item','payload':{'type':'function_call','name':'spawn_agent','call_id':'spawn'}},
        {'type':'response_item','payload':{'type':'function_call_output','call_id':'spawn','output':json.dumps({'task_name':name})}}])
    rows=[{'type':'session_meta','payload':{'id':child,'source':{'subagent':{'thread_spawn':{'parent_thread_id':parent,'agent_path':name}}}}},
        {'type':'response_item','payload':{'type':'message','role':'assistant','phase':'final_answer','content':[
            {'type':'output_text','text':json.dumps(prompt_result(context))}]}},
        {'type':'event_msg','payload':{'type':'task_complete'}}]
    path=directory/('child-'+child+'.jsonl');write(path,rows)
    ref=resource_ref(kind='receipt',identity=name,uri='db://assistant/model_invocations/'+invocation,
        schema_version='prompt-assembly-result.v1',purpose='Parent explicitly adopted this completed child')
    return repo,service,control,ref,path,rows,prompt_result(context)


async def test_kernel_reads_actual_parent_linked_prompt_result_without_model_retyping(core_settings):
    repo,service,control,ref,path,rows,expected=await native_fixture(core_settings)
    result,actual=await service.read_adopted_native_child(control=control,ref=ref)
    assert result==expected and actual['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual['kind']=='trace' and actual['uri'].endswith(path.name)


@pytest.mark.parametrize('change',['different_parent','not_spawned','not_completed','failed_parent'])
async def test_unproven_child_cannot_enter_prompt_preparation(core_settings,change):
    repo,service,control,ref,path,rows,expected=await native_fixture(core_settings)
    if change=='different_parent':rows[0]['payload']['source']['subagent']['thread_spawn']['parent_thread_id']=str(uuid.uuid4())
    elif change=='not_spawned':ref={**ref,'id':'/root/invented_child'}
    elif change=='not_completed':rows.pop()
    else:await repo.finish_model_invocation(invocation_id='native-parent-test',status='failed')
    path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
    with pytest.raises(PermissionError):await service.read_adopted_native_child(control=control,ref=ref)


async def test_required_native_result_is_frozen_and_rechecked_before_dispatch(core_settings):
    repo,service,control,ref,path,rows,expected=await native_fixture(core_settings)
    output=decision();output['public_result']['artifact_refs']=[ref]
    output['proposed_actions']=[]
    result=await service.receive_parent_result(control=control,worker_instance_id=str(control['worker_instance_id']),decision=output)
    proposal=service.documents.read('demo',result['prompt_proposal_ref'])
    children=proposal['payload']['child_result_refs'];assert len(children)==1
    record=service.documents.read('demo',children[0])
    assert record['payload']['actual_status']=='completed' and record['payload']['required'] is True
    task=await repo.get_run(proposal['task_id'])
    async with repo.database.connection() as c:
        await service.prompts.gates_locked(c,task,'requirements',required_child_refs=children)
    path.write_bytes(path.read_bytes()+b' ')
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError):await service.prompts.gates_locked(c,task,'requirements',required_child_refs=children)
