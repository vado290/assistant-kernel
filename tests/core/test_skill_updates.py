"""Isolated skill-adoption batches; no installed project files or production database are used."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import threading
import zipfile

import pytest
import yaml
from psycopg.types.json import Jsonb

from assistant_kernel.core import skill_updates as su, skills, vault_sync
from .sim import next_step


def wheel(path, trees, modes=None):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, tree in trees.items():
            for relative, raw in tree.items():
                info = zipfile.ZipInfo(f'assistant_kernel/prompts/skills/{name}/{relative}')
                info.external_attr = ((0o100000 | (modes or {}).get(relative, 0o644)) << 16)
                archive.writestr(info, raw)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(root, key, tree):
    agent, name = key.split('/')
    destination = skills.agent_skill_dir(root, agent) / name
    for relative, raw in tree.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return destination


async def policy(kernel, release, enabled=True, project_id='p1'):
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.projects SET settings=%s WHERE project_id=%s",
                        (Jsonb({'skill_updates': {'enabled': enabled, 'releases': [release]}}), project_id))


@pytest.fixture
def release(kernel_settings):
    root = kernel_settings.data_root / 'vaults/p1'
    old = {'SKILL.md': b'---\nname: example\ndescription: purpose\nmetadata:\n  version: "1"\n  load: {roles: [pm, supervisor]}\n---\n\nOld method.\n',
           'scripts/helper.py': b'VALUE = 1\n'}
    new = {k: v.replace(b'"1"', b'"2"').replace(b'Old method', b'New method').replace(b'VALUE = 1', b'VALUE = 2') for k, v in old.items()}
    base_path, target_path = kernel_settings.data_root / 'old.whl', kernel_settings.data_root / 'new.whl'
    base_sha, target_sha = wheel(base_path, {'example': old}), wheel(target_path, {'example': new})
    for key in ('pm/example', 'supervisor/example'):
        install(root, key, su._stamp(old, old))
    return {'base_wheel': str(base_path), 'base_sha256': base_sha,
            'target_wheel': str(target_path), 'target_sha256': target_sha,
            'selections': ['pm/example', 'supervisor/example'], 'authorization_ref': 'test:approved-policy-v1',
            'compatible_with_active_tasks': True, 'rollback_allowed': True}


def trees(kernel, release, project_id='p1'):
    vault = kernel.settings.data_root / 'vaults' / project_id
    return {key: su._tree(skills.agent_skill_dir(vault, key.split('/')[0]) / key.split('/')[1]) for key in release['selections']}


@pytest.mark.parametrize('case,roles,valid', [
    ('omitted', None, True), ('null', None, True), ('empty', [], True),
    ('matching', ['pm', 'supervisor'], True), ('mismatch', ['analyst'], False),
    ('string', 'pm', False), ('number', 0, False), ('boolean', False, False),
    ('mapping', {'pm': True}, False), ('invalid_member', ['pm', 1], False),
])
async def test_roles_follow_loader_semantics_or_block_with_deduplicated_handoff(kernel, sim, release, case, roles, valid):
    old = su._wheel(release['base_wheel'], release['base_sha256'], {'example'})['example']
    target = su._wheel(release['target_wheel'], release['target_sha256'], {'example'})['example']
    load = {'stages': ['build']}
    if case != 'omitted':
        load['roles'] = roles
    def set_load(tree):
        front, body = su._document(tree['SKILL.md'])
        front['metadata']['load'] = load
        return {**tree, 'SKILL.md': ('---\n' + yaml.safe_dump(front, sort_keys=False) + '---\n' + body + '\n').encode()}
    target = set_load(target)
    if valid:
        old = set_load(old)
        release['base_sha256'] = wheel(Path(release['base_wheel']), {'example': old})
        for key in release['selections']:
            install(kernel.settings.data_root / 'vaults/p1', key, su._stamp(old, old))
        for agent in ('pm', 'supervisor'):
            assert skills.matches(load, role=agent, stage='build')
            su._validate(target, agent, 'example')
    release['target_sha256'] = wheel(Path(release['target_wheel']), {'example': target})
    before = trees(kernel, release)
    await policy(kernel, release)
    assert await su.maintain(kernel) == int(valid)
    rows = await su.list_updates(kernel, 'p1')
    assert len(rows) == 1 and rows[0]['status'] == ('applied' if valid else 'blocked')
    assert await su.maintain(kernel) == 0
    assert (await su.list_updates(kernel, 'p1'))[0]['update_id'] == rows[0]['update_id']
    messages = await sim.outbound('p1')
    if valid:
        assert not messages
        for key in release['selections']:
            agent, name = key.split('/')
            skill = skills.parse(skills.agent_skill_dir(kernel.settings.data_root / 'vaults/p1', agent) / name / 'SKILL.md')
            assert skill.version == '2' and skills.matches(skill.load, role=agent, stage='build')
    else:
        expected = 'Target skill does not serve this agent' if case == 'mismatch' else 'load.roles must be a list'
        assert expected in rows[0]['reason']
        assert trees(kernel, release) == before
        assert len(messages) == 1 and str(rows[0]['update_id']) in messages[0]['body']['text']


@pytest.mark.parametrize('which', ['base', 'target'])
def test_wheel_replacement_after_hash_cannot_change_content_or_modes(kernel_settings, release, monkeypatch, which):
    path = Path(release[which + '_wheel'])
    expected_sha = release[which + '_sha256']
    approved, modes = su._wheel(path, expected_sha, {'example'}, with_modes=True)
    replacement = kernel_settings.data_root / 'replacement.whl'
    altered = {**approved['example'], 'scripts/helper.py': b'VALUE = 999\n'}
    wheel(replacement, {'example': altered}, {'scripts/helper.py': 0o755})
    approved_bytes, other_bytes = path.read_bytes(), replacement.read_bytes()
    original_sha = su._sha
    replaced = []
    def swap_after_hash(raw):
        result = original_sha(raw)
        if raw == approved_bytes and not replaced:
            path.write_bytes(other_bytes)
            replaced.append(True)
        return result
    monkeypatch.setattr(su, '_sha', swap_after_hash)
    plan, snapshots = su.preview(kernel_settings, 'p1', release)
    assert replaced and path.read_bytes() == other_bytes and not plan['problems']
    expected_target = b'VALUE = 2\n'
    for item in plan['items']:
        assert item[which] == su._manifest(approved['example'])
        assert item['after_modes']['scripts/helper.py'] == 0o644
        assert snapshots[item['key']]['after']['scripts/helper.py'] == expected_target
    with pytest.raises(ValueError, match='hash'):
        su._wheel(path, expected_sha, {'example'})  # a later operation sees the replaced package and refuses it


async def test_invalid_candidate_is_a_visible_blocked_batch_with_one_notification(kernel, sim, release):
    before = trees(kernel, release)
    target = su._wheel(release['target_wheel'], release['target_sha256'], {'example'})['example']
    target['scripts/helper.py'] = b'def broken('
    release['target_sha256'] = wheel(Path(release['target_wheel']), {'example': target})
    await policy(kernel, release)
    assert await su.maintain(kernel) == 0
    rows = await su.list_updates(kernel, 'p1')
    assert len(rows) == 1 and rows[0]['status'] == 'blocked'
    assert 'scripts/helper.py:1' in rows[0]['reason'] and 'invalid Python candidate' in rows[0]['reason']
    assert trees(kernel, release) == before
    await su.maintain(kernel)
    assert (await su.list_updates(kernel, 'p1'))[0]['update_id'] == rows[0]['update_id']
    messages = await sim.outbound('p1')
    assert len(messages) == 1 and str(rows[0]['update_id']) in messages[0]['body']['text']
    # A corrected, newly authorized candidate can complete through the same workflow.
    target['scripts/helper.py'] = b'VALUE = 2\n'
    corrected = {**release, 'authorization_ref': 'test:corrected-approved-candidate',
                 'target_sha256': wheel(Path(release['target_wheel']), {'example': target})}
    await policy(kernel, corrected)
    assert await su.maintain(kernel) == 1
    latest = await su.list_updates(kernel, 'p1')
    applied = next(r for r in latest if r['status'] == 'applied')
    assert {r['status'] for r in latest} == {'blocked', 'applied'}
    # Explicitly close the superseded request through the existing operator handoff, not a new state/table.
    await su.defer(kernel, rows[0]['update_id'], f"Corrected by applied update {applied['update_id']}")
    assert {r['status'] for r in await su.list_updates(kernel, 'p1')} == {'deferred', 'applied'}
    assert await su.maintain(kernel) == 0


async def test_disabled_by_default_and_read_only_preview(kernel, release):
    before = trees(kernel, release)
    plan, _ = su.preview(kernel.settings, 'p1', release)
    assert not plan['problems']
    assert await su.list_updates(kernel, 'p1') == []
    assert await vault_sync._sync_templates(kernel, kernel.settings) == 0
    row = await su.prepare(kernel, 'p1', release)
    with pytest.raises(PermissionError):
        await su.execute(kernel, row['update_id'])
    assert trees(kernel, release) == before
    assert await su.list_updates(kernel, 'p2', update_id=row['update_id']) == []


async def test_missing_baseline_is_visible_and_does_not_block_other_turns(kernel, release):
    release['base_sha256'] = '0' * 64
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    assert row['status'] == 'blocked' and 'hash' in row['reason']
    await kernel.receive('p1', role='pm', text='normal work')
    assert (await kernel.claim('w1'))['project_id'] == 'p1'


async def test_project_gate_preserves_oldest_run_order(kernel):
    await kernel.receive('p2', role='pm', text='older')
    await kernel.receive('p1', role='pm', text='newer')
    assert (await kernel.claim('w1'))['project_id'] == 'p2'


@pytest.mark.parametrize('damage', ['snapshot', 'plan'])
async def test_corrupt_prepared_evidence_is_visible_and_never_written(kernel, sim, release, damage):
    await policy(kernel, release)
    before = trees(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    if damage == 'snapshot':
        (su._root(kernel.settings, 'p1', row['update_id']) / 'after/pm/example/SKILL.md').unlink()
        assert (await su.execute(kernel, row['update_id']))['status'] == 'blocked'
    else:
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.skill_updates SET plan=plan || '{\"tampered\":true}'::jsonb WHERE update_id=%s", (row['update_id'],))
        with pytest.raises(ValueError, match='Modified'): await su.execute(kernel, row['update_id'])
    assert (await su.list_updates(kernel, 'p1'))[0]['status'] == 'blocked'
    assert trees(kernel, release) == before
    assert len(await sim.outbound('p1')) == 1
    await su.maintain(kernel)
    assert len(await sim.outbound('p1')) == 1


async def test_adoption_evidence_tool_is_read_only_and_project_scoped(kernel, sim, kernel_settings, tmp_path, release):
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from assistant_kernel.core.executors.common import workspace_snapshot
    from .test_scoped_tools_core import private_config, payload
    row = await su.prepare(kernel, 'p1', release)
    task_id = (await kernel.receive('p1', role='pm', text='inspect adoption'))['task_id']
    run = await kernel.claim('reader')
    snapshot = workspace_snapshot(kernel_settings, 'p1', task_id, 'probe', candidates=False)
    env = private_config(kernel_settings, run, task_id, snapshot, tmp_path,
                         tools=('get_authorized_workspace', 'list_skill_updates'), stage='requirements')
    server = StdioServerParameters(command=sys.executable, args=['-m', 'assistant_kernel.core.tools'], env=env)
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {'get_authorized_workspace', 'list_skill_updates'}
        await session.call_tool('get_authorized_workspace', {})
        data = payload(await session.call_tool('list_skill_updates', {'update_id': str(row['update_id'])}))
        assert data['count'] == 1 and data['updates'][0]['status'] == 'prepared'
    assert await su.list_updates(kernel, 'p2', update_id=row['update_id']) == []


@pytest.mark.parametrize('custom', ['description', 'load', 'body', 'script', 'extra_file', 'removed'])
async def test_entire_custom_skill_is_preserved(kernel, release, custom):
    root = skills.agent_skill_dir(kernel.settings.data_root / 'vaults/p1', 'pm') / 'example'
    path = root / 'SKILL.md'
    if custom == 'removed':
        import shutil
        shutil.rmtree(root)
    elif custom == 'extra_file':
        (root / 'local.txt').write_text('project data')
    elif custom == 'script':
        (root / 'scripts/helper.py').write_text('VALUE = 42\n')
    else:
        text = path.read_text()
        text = text.replace('purpose', 'local purpose') if custom == 'description' else (
            text.replace('  load:\n', '  load:\n    on_demand: true\n') if custom == 'load' else text + '\nLocal method.\n')
        path.write_text(text)
    before = trees(kernel, release)
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    assert row['status'] == 'blocked'
    assert (await su.execute(kernel, row['update_id']))['status'] == 'blocked'
    assert await su.maintain(kernel) == 0 and trees(kernel, release) == before


async def test_apply_is_idempotent_and_old_card_remains_valid(kernel, sim, release):
    task_id = (await kernel.receive('p1', role='pm', text='existing task'))['task_id']
    sim.script('supervisor:evaluate', next_step('requirements'))
    await sim.drain()
    old_task, old_card = await kernel.get_task(task_id), await sim.item('p1')
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    assert await su.maintain(kernel) == 1
    assert await kernel.get_task(task_id) == old_task and await sim.item('p1') == old_card
    assert await su.maintain(kernel) == 0
    assert (await su.prepare(kernel, 'p1', release))['update_id'] == row['update_id']
    records = await kernel.iterations('p1')
    assert len(records) == 1 and records[0]['effect_status'] is None
    assert str(row['update_id']) in records[0]['source_ref']
    await kernel.act('p1', old_card['item_id'], 'execute')
    assert (await kernel.claim('w1'))['input']['stage'] == 'requirements'


async def test_running_run_and_incompatible_waiting_task_defer_adoption(kernel, sim, release):
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    await kernel.receive('p1', role='pm', text='busy')
    run = await kernel.claim('w1')
    assert (await su.execute(kernel, row['update_id']))['status'] == 'prepared'
    await kernel.complete(run, next_step('requirements'))
    incompatible = {**release, 'authorization_ref': 'test:new-policy', 'compatible_with_active_tasks': False}
    await policy(kernel, incompatible)
    second = await su.prepare(kernel, 'p1', incompatible)
    assert (await su.execute(kernel, second['update_id']))['status'] == 'prepared'


async def test_claim_and_recovery_cannot_enter_while_writer_is_live(kernel, release, monkeypatch):
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    await kernel.receive('p1', role='pm', text='next turn')
    await kernel.receive('p2', role='pm', text='other project')
    started, resume = threading.Event(), threading.Event()
    original = su._write_file
    def paused(*args):
        original(*args)
        if not started.is_set():
            started.set()
            assert resume.wait(20)
    monkeypatch.setattr(su, '_write_file', paused)
    work = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(su.execute(kernel, row['update_id']))))
    try:
        for _ in range(500):
            if started.is_set(): break
            await asyncio.sleep(.01)
        assert started.is_set()
        claim = await kernel.claim('other')
        assert claim['project_id'] == 'p2'
        assert await kernel.claim('blocked') is None
        with pytest.raises(BlockingIOError):
            await su.recover(kernel, row['update_id'])
    finally:
        resume.set()
        result = await work
    assert result['status'] == 'applied'
    assert (await kernel.claim('released'))['project_id'] == 'p1'


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize('rollback', [False, True])
async def test_interrupted_apply_or_rollback_restores_batch_without_reapplying(kernel, release, monkeypatch, rollback):
    before = trees(kernel, release)
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    if rollback:
        await su.execute(kernel, row['update_id'])
    original = su._write_file
    def crash(*args):
        original(*args)
        raise SimulatedCrash()
    with monkeypatch.context() as patch:
        patch.setattr(su, '_write_file', crash)
        with pytest.raises(SimulatedCrash):
            await su.execute(kernel, row['update_id'], rollback=rollback)
    assert (await su.recover(kernel, row['update_id']))['status'] == 'rolled_back'
    assert trees(kernel, release) == before
    assert await su.maintain(kernel) == 0
    assert len(await su.list_updates(kernel, 'p1')) == 1
    reordered = {**release, 'selections': list(reversed(release['selections']))}
    await policy(kernel, reordered)
    assert (await su.prepare(kernel, 'p1', reordered))['update_id'] == row['update_id']
    assert await su.maintain(kernel) == 0
    renewed = {**release, 'authorization_ref': 'test:explicit-new-approval'}
    await policy(kernel, renewed)
    assert await su.maintain(kernel) == 1


async def test_missing_receipt_recovers_completed_write(kernel, release, monkeypatch):
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    original = su._mark
    async def fail_receipt(c, row, status, *a, **kw):
        if status == 'applied': raise SimulatedCrash()
        return await original(c, row, status, *a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(su, '_mark', fail_receipt)
        with pytest.raises(SimulatedCrash): await su.execute(kernel, row['update_id'])
    assert (await su.recover(kernel, row['update_id']))['status'] == 'applied'
    assert (await su.recover(kernel, row['update_id']))['status'] == 'applied'
    assert len(await kernel.iterations('p1')) == 1


async def test_later_edit_and_revoked_policy_are_not_overwritten(kernel, release):
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    await policy(kernel, release, enabled=False)
    with pytest.raises(PermissionError): await su.execute(kernel, row['update_id'])
    await policy(kernel, release)
    await su.execute(kernel, row['update_id'])
    path = skills.agent_skill_dir(kernel.settings.data_root / 'vaults/p1', 'pm') / 'example/scripts/helper.py'
    path.write_bytes(b'VALUE = 99\n')
    assert (await su.execute(kernel, row['update_id'], rollback=True))['status'] == 'blocked'
    assert path.read_bytes() == b'VALUE = 99\n'


@pytest.mark.parametrize('damage', ['live', 'before_snapshot', 'after_snapshot', 'plan'])
async def test_blocked_rollback_can_retry_after_repair_without_losing_authorization_checks(kernel, sim, release, damage):
    before = trees(kernel, release)
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    await su.execute(kernel, row['update_id'])
    applied = trees(kernel, release)
    if damage == 'plan':
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.skill_updates SET plan=plan || '{\"damaged\":true}'::jsonb WHERE update_id=%s", (row['update_id'],))
        with pytest.raises(ValueError, match='Modified'): await su.execute(kernel, row['update_id'], rollback=True)
    else:
        path = (skills.agent_skill_dir(kernel.settings.data_root / 'vaults/p1', 'pm') / 'example/scripts/helper.py'
                if damage == 'live' else su._root(kernel.settings, 'p1', row['update_id']) / damage.split('_')[0] / 'pm/example/SKILL.md')
        saved = path.read_bytes()
        path.write_bytes(b'VALUE = 99\n') if damage == 'live' else path.unlink()
        assert (await su.execute(kernel, row['update_id'], rollback=True))['status'] == 'blocked'
    blocked = (await su.list_updates(kernel, 'p1'))[0]
    assert blocked['status'] == 'blocked'
    if damage != 'plan':
        assert blocked['receipt']['blocked_operation'] == 'rollback'
    else:
        assert blocked['receipt']['blocked_from'] == 'applied'
    # Neither recovery nor normal maintenance is allowed to silently resume this operator rollback.
    if damage != 'plan':
        held = trees(kernel, release)
        assert (await su.recover(kernel, row['update_id']))['status'] == 'blocked'
        assert (await su.execute(kernel, row['update_id']))['status'] == 'blocked'
        assert await su.maintain(kernel) == 0 and trees(kernel, release) == held
    if damage == 'plan':
        async with kernel.db.tx() as c:
            await c.execute('UPDATE core.skill_updates SET plan=%s WHERE update_id=%s', (Jsonb(row['plan']), row['update_id']))
    else:
        path.write_bytes(saved)  # operator preserved later work and restored the exact known bytes
    assert trees(kernel, release) == applied
    with pytest.raises(ValueError): await su.defer(kernel, row['update_id'], 'must not lose an applied batch')
    await policy(kernel, release, enabled=False)
    with pytest.raises(PermissionError): await su.execute(kernel, row['update_id'], rollback=True)
    await policy(kernel, release)
    result = await su.execute(kernel, row['update_id'], rollback=True)
    assert result['status'] == 'rolled_back' and trees(kernel, release) == before
    assert 'blocked_operation' not in result['receipt'] and 'blocked_from' not in result['receipt']
    assert await su.maintain(kernel) == 0


async def test_unknown_edit_during_interruption_stays_in_reconciliation(kernel, sim, release, monkeypatch):
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    original = su._write_file
    def crash(*a):
        original(*a); raise SimulatedCrash()
    with monkeypatch.context() as patch:
        patch.setattr(su, '_write_file', crash)
        with pytest.raises(SimulatedCrash): await su.execute(kernel, row['update_id'])
    path = skills.agent_skill_dir(kernel.settings.data_root / 'vaults/p1', 'pm') / 'example/scripts/helper.py'
    path.write_bytes(b'VALUE = 99\n')
    changed = trees(kernel, release)
    assert (await su.recover(kernel, row['update_id']))['status'] == 'reconciling'
    assert trees(kernel, release) == changed
    messages = [m for m in await sim.outbound('p1') if str(row['update_id']) in m['body']['text']]
    assert len(messages) == 1 and '维护者' in messages[0]['body']['text']
    await su.recover(kernel, row['update_id'])
    assert len([m for m in await sim.outbound('p1') if str(row['update_id']) in m['body']['text']]) == 1


async def test_reviewed_merge_keeps_project_content_and_upstream_identity(kernel, release):
    root = skills.agent_skill_dir(kernel.settings.data_root / 'vaults/p1', 'pm') / 'example'
    with (root / 'SKILL.md').open('ab') as out: out.write(b'\nProject requirement.\n')
    merged = kernel.settings.data_root / 'reviewed-merge'
    new = su._wheel(release['target_wheel'], release['target_sha256'], {'example'})['example']
    new['SKILL.md'] += b'\nProject requirement.\n'
    for relative, raw in new.items():
        p = merged / relative; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(raw)
    release['overrides'] = {'pm/example': {'path': str(merged), 'sha256': su._digest({'files': su._manifest(new), 'modes': su._modes(merged)})}}
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    assert (await su.execute(kernel, row['update_id']))['status'] == 'applied'
    current = skills.parse(root / 'SKILL.md')
    assert 'Project requirement.' in current.body and 'New method.' in current.body
    assert skills._content_sha(current.path) != current.template['content_sha']


@pytest.mark.parametrize('preserve_load', [False, True])
async def test_load_permission_compares_actual_project_to_candidate(kernel, release, preserve_load):
    root = skills.agent_skill_dir(kernel.settings.data_root / 'vaults/p1', 'pm') / 'example'
    path = root / 'SKILL.md'
    path.write_text(path.read_text().replace('  load:\n', '  load:\n    on_demand: true\n'))
    assert skills.parse(path).on_demand
    merged = kernel.settings.data_root / 'merged-load'
    new = su._wheel(release['target_wheel'], release['target_sha256'], {'example'})['example']
    if preserve_load:
        new['SKILL.md'] = new['SKILL.md'].replace(b'roles: [pm, supervisor]', b'roles: [pm, supervisor], on_demand: true')
    for relative, raw in new.items():
        p = merged / relative; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(raw)
    release['overrides'] = {'pm/example': {'path': str(merged), 'sha256': su._digest({'files': su._manifest(new), 'modes': su._modes(merged)})}}
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    assert row['status'] == ('prepared' if preserve_load else 'blocked')


@pytest.mark.parametrize('shape', ['directory_to_file', 'file_to_directory', 'executable'])
async def test_resource_shape_and_permissions_roundtrip(kernel, release, shape):
    old = su._wheel(release['base_wheel'], release['base_sha256'], {'example'})['example']
    new = su._wheel(release['target_wheel'], release['target_sha256'], {'example'})['example']
    if shape == 'directory_to_file':
        del new['scripts/helper.py']; new['scripts'] = b'plain resource'
    elif shape == 'file_to_directory':
        del new['scripts/helper.py']; new['scripts/helper.py/guide.txt'] = b'nested resource'
    release['target_sha256'] = wheel(Path(release['target_wheel']), {'example': new},
                                     {'scripts/helper.py': 0o755} if shape == 'executable' else None)
    await policy(kernel, release)
    row = await su.prepare(kernel, 'p1', release)
    assert (await su.execute(kernel, row['update_id']))['status'] == 'applied'
    if shape == 'executable':
        assert all((su._live(kernel.settings, row, i['key']) / 'scripts/helper.py').stat().st_mode & 0o111 for i in row['plan']['items'])
    assert (await su.execute(kernel, row['update_id'], rollback=True))['status'] == 'rolled_back'
    assert all(su._manifest(t) == su._manifest(su._stamp(old, old)) for t in trees(kernel, release).values())


async def test_directory_contract_first_adoption_in_isolated_projects(kernel, kernel_settings):
    # Read trusted release bytes; all extraction/application targets are temporary test project roots.
    base = Path('/home/v/AI/assistant-data/runtime/wheels/20261021-083542/assistant_kernel-0.45.9-py3-none-any.whl')
    if not base.is_file(): pytest.skip('Historical release fixture unavailable on this host')
    baseline = '24582b461845c79315e0d3b94ef75552c81295c5e825083cad4bbe17c06819c5'
    assert su._sha(base.read_bytes()) == baseline
    names = {'assistant-operation','assistant-evolution-review','assistant-task-continuity','assistant-goal-supervisor',
             'assistant-subagent-orchestration','assistant-project-structure','assistant-project-chartering','assistant-workflow-design'}
    old, old_modes = su._wheel(base, baseline, names, with_modes=True)
    new = {n: su._tree(skills.TEMPLATE_SKILLS / n) for n in names}
    target = kernel_settings.data_root / 'directory-contract.whl'
    target_sha = wheel(target, new)
    adopted = 0
    for project_id in ('p1', 'p2', 'p3'):
        await kernel.ensure_project(project_id)
        selected = []
        root = kernel_settings.data_root / 'vaults' / project_id
        for name in sorted(names):
            if name == 'assistant-project-chartering' and project_id != 'p1': continue
            for agent in skills.template_agents(skills.parse(skills.TEMPLATE_SKILLS / name / 'SKILL.md')):
                selected.append(f'{agent}/{name}')
                installed = install(root, selected[-1], su._stamp(old[name], old[name]))
                for relative, mode in old_modes[name].items():
                    (installed / relative).chmod(mode)
        (root / 'knowledge').mkdir(exist_ok=True)
        (root / 'knowledge/charter.md').write_text('Existing charter, no directory section')
        (root / 'data').mkdir(); (root / 'data/live.csv').write_bytes(b'preserve,42\n')
        before = trees(kernel, {'selections': selected}, project_id)
        release = {'base_wheel': str(base), 'base_sha256': baseline, 'target_wheel': str(target), 'target_sha256': target_sha,
                   'selections': selected, 'authorization_ref': 'test:directory-batch', 'rollback_allowed': True}
        await policy(kernel, release, project_id=project_id)
        row = await su.prepare(kernel, project_id, release)
        assert row['status'] == 'prepared', row['reason']
        assert (await su.execute(kernel, row['update_id']))['status'] == 'applied'
        for stage in ('requirements','design','build','review','evolution'):
            selection = skills.select(root, kernel_settings.data_root, role='pm', stage=stage)
            assert any(s.name == 'assistant-task-continuity' and s.version == '2.12.0' for s in selection.always)
        assert (root / 'knowledge/charter.md').read_text() == 'Existing charter, no directory section'
        assert (root / 'data/live.csv').read_bytes() == b'preserve,42\n' and not (root / 'artifacts').exists()
        assert (await su.execute(kernel, row['update_id'], rollback=True))['status'] == 'rolled_back'
        assert trees(kernel, release, project_id) == before
        adopted += len(selected)
    assert adopted == 26
