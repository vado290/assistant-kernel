"""One owned validator session inside the existing Task attempt, with P1 cleanup."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.worker.python_process import python_process_identity,inspect_python_group,cleanup_python_group


class ValidationProcessV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.prompts=PromptProtocol(settings,repository)

    async def record(self,task,receipt):
        async with self.repository.database.connection() as connection:
            identity=receipt.get('process_identity') or {}
            settled=receipt['cleanup']['status']=='verified' and receipt['outcome'] not in {'starting','running'}
            row=await (await connection.execute("""UPDATE assistant.runs SET executor_json=executor_json || %s,
                process_pid=%s,process_start_ticks=%s WHERE run_id=%s AND status='running'
                AND worker_instance_id=%s AND claim_token=%s AND attempt=%s RETURNING run_id""",
                (Jsonb({'validation_process':receipt}),None if settled else receipt.get('pid'),None if settled else identity.get('start_ticks'),task['run_id'],
                 task['worker_instance_id'],task['claim_token'],task['attempt']))).fetchone()
            await connection.commit();return bool(row)

    async def run(self,*,task,policy,bundle,cancel_event=None):
        cancel_event=cancel_event or asyncio.Event();identity=str(task['run_id']);attempt=int(task['attempt'])
        if attempt<1:raise PermissionError('Validation requires the actual claimed Task attempt')
        previous=(task.get('executor_json') or {}).get('validation_process')
        if previous and not previous.get('settled_turn_ref'):
            from assistant_kernel.services.document_runtime import same_resource_ref
            policy_sha=hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if (previous['cleanup']['status']!='verified' or previous['outcome']!='succeeded'
                    or previous.get('sealed_bundle_sha256')!=bundle['bundle_sha256'] or previous.get('policy_sha256')!=policy_sha
                    or not same_resource_ref(previous.get('original_pm_result_ref'),task['request_json'].get('pending_pm_result_ref'))
                    or not previous.get('result_ref')):
                raise PermissionError('Prior validation needs reconciliation; never relaunch it implicitly')
            result=self.prompts.read_blob(task['project_id'],previous['result_ref'])
            evidence=self.prompts.write_blob(task['project_id'],identity,'validation-with-process-evidence',{'validation':result,'process_receipt':previous})
            return {**result,'uri':evidence['uri'],'sha256':evidence['sha256'],'process_receipt':previous,'recovered_without_replay':True}
        root=self.prompts.documents.runtime.ensure(task['project_id']).vault_root
        directory=root/'runs'/identity/'validation-attempts'/str(attempt)/str(task['claim_token'])
        directory.mkdir(parents=True,exist_ok=False)
        payload={'project_id':task['project_id'],'task_id':identity,'validation_policy':policy,'sealed_bundle':bundle}
        (directory/'input.json').write_bytes(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode())
        receipt={'schema_version':'kernel-validation-process.v1','task_id':identity,'attempt':attempt,
            'started_at':datetime.now(timezone.utc).isoformat(),'ended_at':None,'outcome':'starting',
            'pid':None,'process_identity':None,'cleanup':{'status':'unknown','reason':'not launched yet'},
            'sealed_bundle_sha256':bundle['bundle_sha256'],
            'policy_sha256':hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            'result_ref':None}
        receipt['original_pm_result_ref']=task['request_json'].get('pending_pm_result_ref')
        receipt['original_turn_evidence_ref']=task['request_json'].get('pending_turn_evidence_ref')
        receipt['implementation_report_ref']=task['request_json'].get('accepted_documents',{}).get('implementation_report')
        def save():
            target=directory/'execution.json';temporary=target.with_suffix('.tmp')
            temporary.write_bytes(json.dumps(receipt,sort_keys=True).encode());temporary.replace(target)
        environment={**os.environ,'AI_HOME':str(self.settings.workspace_root),'ASSISTANT_DATA_ROOT':str(self.settings.data_root),
            'ASSISTANT_POSTGRES_HOST':self.settings.postgres_host,'ASSISTANT_POSTGRES_PORT':str(self.settings.postgres_port),
            'POSTGRES_DB':self.settings.postgres_database,'POSTGRES_USER':self.settings.postgres_user,
            'POSTGRES_PASSWORD':self.settings.postgres_password.get_secret_value(),
            'ASSISTANT_RUN_ID':identity,'ASSISTANT_ATTEMPT':str(attempt)}
        # The child changes cwd to the Vault. Bind the actually loaded Kernel
        # code root; a development PYTHONPATH="src" must not silently select
        # the older installed package in that new directory.
        paths=[str(Path(__file__).resolve().parents[2])]
        paths.extend(str(Path(item).resolve()) for item in os.environ.get('PYTHONPATH','').split(os.pathsep) if item)
        environment['PYTHONPATH']=os.pathsep.join(dict.fromkeys(paths))
        process=None;failure=None;result=None;descendants=False
        timeout=120+sum(min(max(int(profile.get('timeout_seconds') or 120),1),600) for profile in policy.get('python_unittest') or [])
        try:
            save()
            if not await self.record(task,receipt):raise asyncio.CancelledError('Validation lease lost before launch')
            if not await self.repository.run_claim_is_active(str(task['run_id']),str(task['worker_instance_id']),str(task['claim_token'])):
                raise asyncio.CancelledError('Validation ownership or stop fence changed before launch')
            if cancel_event.is_set():raise asyncio.CancelledError('Validation stopped before launch')
            with (directory/'stdout.log').open('wb') as stdout,(directory/'stderr.log').open('wb') as stderr:
                process=await asyncio.create_subprocess_exec(sys.executable,'-B','-m','assistant_kernel.worker.validation_main',
                    str(directory/'input.json'),cwd=str(root),env=environment,stdin=asyncio.subprocess.DEVNULL,stdout=stdout,stderr=stderr,start_new_session=True)
                receipt.update(pid=process.pid,process_identity=python_process_identity(process.pid,identity,attempt),outcome='running')
                save()
                if not await self.record(task,receipt):raise asyncio.CancelledError('Validation lease lost after launch')
                began=time.monotonic()
                while process.returncode is None:
                    try:await asyncio.wait_for(asyncio.shield(process.wait()),timeout=.2)
                    except asyncio.TimeoutError:pass
                    if cancel_event.is_set():raise asyncio.CancelledError('Validation cancellation requested')
                    if time.monotonic()-began>timeout:raise TimeoutError('Validation exceeded its declared command window')
            if process.returncode!=0:raise RuntimeError('Validator process failed; inspect its pinned stderr')
            descendants=inspect_python_group(receipt['process_identity'])['status']=='active'
            result=json.loads((directory/'result.json').read_bytes())
        except (Exception,asyncio.CancelledError) as exc:failure=exc
        finally:
            cleanup=await asyncio.shield(cleanup_python_group(receipt['process_identity'])) if process else {'status':'verified','reason':'no process launched'}
            if process and cleanup['status']=='verified':
                try:await asyncio.wait_for(asyncio.shield(process.wait()),5)
                except asyncio.TimeoutError:cleanup={'status':'unknown','reason':'validator exit was not acknowledged'}
            receipt.update(cleanup=cleanup,ended_at=datetime.now(timezone.utc).isoformat(),exit_code=process.returncode if process else None,
                outcome='reconciling' if cleanup['status']!='verified' else 'cancelled' if isinstance(failure,asyncio.CancelledError) else 'failed' if failure or descendants else 'succeeded',
                stop_reason=type(failure).__name__ if failure else 'UnsettledDescendants' if descendants else None)
            if result:
                receipt['result_ref']=self.prompts.write_blob(task['project_id'],identity,'actual-validation-result',result)
            save();await asyncio.shield(self.record(task,receipt))
        if cleanup['status']!='verified':raise RuntimeError('Validator process identity/cleanup requires reconciliation; never replay it')
        if failure:raise failure
        if descendants:
            result={**result,'release_gate':'fail','status':'failed','issues':result.get('issues',[])+[
                {'rule':'validation_process_cleanup','path':'validator','message':'Validator left unsettled descendants; bounded group cleanup was required'}]}
        evidence=self.prompts.write_blob(task['project_id'],identity,'validation-with-process-evidence',{
            'validation':result,'process_receipt':receipt})
        return {**result,'uri':evidence['uri'],'sha256':evidence['sha256'],'process_receipt':receipt}
