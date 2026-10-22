"""Skill adoption: pinned wheel baselines, project batches, stop-safe recovery; off without an approved policy.

The operator-owned core.projects.settings.skill_updates policy authorizes exact release hashes and selections.
Models only read the resulting digest and propose follow-up tasks; no model tool can enable or apply this policy.
Snapshots live outside business vaults. Existing task approvals/stages are never rewritten here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import logging
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
import zipfile

from psycopg.types.json import Jsonb
import yaml

from assistant_kernel.core import machine, skills
from assistant_kernel.core.db import all_rows, event, lock_project, one
from assistant_kernel.storage import ProjectRuntimeManager

log = logging.getLogger(__name__)
WRITING = ('applying', 'rolling_back', 'reconciling')
SLUG = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())


def _manifest(tree):
    return {name: _sha(raw) for name, raw in sorted(tree.items())}


def _document(raw):
    text = raw.decode('utf-8').replace('\r\n', '\n')
    match = skills._FRONT.match(text)
    if not match:
        raise ValueError('Skill lacks YAML frontmatter')
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError('Invalid skill YAML') from exc
    if not isinstance(front, dict) or not isinstance(front.get('metadata'), dict):
        raise ValueError('Skill frontmatter and metadata must be objects')
    meta = front.get('metadata') or {}
    meta.pop('template', None)
    front['metadata'] = meta
    return front, match.group(2).strip()


def _equivalent(left, right):
    return (set(left) == set(right) and all(
        _document(left[n]) == _document(right[n]) if n == 'SKILL.md' else left[n] == right[n] for n in left))


def _validate(tree, agent, name):
    front, _ = _document(tree['SKILL.md'])
    meta = front['metadata']
    if front.get('name') != name or not front.get('description') or not meta.get('version') \
            or not isinstance(meta.get('load'), dict) or not meta['load']:
        raise ValueError('Skill identity, description, version and load are required')
    roles = meta['load'].get('roles')
    if roles is not None and (not isinstance(roles, list) or not all(isinstance(role, str) for role in roles)):
        raise ValueError('Skill load.roles must be a list of strings or null')
    # Match the existing loader: omitted/null/empty roles impose no role restriction.
    if roles and agent not in roles:
        raise ValueError('Target skill does not serve this agent')
    for path, raw in tree.items():
        if path.endswith('.py'):
            try:
                compile(raw, path, 'exec')
            except SyntaxError as exc:
                raise ValueError(f'{path}:{exc.lineno}: invalid Python candidate: {exc.msg}') from exc


def _stamp(tree, upstream):
    front, body = _document(tree['SKILL.md'])
    original, original_body = _document(upstream['SKILL.md'])
    content = yaml.safe_dump(original, allow_unicode=True, sort_keys=True) + '\0' + original_body
    front['metadata']['template'] = {
        'name': original['name'], 'version': original['metadata']['version'],
        'sha': skills.body_sha(original_body), 'content_sha': _sha(content.encode()),
    }
    return {**tree, 'SKILL.md': ('---\n' + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
                               + '---\n\n' + body + '\n').encode()}


def _tree(path):
    """Never follow a symlink, including one in the destination's parent chain."""
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Symlink skill roots are not eligible for automatic adoption')
    if not path.is_dir():
        return {}
    result = {}
    for p in sorted(path.rglob('*')):
        if p.is_symlink() or (not p.is_dir() and not p.is_file()):
            raise ValueError('Non-regular skill resources require manual review')
        if p.is_file():
            result[p.relative_to(path).as_posix()] = p.read_bytes()
    return result


def _modes(path):
    return {p.relative_to(path).as_posix(): stat.S_IMODE(p.stat().st_mode)
            for p in path.rglob('*') if p.is_file()} if path.is_dir() else {}


def _wheel(path, expected, names, *, with_modes=False):
    raw = Path(path).read_bytes()
    if _sha(raw) != expected:
        raise ValueError('Wheel hash does not match the approved release')
    result, modes = {}, {}
    # Content and permissions must come from the exact bytes whose hash was approved, not a reopened path.
    with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
        for name in names:
            prefix = f'assistant_kernel/prompts/skills/{name}/'
            tree, permissions = {}, {}
            for item in wheel.infolist():
                if not item.filename.startswith(prefix) or item.is_dir():
                    continue
                relative = item.filename[len(prefix):]
                p = PurePosixPath(relative)
                if p.is_absolute() or '..' in p.parts or '\\' in relative or stat.S_ISLNK(item.external_attr >> 16):
                    raise ValueError('Unsafe wheel resource path')
                if relative in tree:
                    raise ValueError('Duplicate wheel resource')
                mode = stat.S_IMODE(item.external_attr >> 16) or 0o644
                if mode & 0o7000:
                    raise ValueError('Special file permissions require manual review')
                tree[relative] = wheel.read(item)
                permissions[relative] = mode
            if 'SKILL.md' not in tree:
                raise ValueError(f'Missing skill in wheel: {name}')
            result[name] = tree
            modes[name] = permissions
    return (result, modes) if with_modes else result


def _vault(settings, project_id):
    return ProjectRuntimeManager(settings.data_root).describe(project_id).vault_root


def _root(settings, project_id, update_id):
    # IDs originate in core.projects / core.skill_updates, not model-provided paths.
    ProjectRuntimeManager(settings.data_root).describe(project_id)
    return settings.data_root / 'runtime' / 'skill-updates' / project_id / str(uuid.UUID(str(update_id)))


def preview(settings, project_id, release):
    """Read-only baseline/local/target comparison. A matching body alone never authorizes a folder replacement."""
    selected = release['selections']
    if not isinstance(selected, list) or not selected or not all(isinstance(k, str) for k in selected) or len(set(selected)) != len(selected):
        raise ValueError('Select each agent/skill exactly once')
    pairs = [key.split('/') for key in selected]
    if any(len(p) != 2 or not all(SLUG.fullmatch(s) for s in p) for p in pairs):
        raise ValueError('Invalid agent/skill selection')
    names = {name for _, name in pairs}
    base, base_modes = _wheel(release['base_wheel'], release['base_sha256'], names, with_modes=True)
    target, target_modes = _wheel(release['target_wheel'], release['target_sha256'], names, with_modes=True)
    items, snapshots, problems = [], {}, []
    for agent, name in pairs:
        key = f'{agent}/{name}'
        current = _tree(skills.agent_skill_dir(_vault(settings, project_id), agent) / name)
        current_modes = _modes(skills.agent_skill_dir(_vault(settings, project_id), agent) / name)
        chosen = target[name]
        chosen_modes = target_modes[name]
        override = (release.get('overrides') or {}).get(key)
        if override:
            chosen = _tree(Path(override['path']))
            chosen_modes = _modes(Path(override['path']))
            if _digest({'files': _manifest(chosen), 'modes': chosen_modes}) != override['sha256']:
                raise ValueError('Reviewed merge candidate changed')
        _validate(chosen, agent, name)
        if any(mode & 0o7000 for mode in chosen_modes.values()):
            raise ValueError('Special file permissions require manual review')
        if not current:
            problems.append(f'{key}: missing/removed copy; never reinstall implicitly')
        elif (not _equivalent(current, base[name]) or current_modes != base_modes[name]) and not override:
            problems.append(f'{key}: local customization requires a reviewed merge candidate')
        if current and _document(current['SKILL.md'])[0]['metadata'].get('load') != _document(chosen['SKILL.md'])[0]['metadata'].get('load') \
                and release.get('allow_load_changes') is not True:
            problems.append(f'{key}: load/role change requires explicit policy permission')
        after = _stamp(chosen, target[name])
        items.append({'key': key, 'before': _manifest(current), 'after': _manifest(after),
                      'before_modes': current_modes, 'after_modes': chosen_modes,
                      'base': _manifest(base[name]), 'target': _manifest(target[name]),
                      'version': _document(chosen['SKILL.md'])[0]['metadata']['version']})
        snapshots[key] = {'before': current, 'after': after}
    return {'release': release, 'items': items, 'problems': problems}, snapshots


async def list_updates(kernel, project_id, limit=30, update_id=None):
    async with kernel.db.tx() as c:
        return await all_rows(c, """SELECT update_id,status,reason,revision,updated_at,receipt,
                         plan->'release'->>'authorization_ref' AS authorization_ref,plan->'items' AS items
                         FROM core.skill_updates WHERE project_id=%s AND (%s::uuid IS NULL OR update_id=%s)
                         ORDER BY (status IN ('applying','rolling_back','reconciling')) DESC,
                                  (status IN ('prepared','blocked')) DESC,
                                  created_at DESC LIMIT %s""",
                              (project_id, update_id, update_id, min(max(limit, 1), 100)))


async def prepare(kernel, project_id, release):
    """Persist a pinned plan and recovery snapshots, without changing any project skill."""
    identity = {k: release.get(k) for k in ('target_sha256', 'selections', 'authorization_ref')}
    if isinstance(identity['selections'], list) and all(isinstance(s, str) for s in identity['selections']):
        identity['selections'] = sorted(identity['selections'])
    release_key = _digest(identity)  # reordering a batch is not a new approval and must not undo a rollback
    async with kernel.db.tx() as c:
        await lock_project(c, project_id)
        previous = await one(c, 'SELECT * FROM core.skill_updates WHERE project_id=%s AND release_key=%s', (project_id, release_key))
        if previous:
            return previous
        try:
            plan, snapshots = preview(kernel.settings, project_id, release)
        except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
            plan, snapshots = {'release': release, 'items': [], 'problems': [str(exc)]}, {}
        digest = _digest(plan)
        ident = uuid.uuid4()
        root = _root(kernel.settings, project_id, ident)
        for key, versions in snapshots.items():
            for version, files in versions.items():
                for name, raw in files.items():
                    path = root / version / key / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
        row = await one(c, """INSERT INTO core.skill_updates(update_id,project_id,release_key,plan_sha,plan,status,reason)
                             VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (ident, project_id, release_key, digest, Jsonb(plan), 'blocked' if plan['problems'] else 'prepared',
                         '; '.join(plan['problems'])))
        await event(c, project_id, 'skill_update_prepared', update_id=str(ident), plan_sha=digest)
        if plan['problems']:
            await machine.notify(c, project_id, 'supervisor',
                f"技能采用 {ident} 尚未写入：{'; '.join(plan['problems'])[:600]}。保留原副本；后续任务引用此 update_id 补基线或审查合并，条件未变不要重试。")
        return row


async def _policy(c, row):
    project = await one(c, 'SELECT settings FROM core.projects WHERE project_id=%s', (row['project_id'],))
    policy = (project['settings'] or {}).get('skill_updates') or {}
    release = row['plan']['release']
    # Policy is operator-owned existing project configuration, not an agent's completion claim.
    reference = release.get('authorization_ref')
    return bool(policy.get('enabled') is True and isinstance(reference, str) and reference.strip()
                and release in policy.get('releases', []))


@contextmanager
def _writer(root):
    """OS lock survives DB connection loss; recovery cannot take over from a still-running writer."""
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / '.writer.lock').open('a+b') as handle:
        if __import__('os').name == 'nt':
            import msvcrt
            handle.write(b'0'); handle.flush(); handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError('Skill writer is still active') from exc
            try: yield
            finally:
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: yield
            finally: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _live(settings, row, key):
    agent, name = key.split('/')
    return skills.agent_skill_dir(_vault(settings, row['project_id']), agent) / name


def _verify(settings, row, version):
    return all(_manifest(_tree(_live(settings, row, i['key']))) == i[version]
               and _modes(_live(settings, row, i['key'])) == i[version + '_modes'] for i in row['plan']['items'])


def _saved(settings, row, version, item):
    files = _tree(_root(settings, row['project_id'], row['update_id']) / version / item['key'])
    if _manifest(files) != item[version]:
        raise ValueError('Pinned snapshot missing or changed; retain reconciliation barrier')
    return files


def _write_file(path, raw, staging, mode):
    if raw is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        temp = staging / str(uuid.uuid4())
        temp.write_bytes(raw)
        temp.chmod(mode)
        temp.replace(path)


def _replace(settings, row, version):
    prepared = []
    for item in row['plan']['items']:
        destination = _live(settings, row, item['key'])
        files = _saved(settings, row, version, item)
        current = _tree(destination)
        current_modes = _modes(destination)
        # Every value must belong to this operation's old/new set, including absence. Never erase third-party edits.
        for name in set(current) | set(item['before']) | set(item['after']):
            value = _sha(current[name]) if name in current else None
            if (value, current_modes.get(name)) not in ((item['before'].get(name), item['before_modes'].get(name)),
                                                       (item['after'].get(name), item['after_modes'].get(name))):
                raise ValueError('Live skill contains a later/unknown edit; preserve it for manual reconciliation')
        prepared.append((destination, current, files, current_modes, item[version + '_modes']))
    for destination, current, files, current_modes, wanted_modes in prepared:
        staging = _root(settings, row['project_id'], row['update_id']) / 'staging'
        # Remove owned files first so directory/file swaps work; prune only their empty parents.
        for name in sorted(set(current) - set(files), key=lambda n: len(PurePosixPath(n).parts), reverse=True):
            path = destination / name
            _write_file(path, None, staging, 0)
            parent = path.parent
            while parent != destination:
                try: parent.rmdir()
                except OSError: break
                parent = parent.parent
        for name in sorted(files):
            wanted = files.get(name)
            if current.get(name) != wanted or current_modes.get(name) != wanted_modes[name]:
                _write_file(destination / name, wanted, staging, wanted_modes[name])


async def _mark(c, row, status, reason='', receipt=None):
    updated = await one(c, """UPDATE core.skill_updates SET status=%s,reason=%s,receipt=%s,
                          revision=revision+1,updated_at=now() WHERE update_id=%s RETURNING *""",
                        (status, reason[:1500], Jsonb(receipt or row['receipt']), row['update_id']))
    if row['status'] != status or row['reason'] != reason[:1500]:
        await event(c, row['project_id'], 'skill_update_' + status, update_id=str(row['update_id']), reason=reason[:500])
        if status in ('blocked', 'reconciling', 'rolled_back'):
            await machine.notify(c, row['project_id'], 'supervisor',
                f"技能采用 {row['update_id']}：{status}。{reason[:500]}。"
                + ('该项目暂停领取新执行；由维护者核对旧写入已停止、保存后来修改并恢复已知状态，再执行 recover；不要盲目重发。'
                   if status == 'reconciling' else '保留现有内容；后续任务请引用此 update_id 和恢复条件。'))
    if status == 'applied' and row['status'] != 'applied':
        release = row['plan']['release']
        await machine.insert_iteration(c, row['project_id'], None, kind='imported', source='system',
            source_ref=f"skill-update:{row['update_id']}", title='技能采用已完成（效果待复核）', reason='已批准策略下的技能更新',
            changes=[{'type': 'skill', 'target': i['key'], 'action': 'modify', 'version': i['version']} for i in row['plan']['items']],
            verification='完整目录读回校验通过；不代表模型已读取或业务效果通过',
            expected_effect=release.get('expected_effect', '采用已批准的新规则并保留项目约定'),
            measure=release.get('measure', '后续运行的技能/配置指纹与复盘证据'), facts={'update_id': str(row['update_id'])})
    return updated


async def _load(kernel, ident):
    async with kernel.db.tx() as c:
        row = await one(c, 'SELECT * FROM core.skill_updates WHERE update_id=%s', (ident,))
    if row is None:
        raise ValueError('Missing skill update plan')
    if _digest(row['plan']) != row['plan_sha']:
        async with kernel.db.tx() as c:
            await lock_project(c, row['project_id'])
            current = await one(c, 'SELECT * FROM core.skill_updates WHERE update_id=%s FOR UPDATE', (ident,))
            await _mark(c, current, 'reconciling' if current['status'] in WRITING else 'blocked',
                        'Plan integrity failed; restore the reviewed plan, never bless unknown content with a new hash',
                        receipt={**current['receipt'], 'blocked_from': current['receipt'].get('blocked_from', current['status'])})
        raise ValueError('Modified skill update plan; maintainer notified')
    return row


async def execute(kernel, ident, *, rollback=False):
    row = await _load(kernel, ident)
    root = _root(kernel.settings, row['project_id'], ident)
    with _writer(root):
        async with kernel.db.tx() as c:
            await lock_project(c, row['project_id'])
            row = await one(c, 'SELECT * FROM core.skill_updates WHERE update_id=%s FOR UPDATE', (ident,))
            retry_rollback = rollback and row['status'] == 'blocked' and (
                row['receipt'].get('blocked_operation') == 'rollback' or row['receipt'].get('blocked_from') == 'applied')
            if row['status'] != ('applied' if rollback else 'prepared') and not retry_rollback:
                return row  # retries never silently reapply a rolled-back or conflicted target
            if not await _policy(c, row) or (rollback and row['plan']['release'].get('rollback_allowed') is not True):
                raise PermissionError('No current exact-match adoption/rollback policy')
            busy = await one(c, "SELECT 1 FROM core.runs WHERE project_id=%s AND status IN ('running','reconciling') LIMIT 1", (row['project_id'],))
            active = await one(c, "SELECT * FROM core.tasks WHERE project_id=%s AND state IN ('pm','supervising','awaiting_user','validating','publishing','bootstrapping') LIMIT 1", (row['project_id'],))
            if busy or (active and (active['kind'] == 'bootstrap' or row['plan']['release'].get('compatible_with_active_tasks') is not True)):
                return row
            if active and active['stage'] == 'release':
                from assistant_kernel.core.executors.delivery import require_publication_stopped
                await require_publication_stopped(c, kernel.settings, active)
            if await one(c, 'SELECT 1 FROM core.skill_updates WHERE project_id=%s AND status=ANY(%s)', (row['project_id'], list(WRITING))):
                return row
            blocked_receipt = {**row['receipt'], 'blocked_operation': 'rollback'} if rollback else row['receipt']
            try:
                if not _verify(kernel.settings, row, 'after' if rollback else 'before'):
                    reason = ('Rollback not started; preserve later edits and restore the verified applied files, then retry rollback'
                              if rollback else 'Current files drifted; prepare a new reviewed plan')
                    return await _mark(c, row, 'blocked', reason, receipt=blocked_receipt)
                # Verify all backups/candidates before opening the committed barrier.
                for item in row['plan']['items']:
                    _saved(kernel.settings, row, 'before', item); _saved(kernel.settings, row, 'after', item)
            except (ValueError, OSError) as exc:
                return await _mark(c, row, 'blocked', str(exc), receipt=blocked_receipt)
            row = await _mark(c, row, 'rolling_back' if rollback else 'applying')
        try:
            _replace(kernel.settings, row, 'before' if rollback else 'after')
            if not _verify(kernel.settings, row, 'before' if rollback else 'after'):
                raise ValueError('Skill readback mismatch')
            async with kernel.db.tx() as c:
                await lock_project(c, row['project_id'])
                return await _mark(c, row, 'rolled_back' if rollback else 'applied', receipt={
                    'plan_sha': row['plan_sha'], 'files_verified': True, 'authorization_ref': row['plan']['release']['authorization_ref']})
        except Exception as exc:
            async with kernel.db.tx() as c:
                await lock_project(c, row['project_id'])
                await _mark(c, row, 'reconciling', str(exc))
            raise


async def recover(kernel, ident):
    row = await _load(kernel, ident)
    with _writer(_root(kernel.settings, row['project_id'], ident)):
        async with kernel.db.tx() as c:
            await lock_project(c, row['project_id'])
            row = await one(c, 'SELECT * FROM core.skill_updates WHERE update_id=%s FOR UPDATE', (ident,))
            if row['status'] not in WRITING:
                return row
            try:
                # A completed application may have lost only its DB receipt. A rollback always finishes rollback.
                if row['status'] == 'applying' and _verify(kernel.settings, row, 'after'):
                    return await _mark(c, row, 'applied', receipt={'files_verified': True, 'recovered': True})
                _replace(kernel.settings, row, 'before')
                if not _verify(kernel.settings, row, 'before'):
                    raise ValueError('Rollback readback mismatch')
                return await _mark(c, row, 'rolled_back', 'Interrupted batch restored; explicit new authorization is required to retry')
            except Exception as exc:
                return await _mark(c, row, 'reconciling', str(exc))


async def defer(kernel, ident, reason):
    row = await _load(kernel, ident)
    async with kernel.db.tx() as c:
        await lock_project(c, row['project_id'])
        row = await one(c, 'SELECT * FROM core.skill_updates WHERE update_id=%s FOR UPDATE', (ident,))
        if row['status'] not in ('prepared', 'blocked') or row['receipt'].get('blocked_operation') == 'rollback' \
                or row['receipt'].get('blocked_from') == 'applied' or not reason.strip():
            raise ValueError('Only an unwritten batch can be deferred, with a reason')
        return await _mark(c, row, 'deferred', reason)


async def maintain(kernel):
    """Existing maintenance tick: recover first; plan and apply only exact operator-approved releases."""
    async with kernel.db.tx() as c:
        projects = await all_rows(c, "SELECT project_id,settings FROM core.projects WHERE status='active'")
    changed = 0
    for project in projects:
        pid = project['project_id']
        try:
            for row in await list_updates(kernel, pid, 100):
                if row['status'] in WRITING:
                    await recover(kernel, row['update_id'])
            policy = (project['settings'] or {}).get('skill_updates') or {}
            if policy.get('enabled') is not True:
                continue
            for release in policy.get('releases', []):
                if not release.get('authorization_ref'):
                    continue
                row = await prepare(kernel, pid, release)
                if row['status'] == 'prepared':
                    applied = await execute(kernel, row['update_id'])
                    changed += int(applied['status'] == 'applied')
        except BlockingIOError:
            continue
        except Exception:
            log.exception('skill adoption requires attention for %s', pid)
    return changed


def main():
    """Administrative entry point; never exposed as a model write tool."""
    import asyncio
    from assistant_kernel.config import KernelSettings
    from assistant_kernel.core.kernel import Kernel
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preview', 'prepare', 'list', 'apply', 'rollback', 'recover', 'defer'])
    parser.add_argument('--project', required=True)
    parser.add_argument('--release', type=Path, help='Exact approved release policy JSON (preview/prepare)')
    parser.add_argument('--update-id', type=uuid.UUID)
    parser.add_argument('--reason', default='')
    args = parser.parse_args()
    async def run():
        kernel = Kernel(KernelSettings.from_environment())
        if args.action in ('preview', 'prepare'):
            release = json.loads(args.release.read_text('utf-8'))
            result = preview(kernel.settings, args.project, release)[0] if args.action == 'preview' else await prepare(kernel, args.project, release)
        elif args.action == 'list':
            result = await list_updates(kernel, args.project)
        else:
            row = await _load(kernel, args.update_id)
            if row['project_id'] != args.project:
                raise ValueError('Update belongs to another project')
            if args.action == 'defer':
                result = await defer(kernel, args.update_id, args.reason)
            else:
                result = await recover(kernel, args.update_id) if args.action == 'recover' else await execute(kernel, args.update_id, rollback=args.action == 'rollback')
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    asyncio.run(run())


if __name__ == '__main__':
    main()
