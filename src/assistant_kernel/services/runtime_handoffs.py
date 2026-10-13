"""Consume the existing PM handoff in its settlement transaction; no new task engine."""
import hashlib
from assistant_kernel.services.contracts import contracts,canonical_json
from assistant_kernel.services.document_runtime import same_resource_ref,resource_ref
from assistant_kernel.services.runtime_units import RuntimeUnits
from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.repositories.core import approval_decision


class RuntimeHandoffs:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.units=RuntimeUnits(settings,repository)
        self.documents=self.units.documents;self.prompts=PromptProtocol(settings,repository)

    async def authorization_locked(self,c,task,definition):
        state=task['request_json'];accepted=state.get('accepted_documents') or {}
        design_ref=accepted.get('solution_design');approval=state.get('design_approval') or {}
        if not same_resource_ref(definition['design_ref'],design_ref):raise PermissionError('Runtime handoff uses another accepted design')
        design=self.documents.read(task['project_id'],design_ref)
        if design.get('task_id')!=str(task['run_id']):raise PermissionError('Runtime handoff design belongs to another Task')
        if approval.get('approved_document_sha256')!=design_ref['sha256']:raise PermissionError('Initial enable needs approval of this exact design')
        scope=definition['approved_execution_scope_ref'];message_id=approval.get('message_id') or approval.get('approval_message_id')
        if not scope or scope['kind']!='message' or scope['id']!=message_id or scope['uri']!='db://assistant/messages/'+str(message_id):
            raise PermissionError('Initial enable must bind its concrete approved design message, not a file agreement')
        source=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(message_id,))).fetchone()
        if not source or source['project_id']!=task['project_id'] or source['direction']!='inbound' or not source['user_id']:
            raise PermissionError('Initial enable has no actual same-Project user approval')
        body=source['content_json'] or {}
        explicit=any(x.get('decision')=='approve' and same_resource_ref(x.get('document_ref'),design_ref) for x in body.get('document_approvals',[]))
        if body.get('context_revoked') or body.get('source_revoked') or not explicit and approval_decision(body)!='approve':
            raise PermissionError('Runtime design approval is absent or revoked')
        planned=[x for x in design['payload']['runtime_designs'] if x['runtime_unit_id']==definition['runtime_unit_id']]
        if len(planned)!=1 or planned[0]['lifecycle_kind']!=definition['lifecycle_kind'] or not design['payload']['activation_plan']['applicable']:
            raise PermissionError('The approved design does not authorize this Unit activation')
        return scope

    async def apply_locked(self,c,*,task,handoffs,turn_ref):
        if not handoffs:return None
        from assistant_kernel.services.runtime_admission import RuntimeAdmission
        await RuntimeAdmission.lock(c)
        if task['request_json'].get('current_stage')!='operate':raise PermissionError('Runtime handoff is consumed in Operate after Review/publication')
        accepted=task['request_json'].get('accepted_documents') or {};items=[]
        for handoff in handoffs:
            contracts().validate('V12_RuntimeHandoff',handoff)
            definition=self.units.read_definition(task['project_id'],handoff['definition_ref'])
            if not same_resource_ref(definition['implementation_manifest_ref'],handoff['manifest_ref']):raise PermissionError('Handoff manifest differs from its fixed definition')
            action=handoff['requested_action']
            if action=='none':continue
            if not same_resource_ref(definition['design_ref'],accepted.get('solution_design')):
                raise PermissionError('Handoff belongs to another creating design')
            if handoff['unverified'] or handoff['missing_approval_subject_refs']:raise PermissionError('Runtime handoff still has unverified or missing authorization items')
            if not any(same_resource_ref(x,accepted.get('review_report')) for x in handoff['review_refs']):raise PermissionError('Handoff omits the actual accepted Review')
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('runtime-unit:'+task['project_id']+':'+definition['runtime_unit_id'],))
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',
                (task['project_id'],definition['runtime_unit_id']))).fetchone()
            scope=await self.authorization_locked(c,task,definition) if action in {'enable','update'} else None
            if scope:
                manifest=self.documents.read(task['project_id'],handoff['manifest_ref'])['payload']
                if definition['lifecycle_kind']=='finite' and not same_resource_ref(manifest['purpose']['authorization_ref'],scope):raise PermissionError('Manifest authority differs from the concrete activation scope')
            control=None
            if action=='update':
                if not row:raise PermissionError('Update requires an existing registered Unit')
                await self.units.reviewed_definition(task['project_id'],handoff['definition_ref'],accepted.get('review_report'),accepted.get('publication'))
                request={'runtime_unit_ref':row['effective_definition_ref'],'action':'update','expected_control_revision':row['control_revision'],
                    'idempotency_key':'handoff-update:'+turn_ref['sha256']+':'+definition['runtime_unit_id'],'authorization_source_refs':[scope],
                    'target_definition_ref':handoff['definition_ref'],'stop_policy':'drain','cause':'manual','tick_ref':None,'expected_instance_ref':None,'expires_at':None}
                control=await self.units.control(task['project_id'],request,authenticated_operator=True,connection=c)
                row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s',
                    (task['project_id'],definition['runtime_unit_id']))).fetchone()
            else:
                row=await self.units.register(task['project_id'],handoff['definition_ref'],review_ref=accepted.get('review_report'),
                    publication_ref=accepted.get('publication'),expected_control_revision=row['control_revision'] if row else 0,connection=c)
            if action=='enable' and row['desired_state']!='running':
                if row['control_revision']!=0:raise PermissionError('Initial handoff cannot undo a later stop/pause; use explicit Kernel control')
                request={'runtime_unit_ref':handoff['definition_ref'],'action':'start','expected_control_revision':0,
                    'idempotency_key':'handoff-enable:'+handoff['definition_ref']['sha256'],'authorization_source_refs':[scope],
                    'target_definition_ref':None,'stop_policy':'drain','cause':'manual','tick_ref':None,'expected_instance_ref':None,'expires_at':None}
                control=await self.units.control(task['project_id'],request,authenticated_operator=True,connection=c)
            row=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s',
                (task['project_id'],definition['runtime_unit_id']))).fetchone()
            items.append({'definition_ref':handoff['definition_ref'],'manifest_ref':handoff['manifest_ref'],
                'registration':self.units.registration(row),'control_ref':control or row['last_control_receipt_ref'],
                'ownership':'project','creating_task_ref':resource_ref(kind='receipt',identity=str(task['run_id']),uri='db://assistant/runs/'+str(task['run_id']),purpose='actual creating Task')})
        if not items:return None
        return self.prompts.write_blob(task['project_id'],str(task['run_id']),'runtime-ownership',{
            'schema_version':'runtime-ownership-receipt.v1','project_id':task['project_id'],'items':items,'turn_ref':turn_ref,
            'reason':'Kernel committed reviewed Project registrations/control; model handoff alone is not activation'})

    async def accept_activation_locked(self,c,*,task,handoffs):
        """Model reports select evidence; actual finite Runs establish the execution facts."""
        from assistant_kernel.services.v12_evidence import references
        selected=[]
        for handoff in handoffs:
            for ref in handoff['activation_refs']:
                if ref.get('document_schema')=='activation-report.v1' and ref not in selected:selected.append(ref)
        if not selected:return None
        if len(selected)!=1:raise ValueError('Provide one complete activation report for this Task')
        ref=selected[0];document=self.documents.read(task['project_id'],ref);payload=document['payload']
        accepted=task['request_json']['accepted_documents']
        if document.get('task_id')!=str(task['run_id']) or not same_resource_ref(payload['publication_ref'],accepted.get('publication')):
            raise PermissionError('Activation report belongs to another Task/publication')
        if payload['unverified']:raise PermissionError('Activation contains unverified obligations')
        design=self.documents.read(task['project_id'],accepted['solution_design'])['payload']
        definitions=[self.units.read_definition(task['project_id'],x['definition_ref']) for x in handoffs]
        if {x['runtime_unit_id'] for x in definitions}!={x['runtime_unit_id'] for x in design['runtime_designs']}:
            raise PermissionError('Activation must account for every Unit in the accepted runtime design')
        qualified=[];referenced=list(references(payload))
        armed=[]
        if design.get('schedule_plans'):
            from assistant_kernel.services.schedule_delivery import verify_activation_locked
            implementation=self.documents.read(task['project_id'],accepted['implementation_report'])['payload']
            armed=await verify_activation_locked(c,self.settings,self.repository,task['project_id'],design,implementation,payload)
        contract=self.documents.read(task['project_id'],accepted['task_contract'])['payload']
        for handoff in handoffs:
            definition_ref=handoff['definition_ref']
            if not any(same_resource_ref(definition_ref,x) for x in payload['actual_version_refs']):raise PermissionError('Activation omits an actual fixed Unit version')
            definition=self.units.read_definition(task['project_id'],definition_ref)
            plans=[x for x in design.get('schedule_plans',[]) if x['runtime_unit_id']==definition['runtime_unit_id']]
            requirements={identity for x in contract.get('runtime_requirements',[]) if x['runtime_unit_id']==definition['runtime_unit_id'] for identity in x['acceptance_ids']}
            configured=[x for x in armed if x['runtime_unit_id']==definition['runtime_unit_id']]
            if plans and not any(x['requires_first_run'] for x in plans) and configured and requirements<={i for x in configured for i in x['criterion_ids']}:
                continue # Explicit configured-only acceptance; never claim an executed business result.
            if definition['lifecycle_kind']=='service':
                instances=await (await c.execute("SELECT * FROM assistant.service_instances WHERE project_id=%s AND unit_id=%s AND definition_ref=%s AND observed_state='ready' AND cleanup_status='pending'",
                    (task['project_id'],definition['runtime_unit_id'],__import__('psycopg.types.json',fromlist=['Jsonb']).Jsonb(definition_ref)))).fetchall()
                checked=False
                for row in instances:
                    state=row['state_json']
                    if not state.get('readiness_ref') or not state.get('assembly_ref'):continue
                    manifest=self.documents.read(task['project_id'],definition['implementation_manifest_ref'])['payload']
                    work=[x for x in state.get('work_records',{}).values() if x.get('status')=='completed' and
                        x.get('checks',{}).get('contract_status')=='passed' and x.get('checks',{}).get('business_status')=='passed' and
                        x.get('checks',{}).get('effect_status') in {'none','verified'}]
                    if (manifest['io']['business_check_refs'] or manifest['expected_effects']) and not work:continue
                    qualified.append((str(row['instance_id']),state['readiness_ref'],'service_instances'))
                    qualified.extend((str(row['instance_id']),x['receipt_ref'],'service_instances') for x in work)
                    if any(same_resource_ref(x,state['readiness_ref']) for x in referenced):checked=True
                if not checked:raise PermissionError('Service activation lacks actual ready and assembly evidence')
                continue
            rows=await (await c.execute("""SELECT * FROM assistant.runs WHERE project_id=%s AND run_type='python' AND status='succeeded'
                AND request_json#>>'{definition_ref,uri}'=%s AND request_json#>>'{definition_ref,sha256}'=%s""",
                (task['project_id'],definition_ref['uri'],definition_ref['sha256']))).fetchall()
            unit_checked=False
            for row in rows:
                attempt=(row['executor_json'] or {}).get('python_attempt') or {}
                if (attempt.get('cleanup',{}).get('status')=='verified' and attempt.get('contract_status')=='passed'
                        and attempt.get('business_status')=='passed' and attempt.get('effect_status') in {'none','verified'}):
                    qualified.append((str(row['run_id']),attempt.get('settlement_ref'),'runs'))
                    if any(x.get('uri')=='db://assistant/runs/'+str(row['run_id']) or same_resource_ref(x,attempt.get('settlement_ref')) for x in referenced):unit_checked=True
            if not unit_checked:raise PermissionError('Activation has no actual verified execution of a required Unit')
        for check in payload['per_item_checks']:
            if check['result']!='passed':raise PermissionError('Required activation check did not pass')
            evidence=list(references(check['evidence_refs']))
            executed=any(any((x.get('uri')=='db://assistant/'+table+'/'+identity) or same_resource_ref(x,settlement) for x in evidence)
                    for identity,settlement,table in qualified)
            configured=any(set(check['criterion_ids'])<=set(item['criterion_ids']) and any(any(same_resource_ref(x,ref) for x in evidence) for ref in item['receipt_refs']) for item in armed)
            if not executed and not configured:
                raise PermissionError('Activation check lacks a succeeded, business/effect-checked and cleaned actual Run')
        return ref
