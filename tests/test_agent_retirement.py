"""Retired Agent rejection and PM/Supervisor peer workflow regressions."""
import json
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

from test_core4 import core_settings, prepare_agent
from test_slimming import current_project, incoming
from assistant_kernel.api.v2 import build_v2_router
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.goal_supervisor import GoalSupervisorService
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.discord_interfaces import DiscordInterfaceRegistry


async def test_retired_agents_cannot_enter_public_runs_or_message_ingestion(core_settings):
    repo=CoreRepository(core_settings)
    app=FastAPI();app.include_router(build_v2_router(core_settings,repo,lambda:None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        for request in (
            {'interaction_kind':'child_task'},
            {'delegation':{'executor':'kernel_child_run'},'interaction_kind':'child_task'},
        ):
            response=await client.post('/v2/runs',json={'project_id':'demo','run_type':'python','request':request})
            assert response.status_code==409
    for suffix in ('child','operator'):
        with pytest.raises(ValueError,match='retired'):
            await repo.ingest_message(project_id='demo',agent_key=f'project:demo:{suffix}',channel='api',
                chat_id='c',user_id='u',external_event_id=uuid.uuid4().hex,external_message_id=None,content={'text':'obsolete'})
    assert await repo.list_runs('demo')==[]


def test_supervisor_contract_only_retains_native_delegation():
    for executor in ('kernel_child_run','operator'):
        with pytest.raises(ValueError,match='supported executor'):
            GoalSupervisorService.parse_decision_v2(json.dumps({'decision':'continue_current_stage','delegations':[{'executor':executor,'goal':'g'}]}))
    parsed=GoalSupervisorService.parse_decision_v2(json.dumps({'decision':'continue_current_stage',
        'delegations':[{'executor':'native_subagent','goal':'g'}]}))
    assert parsed['delegations'][0]['executor']=='native_subagent'


async def test_aux_config_archive_and_restore_does_not_change_business_state(core_settings):
    from scripts.retire_aux_agents import retire,restore
    root=current_project(core_settings);repo=CoreRepository(core_settings)
    base=yaml.safe_load((root/'agents/pm/config.yaml').read_text())
    for name in ('supervisor','child','operator'):
        folder=root/'agents'/name;folder.mkdir()
        data={**base,'agent_id':name,'agent_key':f'project:demo:{name}','kind':name,'aliases':[]}
        (folder/'config.yaml').write_text(yaml.safe_dump(data));(folder/'role.md').write_text(name)
    start=await incoming(repo)
    before=await repo.get_run(str(start['run']['run_id']))
    result=await retire(core_settings)
    assert set(result['archived_agents'])=={'demo:child','demo:operator'}
    assert {a.agent_id for a in AgentFileLoader(core_settings).list_agents('demo') if a.status=='active'}=={'pm','supervisor'}
    after=await repo.get_run(str(start['run']['run_id']))
    assert before==after
    assert await repo.schema_version()=='core5.v7'
    restore(core_settings)
    assert {a.agent_id for a in AgentFileLoader(core_settings).list_agents('demo') if a.status=='active'}=={'pm','supervisor','child','operator'}


def test_operator_discord_interface_cannot_be_registered(core_settings):
    registry=DiscordInterfaceRegistry(core_settings)
    with pytest.raises(ValueError,match='unsupported'):
        registry.upsert(channel_id='c',guild_id='g',project_id='demo',role='operator')
    registry.upsert(channel_id='s',guild_id='g',project_id='demo',role='supervisor')
    assert registry.resolve('s')['role']=='supervisor'


async def test_unsupported_child_action_does_not_mutate_parent(core_settings):
    repo=CoreRepository(core_settings)
    with pytest.raises(ValueError,match='retired'):
        await repo.commit_supervisor_action(supervisor_run_id=str(uuid.uuid4()),worker_instance_id=str(uuid.uuid4()),
            claim_token=str(uuid.uuid4()),task_id=str(uuid.uuid4()),source_context_epoch=0,source_turn_number=0,
            action_ref={},confirmation_class='safe',pm_instruction='continue',target_work_mode='requirements',
            delegations=[{'executor':'kernel_child_run','delegation_id':'d','goal':'g'}],result={},artifact_refs=[])
    assert await repo.list_runs('demo')==[]
