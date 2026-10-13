from dataclasses import replace
from types import SimpleNamespace
import json
import uuid

from assistant_kernel.harness.base import AgentTurnResult
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.context_budget import TurnUsage
from assistant_kernel.services.codex_models import ResolvedCodexModel
from assistant_kernel.services.goal_supervisor import GoalSupervisorService
from assistant_kernel.services.published_agents import PublishedAgentService
from test_core4 import core_settings
from test_v12_foundations import seed,source
from test_v12_packages import candidate


async def test_supervisor_uses_leased_control_actor_fixed_skills_and_discovered_tools(core_settings,monkeypatch):
    await seed(core_settings)
    bundle,pm,_=candidate(core_settings)
    base=replace(pm,agent_id='supervisor',agent_key='project:demo:supervisor',kind='supervisor',
                 allowed_tools=('get_authorized_workspace',),phase_mode=None)
    config,_=bundle.assemble(base,actor='supervisor',phase=None,conditions=set(),condition_source_refs=[],
        local_role_append='',local_role_source_refs=[])
    (core_settings.data_root/'vaults/demo/project.yaml').write_text(
        'task_protocol: single-agent.v2\nsupervisor:\n  enabled: true\n',encoding='utf-8')
    service=PublishedAgentService(core_settings)
    ref=service.prepare('demo',configurations={'project:demo:supervisor/default':config},
        template_ref=bundle.ref('profiles.json'),adoption_scope={'project_ids':['demo'],
        'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True},source_refs=[source()])
    service.adopt('demo',package_ref=ref,expected_previous_ref=None,approval_refs=[source()])
    repo=CoreRepository(core_settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',
        chat_id='supervisor-proof',user_id='owner',external_event_id='supervisor-proof',external_message_id=None,content={'text':'bounded inspection'})
    async with repo.database.connection() as c:
        task=await (await c.execute("UPDATE assistant.runs SET status='waiting_supervisor' WHERE run_id=%s RETURNING *",(incoming['run']['run_id'],))).fetchone()
        await repo._queue_supervision_locked(c,task,str(incoming['message']['message_id']),reason='isolated_assembly_test')
        await c.commit()
    worker=str(uuid.uuid4());run=await repo.claim_run('supervisor-proof',worker,120)
    captured=[]
    class Harness:
        def __init__(self,*args):self.model_catalog=SimpleNamespace(cache_ttl_seconds=300)
        def authentication_mode(self,project):return 'test'
        async def resolve_model_profile(self,**kwargs):
            return ResolvedCodexModel('openai',kwargs['model'],kwargs['reasoning_effort'],'max',32768,28000,32768,32768,32768,100)
        async def run_turn(self,request):
            captured.append(request)
            return AgentTurnResult(str(uuid.uuid4()),TurnUsage(input_tokens=1,output_tokens=1),[],
                'isolated model stub; actual discovery already completed')
    monkeypatch.setattr('assistant_kernel.services.goal_supervisor.CodexCliHarness',Harness)
    supervisor=GoalSupervisorService(core_settings,repo)
    selected = await supervisor.invocation_configuration('demo', run)
    assert selected['models']['main_evaluation']['model'] == 'gpt-5.6-sol'
    refreshed = await repo.get_run(str(run['run_id']))
    assert await supervisor.invocation_configuration('demo', refreshed) == selected
    from assistant_kernel.services.recovery import RecoveryService
    assert RecoveryService(core_settings, repo)._tier(refreshed, {}) == 3
    await supervisor._codex_ephemeral(project_id='demo',prompt='bounded purpose',invocation_id='sup-proof',
        model='gpt-5.6-sol',reasoning_effort='medium',timeout_seconds=90,
        task_run_id=str(run['run_id']),control_run_id=str(run['run_id']))
    request=captured[0]
    assert not request.ephemeral and request.physical_session_id is None
    assert request.sandbox=='danger-full-access'
    assert set(request.scoped_tools)==set(config.allowed_tools)
    assert {'preview_schedule','list_schedules','schedule_status','list_schedule_ticks'}<=set(request.scoped_tools)
    assert not {'register_schedule','control_schedule','submit_project_python'}&set(request.scoped_tools)
    assert request.scope_context['KERNEL_SCOPE_RUN_ID']==str(run['run_id'])
    assert 'assistant-prompt-assembly' in request.prompt
    receipt=json.loads((core_settings.data_root/'vaults/demo/runtime/invocation-assemblies/sup-proof.json').read_text())
    assert set(receipt['mcp_discovery'][0]['requested_tools'])==set(config.allowed_tools)
    assert receipt['mcp_discovery'][0]['loaded_tools']==[]
    assert receipt['mcp_discovery'][0]['status']=='declared'
    assert receipt['native_isolation'] is False
    assert receipt['ephemeral'] is False and receipt['native_history_resumed'] is False
    from assistant_kernel.services.document_runtime import TaskDocumentService
    context = TaskDocumentService(core_settings).read('demo', receipt['context_manifest_ref'])['payload']
    assert context['resolved_config_ref'] == receipt['resolved_config_snapshot_ref']
    await supervisor._codex_ephemeral(project_id='demo',prompt='second independent purpose',invocation_id='sup-proof-next',
        model='gpt-5.6-sol',reasoning_effort='medium',timeout_seconds=90,
        task_run_id=str(run['run_id']),control_run_id=str(run['run_id']),native_receipt_invocation_id='sup-proof')
    assert len(captured)==2
    assert all(request.physical_session_id is None and not request.ephemeral for request in captured)
    assert 'uri db://assistant/model_invocations/sup-proof, revision_id' in captured[-1].prompt
    assert 'uri db://assistant/model_invocations/sup-proof-next, revision_id' not in captured[-1].prompt


async def test_legacy_supervisor_shares_files_without_adopting_new_output_protocol(core_settings, monkeypatch):
    import yaml
    from assistant_kernel.storage import ProjectRuntimeManager
    root = ProjectRuntimeManager(core_settings.data_root).ensure('demo').vault_root
    (root/'project.yaml').write_text(yaml.safe_dump({'schema_version': 'project.v2', 'project_id': 'demo',
        'layout_version': 2, 'default_agent': 'pm', 'status': 'active'}))
    agent = root/'agents/supervisor'; agent.mkdir(parents=True)
    (agent/'role.md').write_text('Legacy Supervisor role; evaluation is read-only.')
    (agent/'config.yaml').write_text(yaml.safe_dump({'schema_version': 'agent-config.v2',
        'agent_id': 'supervisor', 'agent_key': 'project:demo:supervisor', 'kind': 'supervisor',
        'status': 'active', 'role': 'role.md', 'skills': [], 'tools': [], 'mcp_servers': {},
        'scope_profile': 'current-project', 'harness': {'provider': 'openai', 'model': 'gpt-test',
            'reasoning_effort': 'low', 'sandbox': 'read-only', 'timeout_seconds': 60}}))
    repo = CoreRepository(core_settings)
    incoming = await repo.ingest_message(project_id='demo', agent_key='project:demo:manager', channel='test',
        chat_id='legacy-supervisor', user_id='owner', external_event_id='legacy-sup', external_message_id=None,
        content={'text': 'Keep the existing business protocol'})
    async with repo.database.connection() as c:
        task = await (await c.execute("UPDATE assistant.runs SET status='waiting_supervisor' WHERE run_id=%s RETURNING *", (incoming['run']['run_id'],))).fetchone()
        await repo._queue_supervision_locked(c, task, str(incoming['message']['message_id']), reason='isolated_legacy_assembly')
        await c.commit()
    worker = str(uuid.uuid4()); run = await repo.claim_run('legacy-assembly', worker, 120)
    requests = []
    class Harness:
        def __init__(self, *args): pass
        def authentication_mode(self, project): return 'test'
        async def run_turn(self, request):
            requests.append(request)
            return AgentTurnResult(str(uuid.uuid4()), TurnUsage(input_tokens=1), [], 'legacy model stub')
    monkeypatch.setattr('assistant_kernel.services.goal_supervisor.CodexCliHarness', Harness)
    await GoalSupervisorService(core_settings, repo)._codex_ephemeral(project_id='demo',
        prompt='Retain the existing output contract', invocation_id='legacy-assembly', model='gpt-test',
        reasoning_effort='low', timeout_seconds=60, task_run_id=str(task['run_id']), control_run_id=str(run['run_id']))
    assert requests[0].sandbox == 'danger-full-access' and requests[0].ephemeral
    assert 'get_authorized_workspace' in requests[0].scoped_tools
    record = json.loads((root/'runtime/invocation-assemblies/legacy-assembly.json').read_text())
    assert record['business_output_protocol_changed'] is False
    state = await repo.get_run(str(run['run_id']))
    assert state['executor_json']['file_agreement_revision'] == '1.2'
