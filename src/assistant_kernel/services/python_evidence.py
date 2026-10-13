"""Bridge actual attempt observations to the existing v1.2 execution contracts."""
import hashlib
from pathlib import Path

from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.storage import ProjectRuntimeManager


class PythonEvidence:
    def __init__(self, settings, run):
        self.run=run; self.project=run['project_id']; self.documents=TaskDocumentService(settings)
        self.vault=ProjectRuntimeManager(settings.data_root).describe(self.project).vault_root
        self.root=self.vault/'runs'/str(run['run_id'])/'attempts'/str(run['attempt'])
        self.run_ref=resource_ref(kind='receipt',identity=str(run['run_id']),
            uri='db://assistant/runs/'+str(run['run_id']),purpose='actual Python Run')

    def file_ref(self, path, purpose):
        return resource_ref(kind='source',identity=path.name,
            uri='vault://'+self.project+'/'+path.relative_to(self.vault).as_posix(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),purpose=purpose)

    def blob(self, name, payload, definition=None):
        if definition:contracts().validate(definition,payload)
        raw=canonical_json(payload); path=self.root/(name+'-'+hashlib.sha256(raw).hexdigest()+'.json')
        if path.exists() and path.read_bytes()!=raw:raise ValueError('Immutable execution evidence collision')
        if not path.exists():path.write_bytes(raw)
        return self.file_ref(path,'actual '+name)

    def document(self, kind, payload, sources):
        return self.documents.write_scoped_revision(project_id=self.project,scope='project',
            object_id='run-'+str(self.run['run_id'])+'-a'+str(self.run['attempt']),document_kind=kind,
            document_schema=kind+'.v1',payload=payload,status='settled',actor_type='kernel',actor_id='python-worker',
            reason='Observed fixed Python execution; business success is separately verified',source_refs=sources)

    def binding(self, prepared):
        request=self.run['request_json']; manifest=prepared['manifest']; ownership=manifest['ownership']
        invocation=ownership['creating_invocation_ref']; unit=request.get('runtime_unit_ref')
        payload={'run_id':str(self.run['run_id']),'project_id':self.project,'manifest_ref':request['manifest_ref'],
            'creating_task_ref':ownership['creating_task_ref'],'creating_invocation_ref':invocation,
            'owner_kind':'project_operation' if unit else 'invocation','owner_ref':unit or invocation,
            'handoff_ref':None,'authorization_ref':manifest['purpose']['authorization_ref'],
            'args_sha256':hashlib.sha256(canonical_json(manifest['fixed_args'])).hexdigest(),
            'input_sha256':hashlib.sha256(canonical_json(request['input'])).hexdigest(),
            'code_bundle_ref':prepared['code_bundle_ref'],'environment_ref':prepared['environment_ref'],
            'runtime_unit_ref':unit,'definition_ref':request.get('definition_ref'),
            'logical_execution_id':request['logical_execution_id'],'attempt':self.run['attempt'],
            'assembly_receipt_ref':prepared.get('assembly_receipt_ref'),'resolved_config_ref':manifest['resolved_config_ref'],
            'resume_checkpoint_ref':request.get('resume_checkpoint_ref'),'accumulated_budget_ref':prepared.get('accumulated_budget_ref') or request.get('accumulated_budget_ref')}
        return self.document('python-run-binding',payload,[request['manifest_ref']])

    async def record_documents_locked(self, connection, repository, receipt):
        from assistant_kernel.repositories import change_ledger
        from assistant_kernel.services.project_changes import ProjectChangeService
        if not await change_ledger.enabled(connection):return
        documents=[receipt[x] for x in ('binding_ref','settlement_ref') if receipt.get(x)]
        changes=[{'target_uri':'workspace://project/'+ref['uri'].removeprefix('vault://'+self.project+'/'),
            'document_ref':ref,'after_sha256':ref['sha256'],'operation':'create',
            'accepted_head_guard':{'kind':'python_attempt','owner_id':str(self.run['run_id'])},
            'purpose':'Actual fixed Python '+ref['document_schema']} for ref in documents]
        if changes:
            await ProjectChangeService(repository.settings,repository.database).capture_batch(self.project,
                batch_id=f"python-attempt:{self.run['run_id']}:{self.run['attempt']}",roots=[],changes=changes,
                run_id=str(self.run['run_id']),actor='kernel',reason='Actual Run accepted its attempt binding and settlement',
                reason_source_refs=[self.run_ref],scope_kind='artifact',connection=connection)

    def settle(self, receipt, prepared):
        observation=self.blob('process-observation',receipt)
        identity=receipt['process_identity'] or {}; code=receipt['exit_code']; reason=receipt['stop_reason']
        launch='launch_failed' if not identity else 'identity_unknown' if identity.get('start_ticks') is None else 'started'
        process_status=('unknown' if launch=='identity_unknown' else 'failed' if launch=='launch_failed' else
            'timed_out' if reason=='TimeoutError' else 'cancelled' if reason=='CancelledError' else
            'exited' if code==0 else 'failed')
        process=self.blob('process-receipt',{'run_ref':self.run_ref,'attempt':self.run['attempt'],
            'pid':identity.get('pid'),'start_ticks':identity.get('start_ticks'),'process_group_id':identity.get('pgid'),
            'exit_code':code,'signal':-code if code is not None and code<0 else None,'launch_state':launch,
            'status':process_status,'evidence_refs':[observation]},'V12_ProcessReceipt')
        cleanup=receipt['cleanup']; cleaned=cleanup['status']=='verified'
        cleanup_ref=self.blob('cleanup-receipt',{'run_ref':self.run_ref,'attempt':self.run['attempt'],
            'status':'complete' if cleaned else 'unknown','remaining_member_count':0 if cleaned else None,
            'evidence_refs':[observation]},'CleanupReceipt')
        artifacts=[self.file_ref(p,'actual attempt output') for p in
            (self.root/'stdout.log',self.root/'stderr.log',self.root/'result.json') if p.is_file()]
        if prepared:
            artifacts.extend(self.file_ref(path,'actual observed business assertion snapshot') for path in prepared.get('business_check_evidence',[]))
            output=prepared['business_output_root']
            for relative in set(['result.json',*prepared['manifest']['io']['required_outputs']]):
                from assistant_kernel.services.fixed_python import safe_relative
                path=output/str(safe_relative(relative))
                if not path.resolve().is_relative_to(output.resolve()) or not path.is_file():continue
                if path.stat().st_size>prepared['resources']['max_output_bytes']:continue
                # A separate output root still gets a pinned snapshot in this
                # attempt, so all accepted results remain readable on recovery.
                if path.resolve().is_relative_to(self.root.resolve()):
                    ref=self.file_ref(path,'actual required output')
                else:
                    raw=path.read_bytes();target=self.root/'output-evidence'/relative
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
                    ref=self.file_ref(target,'snapshot of '+str(path))
                if ref not in artifacts:artifacts.append(ref)
        payload={'run_ref':self.run_ref,'attempt':self.run['attempt'],
            'process_status':'succeeded' if process_status=='exited' else process_status,
            'contract_status':receipt.get('contract_status','unverified'),
            'business_status':receipt.get('business_status','unverified'),
            'effect_status':receipt.get('effect_status','unknown'),'artifact_refs':artifacts,
            'process_receipt_ref':process,'cleanup_receipt_ref':cleanup_ref,'unverified':[]}
        payload['unverified']=[key for key in ('process_status','contract_status','business_status','effect_status')
            if payload[key] in {'unknown','unverified'}]+([] if cleaned else ['cleanup'])
        settlement=self.document('python-execution-settlement',payload,[observation,process,cleanup_ref])
        return settlement,artifacts
