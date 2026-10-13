"""Same-schema MCP cutover using the existing stop/snapshot/health mechanisms."""
import argparse,asyncio,json,shutil
from pathlib import Path
import httpx
from psycopg.types.json import Jsonb
from assistant_kernel import __version__,__schema_version__
from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from scripts.deploy_scheduler_release import snapshot,stop_kernel,start_kernel,command,write
from scripts.adopt_mcp_release import adopt
from scripts.verify_release import verify


async def run(settings,backup,decision,phase):
    pointer=settings.data_root/'runtime/kernel-current.txt';source=Path(__file__).resolve().parents[1]
    if phase=='stop':
        if backup.exists() and (backup/'before.json').exists():raise ValueError('Deployment backup already has a stopped snapshot')
        approval=json.loads(decision.read_text())
        if approval.get('user_instruction')!='同意按此方案实施' or approval.get('release')!=__version__:
            raise PermissionError('Actual approved MCP implementation instruction is missing')
        candidate=verify(source)
        if candidate['version']!=__version__:raise ValueError('Candidate package differs from reviewed source')
        before=await snapshot(settings);before['version']=pointer.read_text().strip();before['candidate']=__version__
        if before['version'] not in {'0.31.2','0.31.3','0.32.2'}:raise ValueError('Unexpected running version; reconcile before switching')
        backup.mkdir(parents=True,exist_ok=True);write(backup/'pre-stop.json',before)
        stop_kernel()
        try:
            stopped=await snapshot(settings)
            if before['protected_interactions']!=stopped['protected_interactions']:raise PermissionError('Interactions changed while draining')
            stopped.update(version=before['version'],candidate=__version__);write(backup/'before.json',stopped)
            return {'stopped':True,'previous_version':before['version'],'schema_migration':False}
        except BaseException:start_kernel();raise
    saved=json.loads((backup/'before.json').read_text())
    if phase=='rollback':
        stop_kernel();now=await snapshot(settings)
        if now['protected_interactions']!=saved['protected_interactions']:raise PermissionError('New interactions require reconciliation; never overwrite them')
        async with CoreRepository(settings).database.connection() as c:
            for row in saved['heads']:
                # No system/scheduler configuration is changed by this release.
                if row['key'].startswith('project-runtime-package:'):
                    await c.execute('UPDATE assistant.kernel_meta SET value=%s,updated_at=now() WHERE key=%s',(Jsonb(row['value']),row['key']))
            await c.commit()
        temporary=pointer.with_suffix('.tmp');temporary.write_text(saved['version']);temporary.replace(pointer)
        start_kernel();result={'restored_version':saved['version'],'business_ledger_retained':True};write(backup/'rollback.json',result);return result
    if not (backup/'postgres.dump').is_file():raise PermissionError('Stopped-state database backup is missing')
    try:
        current=await snapshot(settings)
        if current['protected_interactions']!=saved['protected_interactions']:raise PermissionError('Protected interactions changed before activation')
        packages=adopt(settings,decision,apply=True);write(backup/'adoption.json',packages)
        temporary=pointer.with_suffix('.tmp');temporary.write_text(__version__);temporary.replace(pointer);start_kernel()
        async with httpx.AsyncClient(timeout=5) as client:
            for _ in range(30):
                try:
                    ready=(await client.get(f'http://127.0.0.1:{settings.api_port}/health/ready')).json()
                    if ready.get('status')=='ready' and ready.get('interactive_ready') and ready.get('runtime_ready'):break
                except (httpx.HTTPError,ValueError):pass
                await asyncio.sleep(2)
            else:raise RuntimeError('Candidate did not recover readiness')
        after=await snapshot(settings)
        if after['protected_interactions']!=saved['protected_interactions']:raise PermissionError('Protected interactions changed at cutover')
        result={'version':__version__,'schema':__schema_version__,'schema_migration':False,'packages':packages,
            'protected_interactions_preserved':True,'readiness':ready};write(backup/'deployment.json',result);return result
    except BaseException:
        await run(settings,backup,decision,'rollback');raise


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--backup',type=Path,required=True);p.add_argument('--decision',type=Path,required=True)
    p.add_argument('--phase',choices=['stop','activate','rollback'],required=True);args=p.parse_args()
    result=asyncio.run(run(KernelSettings.from_environment(),args.backup,args.decision,args.phase))
    print(json.dumps({k:v for k,v in result.items() if k not in {'packages','readiness'}},default=str))
