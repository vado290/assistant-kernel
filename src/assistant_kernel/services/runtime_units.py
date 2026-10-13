"""Project-owned definition pointers; code, Role and graph bodies stay in documents."""
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import hashlib,uuid
from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.document_runtime import TaskDocumentService,same_resource_ref
from assistant_kernel.services.published_agents import PublishedAgentService
from assistant_kernel.services.v12_evidence import V12Evidence,references


class RuntimeUnits:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)

    @asynccontextmanager
    async def transaction(self,connection=None):
        from assistant_kernel.services.runtime_admission import RuntimeAdmission
        if connection is not None:
            await RuntimeAdmission.lock(connection)
            yield connection
        else:
            async with self.repository.database.connection() as current:
                await RuntimeAdmission.lock(current)
                yield current
                await current.commit()

    @staticmethod
    def registration(row):
        value={'project_id':row['project_id'],'runtime_unit_id':row['unit_id'],'definition_ref':row['effective_definition_ref'],
            'desired_state':row['desired_state'],'control_revision':row['control_revision'],'enable_authorization_ref':row['enable_authorization_ref'],
            'resource_policy_ref':row['resource_policy_ref'],'recovery_attempts':(row['control_json'] or {}).get('recovery_attempts',0),
            'available_at':row['available_at'].isoformat() if row['available_at'] else None,
            'last_control_receipt_ref':row['last_control_receipt_ref'],'resolved_config_ref':None}
        contracts().validate('V12_RuntimeUnitRegistration',value);return value

    def read_definition(self,project,ref):
        document=self.documents.read(project,ref)
        if document['schema_version']!='runtime-unit-definition.v1' or document.get('scope')!='project':
            raise PermissionError('Runtime definition needs its Project-scoped fixed document')
        definition=document['payload'];contracts().validate('V12_RuntimeProgramDefinition',definition)
        if definition['project_id']!=project:raise PermissionError('Runtime definition belongs to another Project')
        expected='python-service-manifest.v1' if definition['lifecycle_kind']=='service' else 'python-script-manifest.v1'
        manifest=self.documents.read(project,definition['implementation_manifest_ref'])
        if manifest['schema_version']!=expected:raise PermissionError('Runtime mode and manifest type differ')
        return deepcopy(definition)

    async def control(self,project,request,*,authenticated_operator=False,connection=None):
        if self.read_definition(project,request['runtime_unit_ref'])['lifecycle_kind']=='service':
            from assistant_kernel.services.service_runtime import ServiceRuntime
            return await ServiceRuntime(self.settings,self.repository).control(project,request,authenticated_operator=authenticated_operator,connection=connection)
        return await self.control_finite(project,request,authenticated_operator=authenticated_operator,connection=connection)

    async def view(self,project,definition_ref):
        definition=self.read_definition(project,definition_ref)
        async with self.repository.database.connection() as c:
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s',(project,definition['runtime_unit_id']))).fetchone()
            if not row or not same_resource_ref(row['effective_definition_ref'],definition_ref):raise PermissionError('Runtime Unit reference is no longer the registered head')
            runs=await (await c.execute("SELECT run_id FROM assistant.runs WHERE project_id=%s AND request_json->>'runtime_unit_id'=%s ORDER BY created_at DESC LIMIT 20",(project,row['unit_id']))).fetchall()
            instance=await (await c.execute('SELECT * FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s ORDER BY generation DESC LIMIT 1',(project,row['unit_id']))).fetchone() if definition['lifecycle_kind']=='service' else None
        from assistant_kernel.services.service_runtime import ServiceRuntime
        result={'definition_ref':row['effective_definition_ref'],'registration':self.registration(row),
            'service_instance':ServiceRuntime(self.settings,self.repository).instance_payload(instance) if instance else None,
            'finite_run_refs':[resource_ref(kind='receipt',identity=str(x['run_id']),uri='db://assistant/runs/'+str(x['run_id']),purpose='actual finite runtime segment') for x in runs],
            'evidence_refs':[row['last_control_receipt_ref']] if row['last_control_receipt_ref'] else [],
            'unverified':['No actual service readiness yet'] if definition['lifecycle_kind']=='service' and (not instance or instance['observed_state']!='ready') else []}
        contracts().validate('V12_RuntimeUnitView',result);return result

    async def list(self,project,limit=20,cursor=None):
        contracts().validate('Args_list_runtime_units',{'limit':limit,'cursor':cursor})
        async with self.repository.database.connection() as c:
            rows=await (await c.execute('SELECT unit_id,effective_definition_ref FROM assistant.project_runtime_units WHERE project_id=%s AND (%s::text IS NULL OR unit_id>%s) ORDER BY unit_id LIMIT %s',
                (project,cursor,cursor,limit+1))).fetchall()
        return {'project_id':project,'items':[await self.view(project,x['effective_definition_ref']) for x in rows[:limit]],
            'next_cursor':rows[limit-1]['unit_id'] if len(rows)>limit else None}

    async def register(self,project,definition_ref,*,review_ref,publication_ref,expected_control_revision=0,connection=None):
        # Reading the accepted policy proves an actual registered Project; no
        # invented projects table/foreign key and no filesystem-only adoption.
        async with self.repository.database.connection() as c:
            known=await (await c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(PublishedAgentService.key(project),))).fetchone()
        if not known:raise PermissionError('Runtime registration needs an actually adopted Project package')
        PublishedAgentService(self.settings).project_policy(project)
        definition=await self.reviewed_definition(project,definition_ref,review_ref,publication_ref)
        async with self.transaction(connection) as c:
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('runtime-unit:'+project+':'+definition['runtime_unit_id'],))
            prior=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',
                (project,definition['runtime_unit_id']))).fetchone()
            if prior:
                if prior['control_revision']!=expected_control_revision:raise PermissionError('Runtime registration control revision is stale')
                if not same_resource_ref(prior['effective_definition_ref'],definition_ref):
                    raise PermissionError('A registered definition changes only through explicit update/drain control')
                return prior
            if expected_control_revision!=0:raise PermissionError('New RuntimeUnit registration starts at revision zero')
            row=await (await c.execute("""INSERT INTO assistant.project_runtime_units(project_id,unit_id,effective_definition_ref,
                desired_state,control_revision,resource_policy_ref) VALUES (%s,%s,%s,'stopped',0,%s) RETURNING *""",
                (project,definition['runtime_unit_id'],Jsonb(definition_ref),Jsonb(definition['resource_policy_ref']) if definition['resource_policy_ref'] else None))).fetchone()
            return row

    async def reviewed_definition(self,project,definition_ref,review_ref,publication_ref):
        if not review_ref or not publication_ref:raise PermissionError('The target definition has no accepted Review/publication')
        definition=self.read_definition(project,definition_ref)
        review=self.documents.read(project,review_ref);publication=self.documents.read(project,publication_ref)
        if review['schema_version']!='review-report.v1' or review['payload']['conclusion']!='pass':raise PermissionError('Registration needs its actual passing Review')
        if publication['schema_version']!='publication-receipt.v1':raise PermissionError('Registration needs its actual publication receipt')
        if publication['payload']['project_id']!=project:raise PermissionError('Publication belongs to another Project')
        if not same_resource_ref(publication['payload']['review_ref'],review_ref):raise PermissionError('Publication belongs to a different Review')
        creator=publication['payload']['creating_task_ref'];task=await self.repository.get_run(creator['id'])
        if not task or task['project_id']!=project:raise PermissionError('Registration source Task is missing')
        accepted=(task['request_json'] or {}).get('accepted_documents') or {}
        if not same_resource_ref(accepted.get('review_report'),review_ref) or not same_resource_ref(accepted.get('publication'),publication_ref):
            raise PermissionError('Model/filesystem reports are not the Task accepted Review/publication')
        if not same_resource_ref(accepted.get('solution_design'),definition['design_ref']):raise PermissionError('Definition is outside the accepted design')
        implementation=self.documents.read(project,accepted['implementation_report'])['payload']
        if any(not any(same_resource_ref(accepted[k],ref) for ref in review['payload']['fixed_input_refs'])
                for k in ('task_contract','solution_design','implementation_report')):
            raise PermissionError('Review did not examine the current accepted implementation baseline')
        if not any(same_resource_ref(definition_ref,x) for x in references(implementation)):
            raise PermissionError('The reviewed implementation omitted this exact RuntimeUnit definition')
        await V12Evidence(self.settings,self.repository).verify(project,[review_ref,publication_ref,definition_ref])
        return definition

    async def finite_gate(self,project,definition_ref,manifest_ref,authorization_ref):
        definition=self.read_definition(project,definition_ref)
        if definition['lifecycle_kind']!='finite':raise PermissionError('Finite submit cannot start a service')
        if not same_resource_ref(definition['implementation_manifest_ref'],manifest_ref):raise PermissionError('Submit manifest differs from the registered definition')
        async with self.repository.database.connection() as c:
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s',
                (project,definition['runtime_unit_id']))).fetchone()
        if not row or not same_resource_ref(row['effective_definition_ref'],definition_ref):raise PermissionError('Runtime definition has not been registered')
        if row['desired_state']!='running' or not row['enable_authorization_ref']:
            raise PermissionError('Runtime execution has not been actually authorized/enabled')
        if not same_resource_ref(row['enable_authorization_ref'],authorization_ref):raise PermissionError('Runtime authorization differs from its enabled scope')
        await V12Evidence(self.settings,self.repository).verify(project,authorization_ref)
        return row,definition

    async def control_finite(self,project,request,*,authenticated_operator=False,connection=None):
        """Explicit Kernel operator control; a document or model assertion cannot enable a Unit."""
        contracts().validate('V12_RuntimeControlRequest',request)
        if authenticated_operator is not True:raise PermissionError('Finite control needs an authenticated Kernel operator command')
        if request['expires_at'] and datetime.fromisoformat(request['expires_at'].replace('Z','+00:00'))<=datetime.now(timezone.utc):
            raise PermissionError('Runtime control command expired before admission')
        definition=self.read_definition(project,request['runtime_unit_ref'])
        if definition['lifecycle_kind']!='finite' or request['cause']!='manual' or request['action']=='restart':
            raise PermissionError('This controller handles finite manual admission; service/maintenance needs its actual adapter')
        sources=request['authorization_source_refs']
        if not sources:raise PermissionError('Actual operator authorization source references are required')
        await V12Evidence(self.settings,self.repository).verify(project,sources)
        action=request['action'];scope=definition['approved_execution_scope_ref']
        if action in {'start','resume'} and (not scope or not any(same_resource_ref(scope,x) for x in sources)):
            raise PermissionError('The specific approved execution scope is missing from this enable command')
        async with self.transaction(connection) as c:
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('runtime-unit:'+project+':'+definition['runtime_unit_id'],))
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',
                (project,definition['runtime_unit_id']))).fetchone()
            if not row or not same_resource_ref(row['effective_definition_ref'],request['runtime_unit_ref']):raise PermissionError('Unit head changed before control')
            state=deepcopy(row['control_json']);commands=state.setdefault('commands',{})
            digest=hashlib.sha256(canonical_json(request)).hexdigest();old=commands.get(request['idempotency_key'])
            if old:
                if old['request_sha256']!=digest:raise ValueError('Control idempotency key was reused for a different command')
                return old['receipt_ref']
            if row['control_revision']!=request['expected_control_revision']:raise PermissionError('Unit control revision is stale')
            if row['desired_state']=='retired' and action!='retire':raise PermissionError('Retired Unit cannot silently resume')
            target=request['runtime_unit_ref']
            if action=='update':
                updated=self.read_definition(project,request['target_definition_ref'])
                if updated['runtime_unit_id']!=definition['runtime_unit_id'] or updated['lifecycle_kind']!='finite':raise PermissionError('Update changes the Unit identity/kind')
                design=self.documents.read(project,updated['design_ref'])
                owner=await self.repository.get_run(design.get('task_id')) if design.get('task_id') else None
                accepted=(owner['request_json'].get('accepted_documents') or {}) if owner else {}
                updated=await self.reviewed_definition(project,request['target_definition_ref'],accepted.get('review_report'),accepted.get('publication'))
                active=await (await c.execute("""SELECT 1 FROM assistant.runs WHERE project_id=%s AND request_json->>'runtime_unit_id'=%s
                    AND status NOT IN ('succeeded','failed','cancelled','dead_letter') LIMIT 1""",(project,definition['runtime_unit_id']))).fetchone()
                if active:raise PermissionError('Old queued work, waiting input or execution must settle before definition update')
                target=request['target_definition_ref'];definition=updated
            desired={'start':'running','resume':'running','stop':'stopped','pause':'paused','retire':'retired','update':'stopped'}[action]
            revision=row['control_revision']+1
            if action in {'stop','pause','retire'}:
                from assistant_kernel.services.schedule_support import pause_unit_schedules
                await pause_unit_schedules(c,project,definition['runtime_unit_id'],'manual_unit_'+action)
                await c.execute("""UPDATE assistant.runs SET status='cancelled',cancel_requested_at=now(),ended_at=now(),updated_at=now()
                    WHERE project_id=%s AND request_json->>'runtime_unit_id'=%s AND status='queued'""",(project,definition['runtime_unit_id']))
                if request['stop_policy']=='cancel':
                    await c.execute("""UPDATE assistant.runs SET cancel_requested_at=now(),updated_at=now()
                        WHERE project_id=%s AND request_json->>'runtime_unit_id'=%s AND status='running'""",(project,definition['runtime_unit_id']))
            payload={'control_id':str(uuid.uuid4()),'runtime_unit_ref':target,'control_revision':revision,'desired_state':desired,'status':'settled',
                'adapter_step_refs':[],'instance_ref':None,'observed_state':'stopped','cleanup_status':'verified','cleanup_ref':None,
                'evidence_refs':sources,'error':None,'next_action':'Admission gate updated; finite executions keep their own process receipts',
                'maintenance_receipt_ref':None,'resolved_config_ref':None}
            # There is no service instance here. Do not describe a still-running
            # finite process as stopped or cleaned when only its gate changed.
            running=await (await c.execute("""SELECT 1 FROM assistant.runs WHERE project_id=%s AND request_json->>'runtime_unit_id'=%s
                AND status IN ('running','reconciling') LIMIT 1""",(project,definition['runtime_unit_id']))).fetchone()
            if running:payload.update(observed_state='unknown',cleanup_status='pending')
            receipt=self.documents.write_scoped_revision(project_id=project,scope='project',object_id='unit-'+hashlib.sha256(definition['runtime_unit_id'].encode()).hexdigest()[:24],
                document_kind='runtime-control-receipt',document_schema='runtime-control-receipt.v1',payload=payload,status='settled',actor_type='kernel',actor_id='kernel-operator',
                reason='Authenticated operator changed this exact finite admission gate; no model granted authority',source_refs=sources)
            commands[request['idempotency_key']]={'request_sha256':digest,'receipt_ref':receipt}
            authorization=scope if desired=='running' else None if action=='update' else row['enable_authorization_ref']
            await c.execute('''UPDATE assistant.project_runtime_units SET effective_definition_ref=%s,desired_state=%s,control_revision=%s,
                enable_authorization_ref=%s,resource_policy_ref=%s,control_json=%s,last_control_receipt_ref=%s,updated_at=now() WHERE project_id=%s AND unit_id=%s''',
                (Jsonb(target),desired,revision,Jsonb(authorization) if authorization else None,
                 Jsonb(definition['resource_policy_ref']) if definition['resource_policy_ref'] else None,
                 Jsonb(state),Jsonb(receipt),project,definition['runtime_unit_id']))
            return receipt
