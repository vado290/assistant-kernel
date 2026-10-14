"""Actual isolated processes plus DB fences; no models or production tasks."""
import asyncio
import json
from pathlib import Path
import sys
import uuid

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.services.validation_process_v12 import ValidationProcessV12
from assistant_kernel.worker.python_process import inspect_python_group
from test_core4 import core_settings


async def claimed(settings):
    repo=CoreRepository(settings)
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:manager',channel='test',chat_id='validator',
        user_id='owner',external_event_id=str(uuid.uuid4()),external_message_id=None,content={'text':'isolated process test'})
    async with repo.database.connection() as c:
        task=await (await c.execute("""UPDATE assistant.runs SET status='running',attempt=1,claim_token=%s,
            worker_instance_id=%s,lease_until=now()+interval '90 seconds',request_json=request_json || %s WHERE run_id=%s RETURNING *""",
            (str(uuid.uuid4()),str(uuid.uuid4()),Jsonb({'protocol_revision':'1.2'}),incoming['run']['run_id']))).fetchone();await c.commit()
    return repo,task


def child(monkeypatch,tmp_path,code):
    script=tmp_path/'validator-child.py';script.write_text(code)
    real=asyncio.create_subprocess_exec
    async def launch(*argv,**kwargs):
        return await real(sys.executable,str(script),argv[-1],**kwargs)
    monkeypatch.setattr('assistant_kernel.services.validation_process_v12.asyncio.create_subprocess_exec',launch)


async def test_real_validator_process_result_is_pinned_and_clears_live_pid(core_settings,monkeypatch,tmp_path):
    repo,task=await claimed(core_settings)
    child(monkeypatch,tmp_path,"import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).with_name('result.json').write_text(json.dumps({'release_gate':'pass','status':'passed','issues':[]}))\n")
    result=await ValidationProcessV12(core_settings,repo).run(task=task,policy={},bundle={'bundle_sha256':'0'*64})
    after=await repo.get_run(str(task['run_id']))
    assert result['release_gate']=='pass' and result['process_receipt']['cleanup']['status']=='verified'
    assert result['process_receipt']['result_ref'] and after['process_pid'] is None
    assert after['executor_json']['validation_process']['outcome']=='succeeded'


async def test_completed_validator_evidence_recovers_without_a_second_process(core_settings,monkeypatch,tmp_path):
    repo,task=await claimed(core_settings);service=ValidationProcessV12(core_settings,repo)
    child(monkeypatch,tmp_path,"import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).with_name('result.json').write_text(json.dumps({'release_gate':'pass','status':'passed','issues':[]}))\n")
    original=await service.run(task=task,policy={},bundle={'bundle_sha256':'f'*64})
    current=await repo.get_run(str(task['run_id']))
    async def never_launch(*args,**kwargs):raise AssertionError('A completed validator must not run twice')
    monkeypatch.setattr('assistant_kernel.services.validation_process_v12.asyncio.create_subprocess_exec',never_launch)
    recovered=await service.run(task=current,policy={},bundle={'bundle_sha256':'f'*64})
    assert recovered['recovered_without_replay'] and recovered['process_receipt']==original['process_receipt']
    with pytest.raises(PermissionError):await service.run(task=current,policy={'changed':True},bundle={'bundle_sha256':'f'*64})


async def test_real_lingering_child_is_cleaned_and_cannot_pass_validation(core_settings,monkeypatch,tmp_path):
    repo,task=await claimed(core_settings)
    child(monkeypatch,tmp_path,"import subprocess,json,sys\nfrom pathlib import Path\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\nPath(sys.argv[1]).with_name('result.json').write_text(json.dumps({'release_gate':'pass','status':'passed','issues':[]}))\n")
    result=await ValidationProcessV12(core_settings,repo).run(task=task,policy={},bundle={'bundle_sha256':'1'*64})
    assert result['release_gate']=='fail' and result['process_receipt']['stop_reason']=='UnsettledDescendants'
    assert inspect_python_group(result['process_receipt']['process_identity'])['status']=='verified'


async def test_real_validator_cancellation_cleans_parent_and_descendants(core_settings,monkeypatch,tmp_path):
    repo,task=await claimed(core_settings)
    child(monkeypatch,tmp_path,"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\ntime.sleep(60)\n")
    stopped=asyncio.Event();running=asyncio.create_task(ValidationProcessV12(core_settings,repo).run(task=task,policy={},
        bundle={'bundle_sha256':'2'*64},cancel_event=stopped))
    for _ in range(50):
        row=await repo.get_run(str(task['run_id']))
        if row['executor_json'].get('validation_process',{}).get('pid'):break
        await asyncio.sleep(.05)
    stopped.set()
    with pytest.raises(asyncio.CancelledError):await running
    after=await repo.get_run(str(task['run_id']));receipt=after['executor_json']['validation_process']
    assert receipt['outcome']=='cancelled' and receipt['cleanup']['status']=='verified' and after['process_pid'] is None
    assert inspect_python_group(receipt['process_identity'])['status']=='verified'


async def test_unknown_cleanup_stays_reconciling_and_blocks_every_retry_path(core_settings,monkeypatch,tmp_path):
    repo,task=await claimed(core_settings)
    child(monkeypatch,tmp_path,"import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).with_name('result.json').write_text(json.dumps({'release_gate':'pass','status':'passed','issues':[]}))\n")
    async def unknown(*args,**kwargs):return {'status':'unknown','reason':'isolated missing identity proof'}
    monkeypatch.setattr('assistant_kernel.services.validation_process_v12.cleanup_python_group',unknown)
    with pytest.raises(RuntimeError,match='never replay'):await ValidationProcessV12(core_settings,repo).run(task=task,policy={},bundle={'bundle_sha256':'3'*64})
    assert await repo.retry_run(str(task['run_id']),str(task['worker_instance_id']),str(task['claim_token']),{'type':'UnknownCleanup'})=='reconciling'
    assert await repo.claim_run('cleanup-test',str(uuid.uuid4()),60) is None
    with pytest.raises(PermissionError,match='cleanup is unresolved'):
        await repo.reconcile_run(str(task['run_id']),'retry','test manual recovery')
    current=await repo.get_run(str(task['run_id']))
    async with repo.database.connection() as c:
        with pytest.raises(PermissionError,match='without replay'):await PromptProtocol(core_settings,repo).gates_locked(c,current,'build')


async def test_actual_unittest_timeout_cleans_descendants_inside_owned_validator(core_settings):
    from test_validation_runner import write_manifest
    from assistant_kernel.services.candidate_bundle import CandidateBundleService
    repo,task=await claimed(core_settings);service=ValidationProcessV12(core_settings,repo)
    root=service.prompts.documents.runtime.ensure('demo').vault_root
    candidate=root/'runs'/str(task['run_id'])/'candidate';candidate.mkdir(parents=True)
    write_manifest(candidate,{'test_slow.py':"import unittest,subprocess,sys,time\nclass TestSlow(unittest.TestCase):\n def test_child(self):\n  subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n  time.sleep(30)\n"})
    bundle=CandidateBundleService(core_settings).seal('demo',str(task['run_id']))
    policy={'required_scopes':['structure','component'],'python_unittest':[{'target_id':'slow-child','root_key':'project',
        'module':'test_slow','required':True,'timeout_seconds':1,'min_tests':1}]}
    result=await service.run(task=task,policy=policy,bundle=bundle)
    assert result['release_gate']=='fail' and result['process_receipt']['cleanup']['status']=='verified'
    assert inspect_python_group(result['process_receipt']['process_identity'])['status']=='verified'
    artifact=service.prompts.read_blob('demo',result['process_receipt']['result_ref'])
    underlying=(root/artifact['uri'].split('vault://demo/')[1]).read_bytes()
    report=json.loads(underlying)
    assert any(target['target_id']=='slow-child' and target['status']=='failed' for target in report['targets'])
