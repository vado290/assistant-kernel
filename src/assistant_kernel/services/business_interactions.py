"""One fixed graph wait/resume over existing Runs, QuestionGroups and Outbox."""
import hashlib
from copy import deepcopy
from psycopg.types.json import Jsonb
from assistant_kernel.services.business_runtime import BusinessRuntime
from assistant_kernel.services.contracts import contracts,canonical_json
from assistant_kernel.services.document_runtime import resource_ref,same_resource_ref
from assistant_kernel.services.question_groups import QuestionGroupService,GROUP_SCHEMA


class BusinessInteractions:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.runtime=BusinessRuntime(settings,repository)

    async def recover_answered(self):
        """Existing maintenance repairs answer-commit/API-restart gaps without a model."""
        async with self.repository.database.connection() as c:
            rows=await (await c.execute("""SELECT r.run_id FROM assistant.runs r JOIN assistant.messages m
                ON m.message_id::text=r.executor_json#>>'{runtime_wait,group_id}'
                WHERE r.run_type='python' AND r.status='waiting_user' AND r.cancel_requested_at IS NULL
                AND m.content_json->>'schema_version'='kernel-question-group.v1'
                AND m.content_json#>>'{group,status}'='answered'
                ORDER BY r.updated_at LIMIT 20""")).fetchall()
        resumed=[]
        for row in rows:
            try:
                result=await self.resume(str(row['run_id']))
                if result:resumed.append(str(result['run_id']))
            except (ValueError,PermissionError,FileNotFoundError):
                # Keep accepted answers and the waiting owner; no guessed replay.
                continue
        return resumed

    async def wait(self,run,request):
        from assistant_kernel.services.runtime_units import RuntimeUnits
        definition=RuntimeUnits(self.settings,self.repository).read_definition(run['project_id'],run['request_json']['runtime_unit_ref'])
        if definition['workflow']['framework']!='langgraph':raise PermissionError('Only the fixed persistent graph can wait for this checkpoint')
        checkpoint=request['checkpoint_ref'];roots=self.runtime.roots(run)
        path=self.runtime.fixed.source_path(run['project_id'],checkpoint,roots)
        state_root=self.runtime.fixed.runtime.describe(run['project_id']).vault_root/'business-state'/hashlib.sha256(canonical_json([
            definition['runtime_unit_id'],definition['workflow']['graph_id'],definition['workflow']['namespace'],run['request_json']['logical_execution_id']])).hexdigest()
        if not path.resolve().is_relative_to((state_root/'snapshots').resolve()):raise PermissionError('Checkpoint is outside this actual logical graph namespace')
        import sqlite3
        with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as connection:
            columns=connection.execute('SELECT thread_id,checkpoint_ns,checkpoint_id FROM checkpoints').fetchall()
        logical=run['request_json']['logical_execution_id']
        current=await self.repository.get_run(str(run['run_id']))
        prior=current['executor_json'].get('runtime_wait')
        if prior:
            async with self.repository.database.connection() as c:
                canonical=await (await c.execute('SELECT content_json FROM assistant.messages WHERE message_id=%s',(prior['group_id'],))).fetchone()
            if (canonical and canonical['content_json']['group']['questions']==request['questions']
                    and same_resource_ref(prior['runtime_owner']['checkpoint_ref'],checkpoint) and prior['interrupt_ids']==request['interrupt_ids']):return prior
            raise PermissionError('This execution already owns another pending checkpoint/question')
        from assistant_kernel.services.business_budget import snapshot
        budget_ref=await snapshot(self.settings,self.repository,run)
        if self.runtime.documents.read(run['project_id'],budget_ref)['payload']['pending_calls']:
            raise PermissionError('Running/unknown model work must settle before graph wait releases its Worker')
        if not columns or any(x[0]!=logical for x in columns):raise PermissionError('Checkpoint belongs to another business thread')
        if request['business_thread_id']!=logical or not request.get('interrupt_ids'):raise PermissionError('Graph wait lacks its actual interrupted thread')
        manifest=self.runtime.fixed.read_manifest(run['project_id'],run['request_json']['manifest_ref'])
        source=manifest['ownership']['creating_task_ref'] or definition['design_ref']
        task_id=source['id'] if source['uri'].startswith('db://assistant/runs/') else self.runtime.documents.read(run['project_id'],definition['design_ref']).get('task_id')
        async with self.repository.database.connection() as c:
            owner=await (await c.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(run['run_id'],))).fetchone()
            if owner['status']!='running' or str(owner['claim_token'])!=str(run['claim_token']) or owner.get('cancel_requested_at'):
                raise PermissionError('Graph wait no longer has its active claim')
            task=await (await c.execute('SELECT * FROM assistant.runs WHERE run_id=%s',(task_id,))).fetchone()
            if not task or task['project_id']!=run['project_id'] or not task['trigger_message_id']:raise PermissionError('Runtime question lacks its actual originating user message')
            source=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(task['trigger_message_id'],))).fetchone()
            root_id=run['request_json'].get('runtime_root_run_id') or str(run['run_id'])
            root=await (await c.execute('SELECT executor_json FROM assistant.runs WHERE run_id=%s',(root_id,))).fetchone()
            effects=[x['receipt_ref'] for x in root['executor_json'].get('runtime_effects',{}).values() if x.get('receipt_ref')]
            runtime_owner={'runtime_unit_ref':run['request_json']['runtime_unit_ref'],'definition_ref':run['request_json']['definition_ref'],
                'logical_execution_ref':resource_ref(kind='receipt',identity=root_id,uri='db://assistant/runs/'+root_id,purpose='same logical business execution'),
                'business_thread_id':logical,'checkpoint_ref':checkpoint,'prior_effect_refs':effects,'control_ref':None}
            questions=QuestionGroupService(self.settings,self.repository)
            routes=await questions.resolve_routes_locked(c,owner=owner,source=source,interaction_scope='runtime_business')
            canonical=await questions.create_locked(c,owner=owner,questions=request['questions'],source_message_id=str(source['message_id']),
                routes=routes,interaction_scope='runtime_business',blocking=True,runtime_owner=runtime_owner)
            pending={'group_id':str(canonical['message_id']),'runtime_owner':runtime_owner,'interrupt_ids':request['interrupt_ids'],'budget_ref':budget_ref}
            prior=owner['executor_json'].get('runtime_wait')
            if prior and prior!=pending:raise PermissionError('This execution already has a different pending graph checkpoint')
            await c.execute('UPDATE assistant.runs SET executor_json=executor_json || %s,waiting_question_message_id=%s WHERE run_id=%s',
                (Jsonb({'runtime_wait':pending}),canonical['message_id'],run['run_id']));await c.commit()
        return pending

    async def resume(self,run_id):
        """Called both after answer acceptance and after process cleanup; either order is safe."""
        from assistant_kernel.services.runtime_units import RuntimeUnits
        run=await self.repository.get_run(run_id)
        if not run or run['run_type']!='python' or not run['executor_json'].get('runtime_wait'):return None
        definition=RuntimeUnits(self.settings,self.repository).read_definition(run['project_id'],run['request_json']['runtime_unit_ref'])
        checkpoint=run['executor_json']['runtime_wait']['runtime_owner']['checkpoint_ref']
        self.runtime.fixed.source_path(run['project_id'],checkpoint,self.runtime.roots(run))
        # Check source closure/inputs again at dispatch; here protect canonical answers and exact Unit head.
        async with self.repository.database.connection() as c:
            owner=await (await c.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(run_id,))).fetchone()
            wait=owner['executor_json'].get('runtime_wait')
            if owner['status']!='waiting_user' or owner.get('cancel_requested_at'):return None
            unit=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR SHARE',
                (owner['project_id'],definition['runtime_unit_id']))).fetchone()
            if not unit or unit['desired_state']!='running' or not same_resource_ref(
                    unit['effective_definition_ref'],owner['request_json']['runtime_unit_ref']):return None
            group=await (await c.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',(wait['group_id'],))).fetchone()
            if not group or group['project_id']!=owner['project_id'] or group['content_json'].get('schema_version')!=GROUP_SCHEMA:return None
            formal=group['content_json']['group']
            if formal['status']!='answered' or formal['runtime_owner']!=wait['runtime_owner']:return None
            if owner['executor_json'].get('runtime_resume_run_id'):return None
            root_id=owner['request_json'].get('runtime_root_run_id') or run_id
            root=await (await c.execute('SELECT executor_json FROM assistant.runs WHERE run_id=%s',(root_id,))).fetchone()
            if any(x['status']!='succeeded' for x in root['executor_json'].get('runtime_effects',{}).values()):return None
            answers={x['question_id']:x['value'] for x in formal['accepted_answers']}
            request=deepcopy(owner['request_json']);request.pop('capacity_reservation',None);request.pop('capacity_wait',None)
            request.update(runtime_root_run_id=request.get('runtime_root_run_id') or run_id,runtime_resume_from_run_id=run_id,
                runtime_resume_answers=answers,runtime_interrupt_ids=wait['interrupt_ids'],resume_checkpoint_ref=checkpoint,
                runtime_question_group_id=wait['group_id'],prior_effect_refs=wait['runtime_owner']['prior_effect_refs'])
            request['accumulated_budget_ref']=wait['budget_ref']
            request['unit_control_revision']=unit['control_revision']
            # This is a new process segment, retaining the same Work, code, inputs and ledger.
            row=await self.repository.create_run(project_id=owner['project_id'],agent_key=owner['agent_key'],run_type='python',
                parent_run_id=str(owner['parent_run_id']) if owner['parent_run_id'] else None,
                request=request,idempotency_key='business-resume:'+wait['group_id'],max_attempts=1)
            await c.execute("UPDATE assistant.runs SET status='succeeded',ended_at=now(),executor_json=executor_json || %s,waiting_question_message_id=NULL,updated_at=now() WHERE run_id=%s",
                (Jsonb({'runtime_resume_run_id':str(row['run_id'])}),run_id));await c.commit()
            return row
