from dataclasses import replace
import json
import uuid

import pytest
from psycopg.types.json import Jsonb
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.project_grants import ProjectGrantService,policy_sha256
from assistant_kernel.services.published_agents import PublishedAgentService
from assistant_kernel.services.template_bundle import TemplateBundle
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from test_core4 import core_settings,prepare_agent
from test_v12_foundations import source


def candidate(settings):
    prepare_agent(settings)
    base=AgentFileLoader(settings).load('demo','project:demo:manager')
    bundle=TemplateBundle().preserve(settings.data_root)
    config,receipt=bundle.assemble(base,actor='pm',phase='requirements',conditions={'python_program_work'},
        condition_source_refs=[source()],local_role_append='',local_role_source_refs=[])
    return bundle,config,receipt


def test_complete_template_candidate_and_exact_conditional_loading(core_settings):
    bundle,config,receipt=candidate(core_settings)
    assert len(bundle.manifest['design_sources'])==29  # Dedicated intake Role retired.
    assert config.foundation_revision=='1.2' and config.sandbox=='danger-full-access'
    assert 'assistant-business-runtime-design' in receipt['loaded_skills']
    assert 'assistant-business-workflow' not in receipt['loaded_skills']
    assert receipt['tool_discovery_status']=='unverified'
    assert receipt['adopted'] is False
    with pytest.raises(ValueError,match='provenance'):
        bundle.assemble(config,actor='pm',phase='build',conditions=set(),condition_source_refs=[],
                        local_role_append='local custom rule',local_role_source_refs=[])


def test_published_package_cas_and_physical_edits_do_not_change_effective_role(core_settings):
    bundle,config,_=candidate(core_settings)
    service=PublishedAgentService(core_settings)
    scope={'project_ids':['demo'],'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True}
    ref=service.prepare('demo',configurations={'project:demo:manager/requirements':config},
        template_ref=bundle.ref('profiles.json'),adoption_scope=scope,source_refs=[source()])
    assert service.loader.load('demo','project:demo:manager').foundation_revision is None
    service.adopt('demo',package_ref=ref,expected_previous_ref=None,approval_refs=[source()])
    loaded=service.load('demo','project:demo:manager',phase='requirements')
    assert loaded.role_text==config.role_text
    live=core_settings.data_root/'vaults/demo/role.md'
    live.write_text('Unaccepted physical edit')
    assert service.load('demo','project:demo:manager',phase='requirements').role_text==config.role_text
    assert service.adopt('demo',package_ref=ref,expected_previous_ref=None,approval_refs=[source()])['duplicate']
    altered=replace(config,role_text=config.role_text+'\nAccepted future change',config_sha256='a'*64)
    next_ref=service.prepare('demo',configurations={'project:demo:manager/requirements':altered},
        template_ref=bundle.ref('profiles.json'),adoption_scope=scope,source_refs=[source()])
    with pytest.raises(ValueError,match='stale'):
        service.adopt('demo',package_ref=next_ref,expected_previous_ref=None,approval_refs=[source()])
    with pytest.raises(ValueError,match='required actor'):
        service.load('demo','project:demo:manager',phase='audit')


async def test_new_grants_are_visible_to_existing_run_without_role_downgrade(core_settings):
    repo=CoreRepository(core_settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='fixture',user_id='owner',external_event_id='grant-late',external_message_id=None,content={'text':'fixture'})
    run=incoming['run'];root=core_settings.data_root/'external';root.mkdir()
    policy={'key':'external','path':str(root),'access':'write','write_mode':'direct','native_mode':'enforced','recursive':True,'includes':[],'excludes':[]}
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.project_grants(grant_id,project_id,grant_kind,resource_key,subject_kind,policy_json,policy_sha256) VALUES (%s,'demo','file','external','project',%s,%s)",(str(uuid.uuid4()),Jsonb(policy),policy_sha256(policy)))
        await c.commit()
    assert any(x['key']=='external' for x in ProjectGrantService(core_settings).active_file_roots('demo',str(run['run_id'])))
    scopes=WorkspaceScopeService(core_settings)
    roots=scopes.resolve('demo',str(run['run_id']),'requirements',candidate_only=True,shared_agreement=True)
    selected={x.key:x for x in roots}
    assert selected['external'].access=='write' and selected['external'].permission_package=='directory-full.v1'
    assert selected['project'].recursive and selected['project'].access=='write'
    assert all(x.native_mode=='behavioral' for x in roots)


async def test_mcp_discovery_checks_actual_tools_before_model_invocation(tmp_path):
    from test_runtime_v2 import write_v2_project
    from assistant_kernel.services.execution_plan import ProjectResourceResolver
    from assistant_kernel.services.mcp_discovery import discover_mcp
    settings,root=write_v2_project(tmp_path,with_mcp=True)
    (root/'mcp/local-tools/server.py').write_text(
        'from mcp.server.mcpserver import MCPServer\nserver=MCPServer(name="discovery-proof")\n'
        '@server.tool()\ndef echo_value(value: str) -> dict:\n    return {"value":value}\n'
        'server.run(transport="stdio")\n',encoding='utf-8')
    config=AgentFileLoader(settings).load('demo','project:demo:analyst')
    _,bindings=ProjectResourceResolver(settings).resolve_mcp_servers('demo',config)
    receipt=await discover_mcp(bindings[0])
    assert receipt['loaded_tools']==['echo_value']
    assert len(receipt['tool_schema_sha256']['echo_value'])==64
    with pytest.raises(Exception):
        await discover_mcp({**bindings[0],'enabled_tools':['missing_required_tool']})


async def test_invocation_refreshes_grants_without_rebuilding_business_package(core_settings):
    from assistant_kernel.services.execution_plan import ProjectResourceResolver
    _, config, _ = candidate(core_settings)
    repo = CoreRepository(core_settings)
    incoming = await repo.ingest_message(project_id='demo', agent_key=config.agent_key,
        channel='test', chat_id='scope-refresh', user_id='owner', external_event_id='scope-refresh',
        external_message_id=None, content={'text': 'preserve the fixed requirement'})
    run = incoming['run']; resolver = ProjectResourceResolver(core_settings)
    snapshot, digest = AgentFileLoader(core_settings).snapshot('demo', config)
    plan, _, uri, sha = resolver.materialize(run=run, config=config,
        config_snapshot_uri=snapshot, config_snapshot_sha256=digest, provider='openai',
        model='test', reasoning_effort='medium', timeout_seconds=60,
        session={'logical_session_id': str(uuid.uuid4()), 'native_session_id': None})
    fixed_bytes = resolver.runtime.resolve_vault_file('demo', uri.removeprefix('vault://demo/')).read_bytes()
    root = core_settings.data_root / 'later-grant'; root.mkdir()
    policy = {'key': 'later', 'path': str(root), 'access': 'write', 'write_mode': 'direct',
        'native_mode': 'behavioral', 'recursive': True, 'includes': [], 'excludes': []}
    grant_id = str(uuid.uuid4())
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.project_grants(grant_id,project_id,grant_kind,resource_key,subject_kind,policy_json,policy_sha256) VALUES (%s,'demo','file','later','project',%s,%s)",
            (grant_id, Jsonb(policy), policy_sha256(policy)))
        await c.commit()
    refreshed, next_uri, next_sha = resolver.bind_current_file_agreement(plan, invocation_id='grant-added')
    assert grant_id in refreshed['bindings']['active_file_grant_ids']
    assert refreshed['bindings']['config_snapshot_uri'] == snapshot
    assert refreshed['bindings']['semantic_context'] == plan['bindings']['semantic_context']
    assert resolver.load_materialized('demo', next_uri, next_sha)[0] == refreshed
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.project_grants SET status='revoked',revoked_at=now() WHERE grant_id=%s", (grant_id,))
        await c.commit()
    revoked, _, _ = resolver.bind_current_file_agreement(refreshed, invocation_id='grant-revoked')
    assert grant_id not in revoked['bindings']['active_file_grant_ids']
    assert resolver.runtime.resolve_vault_file('demo', uri.removeprefix('vault://demo/')).read_bytes() == fixed_bytes
    assert resolver.load_materialized('demo', uri, sha)[0] == plan


async def test_full_agreement_does_not_reuse_a_legacy_gated_directory(core_settings):
    import yaml
    from assistant_kernel.services.source_scope import SourceScopeService
    from test_core4 import prepare_bindable_project
    settings = core_settings.model_copy(update={'workspace_root': core_settings.data_root})
    prepare_bindable_project(settings, 'demo')
    path = settings.data_root / 'external'; path.mkdir()
    catalog = settings.data_root / 'config/source-discovery.yaml'
    catalog.write_text(yaml.safe_dump({'schema_version': 'source-discovery.v1',
        'roots': [{'discovery_id': 'fixture', 'path': str(settings.workspace_root), 'display_path': 'TEST'}]}))
    repo = CoreRepository(settings); service = SourceScopeService(settings, repo)
    old_id = str(uuid.uuid4())
    policy = {'key': 'external', 'path': str(path), 'access': 'write', 'write_mode': 'gated',
        'native_mode': 'mcp_only', 'recursive': True, 'includes': [], 'excludes': []}
    async with repo.database.connection() as c:
        await c.execute("INSERT INTO assistant.project_grants(grant_id,project_id,grant_kind,resource_key,subject_kind,policy_json,policy_sha256) VALUES (%s,'demo','file','external','project',%s,%s)",
            (old_id, Jsonb(policy), policy_sha256(policy)))
        # This internal step is called only after the separate real approval gate.
        result = await service.apply_full_directory_grants(connection=c, target_project_id='demo',
            bindings=[{'discovery_id': 'fixture', 'relative_path': 'external', 'root_key': 'external'}],
            source_binding={'isolated_approval_fixture': True}, source_message_id=None, approval_message_id=None)
        await c.commit()
    assert old_id in result['superseded_grant_ids']
    active = ProjectGrantService(settings).active_file_roots('demo')
    assert active[0]['write_mode'] == 'direct' and active[0]['grant_id'] != old_id
