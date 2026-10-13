"""One finite manifest submission path shared by API, scoped tools and dispatch."""
from copy import deepcopy
import uuid
from assistant_kernel.services.contracts import contracts,canonical_json
from assistant_kernel.services.document_runtime import same_resource_ref,resource_ref
from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.services.fixed_python import FixedPython,data_schema
from assistant_kernel.services.runtime_units import RuntimeUnits
from assistant_kernel.services.v12_evidence import V12Evidence


class ProjectPython:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.fixed=FixedPython(settings);self.units=RuntimeUnits(settings,repository)

    async def authorization(self,project,manifest,submission,source_run=None):
        ownership=manifest['ownership'];purpose=manifest['purpose'];kind=submission['execution_kind']
        if purpose['kind']!=kind:raise PermissionError('Submission kind differs from the approved manifest purpose')
        if kind=='formal':
            if not submission['runtime_unit_ref']:raise PermissionError('Formal execution needs its registered fixed RuntimeUnit')
            return await self.units.finite_gate(project,submission['runtime_unit_ref'],submission['manifest_ref'],purpose['authorization_ref'])
        if purpose['kind']!=kind or not ownership['creating_invocation_ref'] or not purpose['turn_boundary_ref']:
            raise PermissionError('Auxiliary/trial execution needs its actual creating invocation and fixed Turn')
        invocation=ownership['creating_invocation_ref']
        if not invocation['uri'].startswith('db://assistant/model_invocations/'):
            raise PermissionError('Creating invocation must be an actual model ledger record')
        async with self.repository.database.connection() as c:
            actual=await (await c.execute('SELECT * FROM assistant.model_invocations WHERE invocation_id=%s',(invocation['uri'].split('/')[-1],))).fetchone()
        if not actual or actual['project_id']!=project or actual['status']!='running' or not actual['run_id']:
            raise PermissionError('Creating invocation is no longer an active same-Project invocation')
        if invocation['id']!=actual['invocation_id']:raise PermissionError('Creating invocation identity differs from its original record')
        parent=await self.repository.get_run(str(actual['run_id']))
        creator=ownership['creating_task_ref']
        if not creator or creator['id']!=str(parent['run_id']):
            raise PermissionError('Creating Task differs from the actual invocation owner')
        if source_run and str(parent['run_id'])!=str(source_run['run_id']):raise PermissionError('Scoped caller differs from the creating invocation')
        if parent['status']!='running' or parent.get('cancel_requested_at'):raise PermissionError('Creating Turn is no longer active')
        state=parent['request_json'] or {}
        if state.get('current_stage') not in {'build','operate'}:raise PermissionError('This phase cannot launch business Python')
        if not same_resource_ref(purpose['turn_boundary_ref'],state.get('confirmed_turn_package_ref')):
            raise PermissionError('Auxiliary work is outside the current confirmed Turn package')
        if purpose['authorization_ref']['id']!=state.get('prompt_confirmation_message_id'):
            raise PermissionError('Auxiliary work has no current actual Prompt confirmation')
        await V12Evidence(self.settings,self.repository).verify(project,[purpose['authorization_ref'],purpose['turn_boundary_ref']])
        return parent,None

    async def submit(self,project,submission,*,source_run=None,operation_binding=None):
        prepared=await self.prepare_submission(project,submission,source_run=source_run,operation_binding=operation_binding)
        async with self.repository.database.connection() as c:
            from assistant_kernel.services.runtime_admission import RuntimeAdmission
            await RuntimeAdmission.lock(c)
            row,duplicate=await self.submit_locked(c,project,prepared)
            await c.commit()
        return self.admission(row,prepared['manifest'],duplicate)

    async def prepare_submission(self,project,submission,*,source_run=None,operation_binding=None):
        contracts().validate('V12_Active_PythonSubmitRequest',submission)
        manifest=self.fixed.read_manifest(project,submission['manifest_ref'])
        if 'ownership' not in manifest:raise PermissionError('Finite submission requires a Python script manifest; use service lifecycle control')
        key='project-python:'+project+':'+submission['idempotency_key']
        logical=submission['logical_execution_id'] or uuid.uuid5(uuid.NAMESPACE_URL,key).hex
        fixed_submission={**submission,'logical_execution_id':logical}
        async with self.repository.database.connection() as c:
            duplicate=await (await c.execute('SELECT * FROM assistant.runs WHERE idempotency_key=%s',(key,))).fetchone()
        if duplicate:
            if duplicate['request_json'].get('python_submission')!=fixed_submission:
                raise ValueError('Python idempotency key was reused with different fixed inputs')
            if operation_binding is not None and duplicate['request_json'].get('operation_action')!=operation_binding:
                raise PermissionError('Operating action identity changed before duplicate submission')
            return {'duplicate':duplicate,'manifest':manifest}
        if submission['resume_owner'] is not None:
            raise PermissionError('Business checkpoint resume adapter is not active yet; do not replay a logical work item as a new execution')
        async with self.repository.database.connection() as c:
            prior=await (await c.execute("SELECT run_id FROM assistant.runs WHERE project_id=%s AND request_json->>'logical_execution_id'=%s AND idempotency_key<>%s LIMIT 1",
                (project,logical,key))).fetchone()
        if prior:raise PermissionError('Logical work item already exists; continue through its actual checkpoint owner')
        parent,definition=await self.authorization(project,manifest,submission,source_run)
        data_schema(submission['inputs'],manifest['io']['input_schema'])
        if len(canonical_json(submission['inputs']))>manifest['resources']['max_input_bytes']:raise ValueError('Python input exceeds the approved limit')
        if submission['execution_kind'] in {'auxiliary','trial'} and manifest['ownership']['lifecycle_relation']!='invocation_dependent':
            raise PermissionError('Auxiliary/trial Python cannot detach from its creating invocation')
        request={'protocol_revision':'1.2','python_protocol':'fixed-manifest.v1','manifest_ref':submission['manifest_ref'],
            'python_submission':fixed_submission,'runtime_unit_ref':submission['runtime_unit_ref'],
            'definition_ref':submission['runtime_unit_ref'],'logical_execution_id':logical,'input':submission['inputs'],
            'args':manifest['fixed_args'],'timeout_seconds':manifest['resources']['timeout_seconds'],'shared_write':manifest['shared_write'],
            'runtime_resources':manifest['resources'],
            'resource_keys':sorted(set(self.fixed.resource_keys(project,manifest)+(definition['shared_resource_keys'] if definition else []))),
            'creating_invocation_id':(manifest['ownership']['creating_invocation_ref'] or {}).get('id'),
            'lifecycle_relation':manifest['ownership']['lifecycle_relation'],
            'source_uri':manifest['code_bundle']['entrypoint_ref']['uri'],'source_sha256':manifest['code_bundle']['entrypoint_ref']['sha256']}
        if definition:request.update(unit_control_revision=parent['control_revision'],runtime_unit_id=definition['runtime_unit_id'],runtime_resource_policy_ref=definition['resource_policy_ref'])
        if operation_binding is not None:
            if not definition:raise PermissionError('Operating action needs its formal fixed definition')
            request['operation_action']=deepcopy(operation_binding)
        return {'manifest':manifest,'key':key,'logical':logical,'definition':definition,'parent':parent,
                'request':request,'operation_binding':operation_binding}

    async def submit_locked(self,c,project,prepared,*,schedule_binding=None):
        """Only database writes; caller owns admission lock, transaction and commit."""
        if prepared.get('duplicate'):
            return prepared['duplicate'],True
        manifest=prepared['manifest'];key=prepared['key'];logical=prepared['logical']
        definition=prepared['definition'];parent=prepared['parent'];request=deepcopy(prepared['request'])
        binding=prepared['operation_binding']
        await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
        await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('python-logical:'+project+':'+logical,))
        collision=await (await c.execute("SELECT run_id FROM assistant.runs WHERE project_id=%s AND request_json->>'logical_execution_id'=%s AND idempotency_key<>%s LIMIT 1",
            (project,logical,key))).fetchone()
        if collision:raise PermissionError('Logical work item already exists; continue through its actual checkpoint owner')
        if definition is None:
            live=await (await c.execute("SELECT status FROM assistant.model_invocations WHERE invocation_id=%s FOR SHARE",
                (manifest['ownership']['creating_invocation_ref']['id'],))).fetchone()
            if not live or live['status']!='running':raise PermissionError('Creating invocation ended before submission committed')
        else:
            live=await (await c.execute('SELECT desired_state,control_revision,effective_definition_ref FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR SHARE',
                (project,definition['runtime_unit_id']))).fetchone()
            if (not live or live['desired_state']!='running' or live['control_revision']!=parent['control_revision']
                    or not same_resource_ref(live['effective_definition_ref'],request['runtime_unit_ref'])):
                raise PermissionError('Runtime control changed before submission committed')
        if schedule_binding is not None:
            request['schedule_binding']=deepcopy(schedule_binding)
        existing=await (await c.execute('SELECT run_id FROM assistant.runs WHERE idempotency_key=%s',(key,))).fetchone()
        row=await self.repository.create_run_locked(c,project_id=project,agent_key='project:'+project+':runtime',run_type='python',
            parent_run_id=binding['task_id'] if binding is not None else str(parent['run_id']) if definition is None else None,
            request=request,idempotency_key=key,max_attempts=manifest['retry']['max_attempts'])
        return row,bool(existing)

    def admission(self,row,manifest,duplicate):
        project=row['project_id'];submission=row['request_json']['python_submission'];logical=submission['logical_execution_id']
        owner=resource_ref(kind='receipt',identity=str(row['run_id']),uri='db://assistant/runs/'+str(row['run_id']),purpose='actual fixed Python execution')
        receipt=PromptProtocol(self.settings,self.repository).write_blob(project,str(row['run_id']),'python-admission',{
            'manifest_ref':submission['manifest_ref'],'authorization_ref':manifest['purpose']['authorization_ref'],
            'input_sha256':__import__('hashlib').sha256(canonical_json(submission['inputs'])).hexdigest(),
            'run_ref':owner,'logical_execution_id':logical,'definition_ref':submission['runtime_unit_ref']})
        admission={'run_ref':owner,'status':'duplicate' if duplicate else 'queued','deduplicated':duplicate,
            'manifest_ref':submission['manifest_ref'],'authorization_ref':manifest['purpose']['authorization_ref'],
            'input_sha256':__import__('hashlib').sha256(canonical_json(submission['inputs'])).hexdigest(),'receipt_ref':receipt}
        contracts().validate('ExecutionAdmission',admission);return admission

    async def validate_dispatch(self,run):
        submission=deepcopy(run['request_json']['python_submission'])
        manifest=self.fixed.read_manifest(run['project_id'],submission['manifest_ref'])
        owner,definition=await self.authorization(run['project_id'],manifest,submission)
        if definition and run['request_json'].get('unit_control_revision')!=owner['control_revision']:
            raise PermissionError('Queued execution was overtaken by newer Unit control')
        if definition and not (run['request_json'].get('capacity_reservation') or {}).get('resolved_config_ref'):
            raise PermissionError('Formal execution needs actual accepted Kernel configuration admission')
        if (run['request_json']['args']!=manifest['fixed_args'] or run['request_json']['input']!=submission['inputs']
                or run['request_json']['runtime_resources']!=manifest['resources']):
            raise PermissionError('Queued execution changed after its fixed binding')
        expected_keys=sorted(set(self.fixed.resource_keys(run['project_id'],manifest,str(run['run_id']))+
            (definition['shared_resource_keys'] if definition else [])))
        if run['request_json']['resource_keys']!=expected_keys:
            raise PermissionError('A shared target changed after its capacity reservation')
        prepared=self.fixed.prepare(run=run,authorization_verified=True)
        prepared['definition']=definition
        return prepared
