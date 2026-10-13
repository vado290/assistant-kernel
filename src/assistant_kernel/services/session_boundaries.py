"""Atomic Task/session boundary primitive; no model summary or business replay.

The stage controller supplies its existing approval/result validator inside the
same transaction. This primitive is internal and is not exposed as a model tool.
"""
from __future__ import annotations
import hashlib
import uuid

from psycopg.types.json import Jsonb
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import resource_ref


class SessionBoundaryService:
    def __init__(self,settings):self.database=AssistantDatabase(settings)

    async def commit(self,project_id,request,*,session_scope,validate_transition=None):
        async with self.database.connection() as connection:
            result=await self.commit_locked(connection,project_id,request,session_scope=session_scope,
                                            validate_transition=validate_transition)
            await connection.commit()
            return result

    async def commit_locked(self,connection,project_id,request,*,session_scope,validate_transition=None):
        contracts().validate('SessionRequest',request)
        task_id=str(uuid.UUID(request['task_ref']['id']))
        if request['task_ref']['uri']!='db://assistant/runs/'+task_id:
            raise ValueError('Session boundary needs the actual Task reference')
        digest=hashlib.sha256(canonical_json(request)).hexdigest()
        key='session-boundary:'+project_id+':'+task_id+':'+digest
        task=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task_id,))).fetchone()
        if not task or task['project_id']!=project_id:raise PermissionError('Session Task belongs to another Project')
        prior=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(key,))).fetchone()
        if prior:return {**prior['value'],'status':'duplicate'}
        state=dict(task['request_json'] or {})
        mode=state.get('current_stage') or state.get('work_mode')
        if mode!=request['from_mode'] or int(state.get('state_revision') or 0)!=request['expected_state_revision']:
            raise ValueError('Session boundary Task head is stale')
        epoch=int(state.get('context_epoch') or 0)
        if epoch!=request['expected_context_epoch']:raise ValueError('Session context epoch is stale')
        if task['status'] in {'running','reconciling'}:
            raise ValueError('Settle execution and unknown effects before a session boundary')
        if task['status'] in {'cancelled','failed','succeeded'}:
            raise ValueError('Session boundary cannot revive a terminal Task')
        if task.get('waiting_question_message_id') or any(state.get(key) for key in (
                'pending_input','pending_interaction_ref','pending_supervisor_questions',
                'pending_operation_runs','unknown_effects')):
            raise ValueError('Resolve required input/effects before a session boundary')
        changed=request['from_mode']!=request['target_mode']
        if changed and (request['action']!='fresh' or request['reason']!='phase_changed'):
            raise ValueError('A phase change requires a fresh phase_changed boundary')
        if request['action']=='fresh' and request['reason']=='none':raise ValueError('Fresh boundary requires a real reason')
        if validate_transition is None:
            raise PermissionError('Approval, context-policy and required-result validator must participate in this transaction')
        await validate_transition(connection,task,request)
        if set(session_scope)!={'agent_key','channel','chat_id'} or session_scope['agent_key']!=task['agent_key']:
            raise PermissionError('Session identity differs from the Task actor')
        await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
            ('session:'+project_id+':'+session_scope['agent_key']+':'+session_scope['channel']+':'+session_scope['chat_id'],))
        previous=await (await connection.execute("SELECT * FROM assistant.sessions WHERE project_id=%s AND agent_key=%s AND channel=%s AND chat_id=%s AND status IN ('active','rotation_required') ORDER BY updated_at DESC LIMIT 1 FOR UPDATE",
            (project_id,session_scope['agent_key'],session_scope['channel'],session_scope['chat_id']))).fetchone()
        previous_id=str(previous['session_id']) if previous else None
        if request['action']=='keep' and previous is None:raise ValueError('Keep requires an existing session')
        next_id=previous_id
        if request['action']=='fresh' or previous is None:
            next_id=str(uuid.uuid4())
            if previous:
                await connection.execute("UPDATE assistant.sessions SET status='sealed',ended_at=now(),updated_at=now() WHERE session_id=%s",(previous_id,))
            await connection.execute("INSERT INTO assistant.sessions(session_id,logical_session_id,project_id,agent_key,channel,chat_id,parent_session_id,status) VALUES (%s,%s,%s,%s,%s,%s,%s,'active')",
                (next_id,str(previous['logical_session_id']) if previous else next_id,project_id,
                 session_scope['agent_key'],session_scope['channel'],session_scope['chat_id'],previous_id))
        revision=int(state.get('state_revision') or 0)+1
        epoch+=int(request['action']=='fresh')
        def session_ref(identity):
            return resource_ref(kind='receipt',identity=identity,uri='db://assistant/sessions/'+identity,
                                purpose='Kernel session boundary identity; native ID assigned by the actual CLI') if identity else None
        receipt={'boundary_id':'boundary-'+digest,'task_ref':request['task_ref'],'action':request['action'],
            'reason':request['reason'],'from_mode':request['from_mode'],'target_mode':request['target_mode'],
            'status':'committed','previous_session_ref':session_ref(previous_id),'next_session_ref':session_ref(next_id),
            'state_revision':revision,'context_epoch':epoch,'evidence_refs':request['source_refs'],
            'next_action':('resume_same_native_session' if previous and previous.get('native_session_id')
                else 'start_prepared_session') if request['action']=='keep' else 'fresh_from_accepted_artifacts'}
        contracts().validate('SessionBoundaryReceipt',receipt)
        patch={'current_stage':request['target_mode'],'work_mode':request['target_mode'],
            'state_revision':revision,'context_epoch':epoch,'session_boundary_receipt':receipt,
            'force_session_rotation':None}
        await connection.execute('UPDATE assistant.runs SET session_id=%s,request_json=request_json || %s,updated_at=now() WHERE run_id=%s',
            (next_id,Jsonb(patch),task_id))
        await connection.execute('INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s)',(key,Jsonb(receipt)))
        return receipt
