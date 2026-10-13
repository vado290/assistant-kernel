"""Actual query evidence and consultation settlement; write approval gates remain closed."""
from pathlib import Path
import pytest
from test_core4 import core_settings
from test_v12_p3_assessment import captured
from assistant_kernel.services.schedules import Schedules


async def question(settings):
    repo,service,task,control,worker,context,decision=await captured(settings,text='检查当前定时器状态，并列出已登记的定时任务；只查询，不修改')
    async with repo.database.connection() as c:
        for version in (8,9,10,11):await c.execute((Path(__file__).parents[1]/'migrations'/f'upgrade_core5_v{version}.sql').read_text())
        await c.commit()
    view=await Schedules(settings,repo).list('demo')
    assert view['items']==[] and view['scheduler']['enabled'] is False and 'query_ref' not in view
    assert not (service.documents.runtime.describe('demo').vault_root/'operations/queries/schedules').exists()
    decision.update(decision='consultation_reply',summary='定时器未启用，当前项目已登记 0 个定时任务。',
        public_result={'payload':{'decision':'consultation_reply'},'evidence_refs':[context['snapshot_ref']],'artifact_refs':[]})
    return repo,service,task,control,worker,context,decision


async def test_fresh_read_only_question_replies_without_prompt_or_fake_engineering_acceptance(core_settings):
    repo,service,task,control,worker,context,decision=await question(core_settings)
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    assert not result['pm_dispatched'] and result['prompt_preparation'] is None
    actual=await repo.get_run(str(task['run_id']))
    assert actual['status']=='succeeded' and actual['result_json']['outcome']=='read_only_answered'
    assert actual['result_json']['engineering_acceptance']=='not_applicable'
    assert not actual['request_json'].get('requirements_approval') and not actual['request_json'].get('design_approval')
    reply=await repo.get_message(result['phase_settlement']['reply_message_id'])
    assert reply['content_json']['interaction_kind']=='read_only_answer' and reply['reply_to_message_id']==task['trigger_message_id']
    async with repo.database.connection() as c:
        assert (await (await c.execute('SELECT count(*) n FROM assistant.project_schedules')).fetchone())['n']==0
        assert (await (await c.execute('SELECT count(*) n FROM assistant.schedule_ticks')).fetchone())['n']==0


async def test_side_query_does_not_complete_existing_work(core_settings):
    repo,service,task,control,worker,context,decision=await question(core_settings)
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET attempt=1,started_at=now() WHERE run_id=%s',(task['run_id'],));await c.commit()
    result=await service.settle(control=control,worker_instance_id=worker,decision=decision)
    assert result['phase_settlement']['query_answered'] and not result['phase_settlement']['task_completed']
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_supervisor'


async def test_read_only_classification_cannot_smuggle_a_prompt_or_bypass_write_gates(core_settings):
    repo,service,task,control,worker,context,decision=await question(core_settings)
    decision['prompt_proposal_ref']=context['snapshot_ref']
    with pytest.raises(PermissionError,match='read-only answer'):await service.settle(control=control,worker_instance_id=worker,decision=decision)
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='concrete user approval'):
            await service.prompts.gates_locked(c,task,'operate')
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_supervisor'
