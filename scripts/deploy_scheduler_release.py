"""Controlled 0.30.2 -> 0.31.0 deployment, with explicit retained-ledger rollback."""
import argparse,asyncio,hashlib,json,os,shutil,subprocess,time
from pathlib import Path
import httpx
from psycopg.types.json import Jsonb
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.system_config import SystemConfigService
from scripts.migrate_core5_v11 import migrate
from scripts.adopt_scheduler_release import adopt
from scripts.inspect_upgrade_state import inspect as inspect_upgrade
from assistant_kernel import __version__,__schema_version__

SUPERVISOR='/etc/supervisor/conf.d/assistant.conf'
SOURCE=Path(__file__).resolve().parents[1]


def command(args,*,capture=False,accepted=(0,)):
    env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
    result=subprocess.run([str(x) for x in args],env=env,text=True,stdout=subprocess.PIPE if capture else None)
    if result.returncode not in accepted:raise subprocess.CalledProcessError(result.returncode,args)
    return result.stdout if capture else None


def supervisor(*args,capture=False):return command(['supervisorctl','-c',SUPERVISOR,*args],capture=capture,accepted=(0,3) if args[0]=='status' else (0,))
def processes():return {line.split()[0]:line.split()[1] for line in supervisor('status',capture=True).splitlines() if line.startswith('kernel-')}
def stop_kernel():
    active=[name for name,state in processes().items() if state in {'RUNNING','STARTING','BACKOFF'}]
    if 'kernel-scheduler' in active:supervisor('stop','kernel-scheduler');active.remove('kernel-scheduler')
    if active:supervisor('stop',*active)
def start_kernel():
    stopped=[name for name,state in processes().items() if state not in {'RUNNING','STARTING'}]
    if stopped:supervisor('start',*stopped)
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')


async def snapshot(settings):
    repo=CoreRepository(settings)
    async with repo.database.connection() as c:
        rows=await(await c.execute("SELECT key,value FROM assistant.kernel_meta WHERE key='system-config:v1' OR key LIKE 'project-runtime-package:%'")).fetchall()
    state=await inspect_upgrade(settings)
    if state['blockers']:raise PermissionError('Deployment has active/reconciling/publication blockers; inspect the protected receipt')
    return {'heads':rows,'protected_interactions':state['protected_interactions']}


async def restore(settings,backup):
    saved=json.loads((backup/'deployment-before.json').read_text());repo=CoreRepository(settings)
    await rollback_preflight(settings)
    # No ledger deletion or full DB restore: current external effects must survive.
    if saved['version']=='0.30.2':await migrate(settings,apply=False,rollback=True)
    current=await snapshot(settings)
    if current['protected_interactions']!=saved['protected_interactions']:raise PermissionError('Protected interactions changed; automatic rollback cannot overwrite them')
    async with repo.database.connection() as c:
        for item in saved['heads']:
            await c.execute('UPDATE assistant.kernel_meta SET value=%s,updated_at=now() WHERE key=%s',(Jsonb(item['value']),item['key']))
        await c.commit()
    config=SystemConfigService(settings);shutil.copy2(backup/'kernel-system-before.json',config.path)
    if saved['version']=='0.30.2':await migrate(settings,apply=True,rollback=True)
    pointer=settings.data_root/'runtime/kernel-current.txt';temporary=pointer.with_suffix('.tmp')
    temporary.write_text(saved['version']);temporary.replace(pointer)
    shutil.copy2(backup/'supervisord-before.conf',SUPERVISOR)
    supervisor('reread');supervisor('update');start_kernel()
    return {'restored_version':saved['version'],'ledger_retained':True,'protected_interactions_preserved':True}


async def rollback_preflight(settings):
    """Do not hand an older consumer queued service permits or new daily-operation Tasks."""
    async with CoreRepository(settings).database.connection() as c:
        installed=(await (await c.execute("SELECT to_regclass('assistant.project_schedules') AS name")).fetchone())['name']
        active=await (await c.execute("SELECT 1 FROM assistant.project_schedules WHERE state='enabled' LIMIT 1")).fetchone() if installed else None
        pending=await (await c.execute("SELECT 1 FROM assistant.schedule_ticks WHERE settlement_status='pending' LIMIT 1")).fetchone() if installed else None
        operation=await (await c.execute("SELECT 1 FROM assistant.runs WHERE request_json ? 'schedule_actions' AND status NOT IN ('succeeded','failed','cancelled','dead_letter') LIMIT 1")).fetchone()
        if active or pending or operation:raise PermissionError('Pause clocks, settle occurrences and finish/cancel daily schedule operations before rollback')
        from assistant_kernel.services.runtime_units import RuntimeUnits
        from assistant_kernel.services.contracts import ContractRegistry
        # Existing v10 Unit/Manifest contracts remain the compatibility floor. No new checksum mechanism.
        root=settings.workspace_root/'assistant-data/runtime/kernel-venvs/0.30.2/lib/python3.10/site-packages/assistant_kernel/contracts'
        old=ContractRegistry(root);documents=RuntimeUnits(settings,CoreRepository(settings)).documents
        units=await(await c.execute("SELECT project_id,effective_definition_ref FROM assistant.project_runtime_units WHERE desired_state<>'retired'")).fetchall()
        for row in units:
            definition=documents.read(row['project_id'],row['effective_definition_ref'])
            old.validate(old.document_definition(definition['schema_version']),definition['payload'])
            manifest=documents.read(row['project_id'],definition['payload']['implementation_manifest_ref'])
            old.validate(old.document_definition(manifest['schema_version']),manifest['payload'])
        return {'enabled_schedules':0,'pending_ticks':0,'pending_daily_operations':0,'legacy_readable_units':len(units)}


async def deploy(settings,backup,decision,rollback=False):
    backup.mkdir(parents=True,exist_ok=True)
    if rollback:
        stop_kernel()
        return await restore(settings,backup)
    pointer=settings.data_root/'runtime/kernel-current.txt';version=pointer.read_text().strip()
    if version not in {'0.30.2','0.31.0','0.31.1','0.31.2'} or version==__version__:raise ValueError('Unsupported source version for this scheduler release')
    from scripts.verify_release import verify
    verified=verify(SOURCE)
    if verified['version']!=__version__ or verified['schema_version']!=__schema_version__:raise ValueError('Wrong candidate package')
    candidate=settings.data_root/'runtime/kernel-venvs'/__version__/'bin/python'
    command([candidate,'-m','pip','check'])
    command([settings.data_root/'runtime/kernel-venvs'/version/'bin/python',SOURCE/'scripts/restart_preflight.py'])
    before=await snapshot(settings);before['version']=version
    write(backup/'deployment-before.json',before)
    shutil.copy2(SUPERVISOR,backup/'supervisord-before.conf')
    shutil.copy2(SystemConfigService(settings).path,backup/'kernel-system-before.json')
    stop_kernel()
    try:
        # The caller records a fresh pg_dump before invoking this controlled cutover.
        stopped=await snapshot(settings)
        if stopped['protected_interactions']!=before['protected_interactions']:raise PermissionError('Protected interactions changed during drain')
        migration=await migrate(settings,apply=True)
        adoption=await adopt(settings,decision,apply=True,enable=False)
        write(backup/'adoption.json',adoption)
        temporary=pointer.with_suffix('.tmp');temporary.write_text(__version__);temporary.replace(pointer)
        shutil.copy2(SOURCE/'deploy/supervisord.conf',SUPERVISOR)
        supervisor('reread');supervisor('update');start_kernel()
        with httpx.Client(timeout=5) as client:
            ready=None
            for _ in range(30):
                try:
                    response=client.get('http://127.0.0.1:'+str(settings.api_port)+'/health/ready');ready=response.json()
                    if ready.get('status')=='ready' and ready.get('interactive_ready') and ready.get('runtime_ready'):break
                except (httpx.HTTPError,ValueError):pass
                await asyncio.sleep(2)
            else:raise RuntimeError('New release readiness did not recover')
        after=await snapshot(settings)
        if after['protected_interactions']!=before['protected_interactions']:raise PermissionError('Protected interactions changed during deployment')
        return {'version':__version__,'schema':__schema_version__,'matched_files':verified['matched_files'],'migration':migration,
            'scheduler_enabled':False,'packages':len(adoption['packages']),'protected_interactions_preserved':True,'readiness':ready}
    except Exception:
        stop_kernel()
        write(backup/'automatic-rollback.json',await restore(settings,backup))
        raise


async def main():
    p=argparse.ArgumentParser();p.add_argument('--backup',type=Path,required=True);p.add_argument('--decision',type=Path,required=True);p.add_argument('--rollback',action='store_true');a=p.parse_args()
    result=await deploy(KernelSettings.from_environment(),a.backup,a.decision,a.rollback)
    write(a.backup/('rollback.json' if a.rollback else 'deployment.json'),result)
    print(json.dumps({k:v for k,v in result.items() if k!='readiness'},default=str))

if __name__=='__main__':asyncio.run(main())
