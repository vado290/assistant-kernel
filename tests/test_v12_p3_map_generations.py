from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.project_changes import ProjectChangeService
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.semantic_map_v2 import SemanticMapV2,MapGenerationChanged
from assistant_kernel.services.workspace_scope import AuthorizedRoot
from test_core4 import core_settings


async def baseline(settings):
    repo=CoreRepository(settings);legacy=SemanticMapService(settings);legacy.bootstrap('demo',purpose='isolated Map proof')
    vault=legacy.runtime.describe('demo').vault_root;roots=[AuthorizedRoot('project',vault,'write','isolated business files')]
    async with repo.database.connection() as c:
        await c.execute((Path(__file__).parents[1]/'migrations/upgrade_core5_v8.sql').read_text())
        await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES ('project-file-ledger:v2',%s)",
            (Jsonb({'write_enabled':True,'backfill_complete':True}),));await c.commit()
    changes=ProjectChangeService(settings,repo.database)
    for name in ('a.txt','b.txt'):
        target=vault/name;target.write_text(name)
        await changes.capture_batch('demo',batch_id='initial:'+name,roots=roots,
            changes=[{'target_uri':'workspace://project/'+name,'operation':'create','after_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}])
    return repo,legacy,SemanticMapV2(settings),changes,roots,vault


async def test_partial_refresh_keeps_unvisited_files_and_uses_one_json_writer(core_settings,monkeypatch):
    repo,legacy,writer,changes,roots,vault=await baseline(core_settings)
    old_yaml=(vault/'knowledge/map/index.yaml').read_bytes()
    first=writer.refresh('demo',roots=roots,full_scan=True)
    with pytest.raises(PermissionError,match='one JSON writer'):legacy.publish_current('demo')
    assert (vault/'knowledge/map/index.yaml').read_bytes()==old_yaml
    (vault/'a.txt').write_text('changed a')
    monkeypatch.setattr('os.walk',lambda *a,**k: (_ for _ in ()).throw(AssertionError('Partial refresh must not walk directories')))
    result=await changes.capture_batch('demo',batch_id='changed-a',roots=roots,
        changes=[{'target_uri':'workspace://project/a.txt','operation':'update','after_sha256':hashlib.sha256((vault/'a.txt').read_bytes()).hexdigest()}])
    assert 'map_refresh_pending' not in result
    index=legacy.load('demo');contracts().validate('Active_MapIndex',index)
    files={f['uri']:f for f in index['files']}
    assert files['workspace://project/b.txt']['sha256']==hashlib.sha256(b'b.txt').hexdigest()
    assert files['workspace://project/a.txt']['sha256']==hashlib.sha256(b'changed a').hexdigest()
    assert legacy.current_ref('demo')['generation_id']!=first['generation_id']


async def test_stale_generation_cannot_overwrite_newer_snapshot(core_settings):
    repo,legacy,writer,changes,roots,vault=await baseline(core_settings)
    first=writer.refresh('demo',roots=roots,full_scan=True);candidate=legacy.load('demo')
    (vault/'a.txt').write_text('later')
    await changes.capture_batch('demo',batch_id='later',roots=roots,changes=[{'target_uri':'workspace://project/a.txt',
        'operation':'update','after_sha256':hashlib.sha256(b'later').hexdigest()}])
    latest=writer.latest('demo')
    with pytest.raises(MapGenerationChanged):writer.publish_locked('demo',candidate,expected_generation=first['generation_id'],source_change_ids=[])
    assert writer.latest('demo')==latest


async def test_latest_success_with_failed_db_progress_recovers_without_new_generation(core_settings,monkeypatch):
    repo,legacy,writer,changes,roots,vault=await baseline(core_settings)
    actual=writer.repair_marks_locked;calls=0
    def fail_after_publication(connection,project):
        nonlocal calls
        calls+=1
        if calls==2: raise RuntimeError('isolated DB marker failure after latest swap')
        return actual(connection,project)
    monkeypatch.setattr(writer,'repair_marks_locked',fail_after_publication)
    with pytest.raises(RuntimeError):writer.refresh('demo',roots=roots,full_scan=True)
    latest=writer.latest('demo');assert legacy.current_ref('demo')['freshness']=='stale'
    with writer.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM assistant.project_file_changes WHERE map_applied_generation IS NULL').fetchone()['n']==2
    monkeypatch.setattr(writer,'repair_marks_locked',actual)
    recovered=writer.refresh('demo',roots=roots)
    assert recovered['generation_id']==latest['generation_id'] and recovered['recovered_progress']
    with writer.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM assistant.project_file_changes WHERE map_applied_generation IS NULL').fetchone()['n']==0
    assert legacy.current_ref('demo')['freshness']=='current'


async def test_concurrent_independent_updates_merge_committed_change_ids(core_settings):
    repo,legacy,writer,changes,roots,vault=await baseline(core_settings)
    first=writer.refresh('demo',roots=roots,full_scan=True)
    # Persist both independent source facts before either derived publication.
    records=[]
    for name in ('a.txt','b.txt'):
        target=vault/name;target.write_text('parallel '+name)
        original_refresh=changes.maps.refresh_files
        changes.maps.refresh_files=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('isolated delayed projection'))
        receipt=await changes.capture_batch('demo',batch_id='parallel:'+name,roots=roots,
            changes=[{'target_uri':'workspace://project/'+name,'operation':'update','after_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}])
        changes.maps.refresh_files=original_refresh;records.extend(receipt['changes'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:SemanticMapV2(core_settings).refresh('demo',roots=roots,expected_generation=first['generation_id']),range(2)))
    assert results[0]['generation_id']==results[1]['generation_id']
    latest=writer.latest('demo');manifest=json.loads((vault/'knowledge/map'/latest['manifest']).read_bytes())
    assert set(manifest['source_change_ids'])=={r['change_id'] for r in records}
    files={f['path']:f for f in legacy.load('demo')['files']}
    assert files['a.txt']['sha256']==hashlib.sha256(b'parallel a.txt').hexdigest()
    assert files['b.txt']['sha256']==hashlib.sha256(b'parallel b.txt').hexdigest()


async def test_accepted_document_commit_and_late_old_projection_keep_new_semantic_head(core_settings):
    from test_v12_p3_prompt_protocol import preparation
    from test_v12_foundations import requirement
    from assistant_kernel.services.document_runtime import resource_ref
    from assistant_kernel.services.document_projections_v12 import DocumentProjectionsV12
    repo,legacy,writer,_,roots,_=await baseline(core_settings)
    repo,compiler,task,_,_=await preparation(core_settings)
    source=resource_ref(kind='message',identity=str(task['trigger_message_id']),uri='db://assistant/messages/'+str(task['trigger_message_id']),purpose='actual isolated ingress')
    projections=DocumentProjectionsV12(core_settings,repo);refs=[];changes=[]
    for revision in (1,2):
        payload=requirement();payload['goal']='Count '+str(revision)+' lines';payload['requirements'][0]['source_refs']=[source]
        ref=compiler.documents.write_scoped_revision(project_id='demo',scope='task',object_id='task-contract',
            task_id=str(task['run_id']),run_id=str(task['run_id']),document_kind='task-contract',document_schema='task-contract.v1',
            payload=payload,status='submitted',actor_type='pm',actor_id=task['agent_key'],reason='isolated semantic revision',source_refs=[source])
        refs.append(ref)
        async with repo.database.connection() as c:
            owner=await (await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *',
                (Jsonb({'state_revision':revision,'accepted_documents':{'task_contract':ref}}),task['run_id']))).fetchone()
            result=await projections.record_locked(c,project='demo',owner=owner,refs=[ref],source_refs=[source])
            changes.append(result['changes'][0]);await c.commit()
    # Simulate old backfill/delayed processing ordering inside this disposable DB.
    async with repo.database.connection() as c:
        await c.execute("UPDATE assistant.project_file_changes SET recorded_at=now()+interval '1 day' WHERE change_id=%s",(changes[0]['change_id'],));await c.commit()
    writer.refresh('demo',roots=roots)
    index=legacy.load('demo');component=next(c for c in index['components'] if c.get('effective_ref')==refs[1])
    assert component['kind']=='artifact' and component['status']=='active'
    assert not any(c.get('effective_ref')==refs[0] for c in index['components'])
    assert len([f for f in index['files'] if f.get('document_id')==refs[1]['id']])==1
    assert index['source_basis']['task_versions']==[{'task_id':str(task['run_id']),'state_revision':2,'context_epoch':0}]
    async with repo.database.connection() as c:
        rows=await (await c.execute('SELECT map_applied_generation FROM assistant.project_file_changes WHERE change_id=ANY(%s)',
            ([item['change_id'] for item in changes],))).fetchall()
    assert all(row['map_applied_generation'] for row in rows)


async def test_document_projection_rolls_back_with_owner_and_maintenance_repairs_committed_work(core_settings):
    from test_v12_p3_prompt_protocol import preparation
    from assistant_kernel.services.document_projections_v12 import DocumentProjectionsV12
    from assistant_kernel.worker.main import _reconcile_project_changes
    repo,legacy,writer,_,roots,_=await baseline(core_settings)
    repo,compiler,task,display,_=await preparation(core_settings)
    ref=display['content_json']['prompt_ref'];projections=DocumentProjectionsV12(core_settings,repo)
    async with repo.database.connection() as c:
        owner=await (await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *',
            (Jsonb({'state_revision':1,'accepted_documents':{'confirmed_prompt':ref}}),task['run_id']))).fetchone()
        result=await projections.record_locked(c,project='demo',owner=owner,refs=[ref],source_refs=[ref]);await c.rollback()
    async with repo.database.connection() as c:
        assert not await (await c.execute('SELECT 1 FROM assistant.project_file_changes WHERE change_id=%s',(result['changes'][0]['change_id'],))).fetchone()
        owner=await (await c.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *',
            (Jsonb({'state_revision':1,'accepted_documents':{'confirmed_prompt':ref}}),task['run_id']))).fetchone()
        result=await projections.record_locked(c,project='demo',owner=owner,refs=[ref],source_refs=[ref]);await c.commit()
    await _reconcile_project_changes(core_settings,repo)
    assert any(c.get('effective_ref')==ref for c in legacy.load('demo')['components'])
    assert (await repo.get_run(str(task['run_id'])))['status']=='waiting_user'
