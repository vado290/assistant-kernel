"""Configured v1.2 Supervisor context and settlement; proposals never grant control."""
from copy import deepcopy
from datetime import datetime,timezone
import os
import sys
import uuid

from psycopg.types.json import Jsonb

from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.services.contracts import contracts


def empty_intent():
    return {'scope':'project_only','explicit_goals':[],'confirmed_preferences':[],
        'current_concerns':[],'hypotheses':[],'pending_confirmations':[]}


class SupervisorV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)
        self.prompts=PromptProtocol(settings,repository)

    async def prepare_context(self,control):
        """Record the actual facts head before the model, without evaluating it."""
        project=control['project_id'];request=control['request_json'] or {};task_id=request.get('task_id')
        async with self.repository.database.connection() as connection:
            task=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task_id,))).fetchone() if task_id else None
            consultation=bool(request.get('mainline_context_only') or not task_id)
            owner=control if consultation else task;owner_id=str(owner['run_id']);state=(task or control)['request_json'] or {}
            from assistant_kernel.services.published_agents import PublishedAgentService
            next_package=await PublishedAgentService(self.settings).foreground_package_locked(connection,task,prefer_accepted_head=True) if task else None
            current_control=await (await connection.execute('SELECT executor_json FROM assistant.runs WHERE run_id=%s',(control['run_id'],))).fetchone()
            fixed_control=((current_control or {}).get('executor_json',{}).get('resolved_supervisor_configuration') or {}).get('supervisor_package_ref')
            if task and fixed_control:next_package=(fixed_control,PublishedAgentService(self.settings).read_package(project,fixed_control))
            head=await self.prompts.input_head_locked(connection,owner)
            mainline_head=await self.prompts.input_head_locked(connection,task) if task else None
            sources=self.prompts.read_blob(project,head)['inputs']
            refs=[resource_ref(kind='message',identity=s['message_id'],uri='db://assistant/messages/'+s['message_id'],
                purpose='actual non-revoked user input') for s in sources if not s['revoked']]
            trigger_id=request.get('answer_message_id') or control['trigger_message_id']
            trigger=await (await connection.execute('SELECT project_id FROM assistant.messages WHERE message_id=%s',(trigger_id,))).fetchone()
            if not trigger or trigger['project_id']!=project:raise PermissionError('Supervisor trigger is outside its Project')
            trigger_ref=resource_ref(kind='message',identity=str(trigger_id),uri='db://assistant/messages/'+str(trigger_id),
                purpose='actual trigger supplied to this Supervisor evaluation; not a user authorization')
            accepted={key:ref for key,ref in (state.get('accepted_documents') or {}).items() if isinstance(ref,dict) and ref.get('kind')=='document'}
            loaded={key:self.documents.read(project,ref) for key,ref in accepted.items()}
            validation=state.get('validation_artifact') or {}
            validation_ref=(resource_ref(kind='source',identity='validation:'+validation['sha256'],
                uri=validation['uri'],sha256=validation['sha256'],purpose='Kernel validation evidence; independent Review still required')
                if validation.get('uri') and validation.get('sha256') else None)
            if validation_ref:
                self.prompts.read_blob(project,validation_ref)
            mode=state.get('current_stage') or 'requirements'
            report_kind={'requirements':'task_contract','design':'solution_design'}.get(mode)
            last_turn=loaded.get('last_turn',{}).get('payload')
            pending_report=None
            if report_kind and last_turn and last_turn.get('outcome') in {'completed','needs_approval'}:
                ref=accepted.get(report_kind);approval=state.get(mode+'_approval') or {}
                if ref and approval.get('approved_document_sha256')!=ref['sha256']:pending_report=ref
            task_ref=resource_ref(kind='receipt',identity=owner_id,uri='db://assistant/runs/'+owner_id,purpose='actual logical owner')
            active_ref=resource_ref(kind='receipt',identity=str(task['run_id']),uri='db://assistant/runs/'+str(task['run_id']),purpose='current mainline Task') if task else None
            facts_ref=self.prompts.write_blob(project,owner_id,'supervisor-facts-basis',{
                'input_head_ref':head,'mainline_input_head_ref':mainline_head,'accepted_documents':accepted,
                'validation_status':state.get('validation_gate_status'),'validation_ref':validation_ref,
                'audit_complete':bool(state.get('audit_complete')),'publication_state':state.get('publication_state')})
            version={'task_id':str(task['run_id']),'state_revision':int(state.get('state_revision') or 0),
                'context_epoch':int(state.get('supervisor_context_epoch') or 0)} if task else None
            active={'task_id':str(task['run_id']),'task_ref':active_ref,'goal':state.get('task_goal') or 'Original user work',
                'work_mode':state.get('current_stage') or 'requirements','status':task['status'],
                'state_revision':version['state_revision'],'context_epoch':version['context_epoch'],
                'accepted_document_refs':list(accepted.values())} if task else None
            group_rows=await (await connection.execute("""SELECT message_id,content_json FROM assistant.messages
                WHERE project_id=%s AND message_type='question_group' AND content_json->'group'->>'status'='open'
                AND (run_id=%s OR content_json->>'task_id'=%s) ORDER BY created_at,message_id""",
                (project,owner['run_id'],owner_id))).fetchall()
            group_refs=[resource_ref(kind='message',identity=str(r['message_id']),uri='db://assistant/messages/'+str(r['message_id']),
                purpose='unanswered logical question; draft edits are not formal answers') for r in group_rows]
            known_refs=refs+list(accepted.values())+group_refs+([validation_ref] if validation_ref else [])
            if not any(ref['kind']=='message' and ref['id']==str(trigger_id) for ref in known_refs):known_refs.append(trigger_ref)
            from assistant_kernel.services.supervisor_assessment_v12 import SupervisorAssessmentV12
            assessments=SupervisorAssessmentV12(self.settings,self.repository)
            previous=await assessments.previous_locked(connection,project)
            previous_head=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',
                (assessments.key(project),))).fetchone()
            intent=assessments.visible_intent(previous['user_intent_context']) if previous else empty_intent()
            now=datetime.now(timezone.utc).isoformat()
            snapshot={'project_contract_ref':None,'active_task':active,'queued_work':[],'suspended_tasks':[],
                'operational_baseline_ref':state.get('operational_baseline_ref') if isinstance(state.get('operational_baseline_ref'),dict)
                    and state['operational_baseline_ref'].get('kind')=='document' else None,
                'runtime_observation_refs':[],'progress':[],'pending':[],'user_intent_context':intent,
                'recent_change_refs':[],'next_step':None,
                'facts_basis':{'source_batch_id':facts_ref['sha256'],'task_versions':[version] if version else [],
                    'source_refs':known_refs,'as_of':now},
                'project_assessment':{'status':'facts_only','assessed_at':None,'evaluated_source_batch_id':None,
                    'covered_task_versions':[],'covered_source_refs':[],'evidence_refs':[],
                    'summary':'Current DB and pinned document facts; no evaluation has been applied.','blockers':[]},
                'coverage':{'status':'partial','covered_refs':known_refs,'missing_refs':[],
                    'notes':['This snapshot covers the current interaction and accepted Task documents. '
                        'Project-wide runtime, queued-work and external observations must be fetched when relevant.']},
                'latest_input_refs':refs,'latest_decision_refs':[],'authorized_action_refs':[],
                'recent_interaction_ref':refs[-1] if refs else None,'in_progress_refs':group_refs,
                'dependencies':[],'unconfirmed_requirement_refs':[]}
            snapshot_ref=self.documents.write_scoped_revision(project_id=project,scope='run',object_id='supervisor-snapshot',
                task_id=str(task['run_id']) if task else None,run_id=str(control['run_id']),document_kind='project-snapshot',
                document_schema='project-snapshot.v1',payload=snapshot,status='draft',actor_type='kernel',actor_id='kernel',
                reason='Capture actual facts before a configured Supervisor evaluation',source_refs=refs)
            context={'schema_version':'kernel-prompt-compilation-context.v1','project_id':project,'task_id':str(task['run_id']) if task else None,
                'control_run_id':str(control['run_id']),
                'owner_ref':task_ref,'input_head_ref':head,'facts_basis_ref':facts_ref,'snapshot_ref':snapshot_ref,'user_message_refs':refs,
                'trigger_message_ref':trigger_ref,
                'state_revision':int((owner['request_json'] or {}).get('state_revision') or 0),
                'context_epoch':int((owner['request_json'] or {}).get('supervisor_context_epoch') or 0),
                'accepted_documents':accepted,'open_group_refs':group_refs,
                'phase_state':{'current_mode':mode,'last_settled_turn':last_turn,
                    'validation_status':state.get('validation_gate_status'),'validation_ref':validation_ref,
                    'audit_complete':bool(state.get('audit_complete')),'publication_state':state.get('publication_state'),
                    'pending_document_approval_ref':pending_report,
                    'pending_document_payload':loaded[report_kind]['payload'] if pending_report else None},
                'runtime_package_ref':next_package[0] if next_package else (owner['executor_json'] or {}).get('task_runtime_package_ref') or state.get('pending_runtime_package_ref'),
                'consultation_only':consultation,'mainline_input_head_ref':mainline_head,
                'previous_snapshot_ref':previous_head['value']['snapshot_ref'] if previous_head else None,
                'consultation_context_epoch':int(request.get('consultation_context_epoch') or 0)}
            saved=await (await connection.execute("""UPDATE assistant.runs SET executor_json=executor_json || %s
                WHERE run_id=%s AND status='running' AND claim_token=%s AND worker_instance_id=%s
                  AND cancel_requested_at IS NULL AND lease_until>now() RETURNING run_id""",
                (Jsonb({'prompt_compilation_context':context}),control['run_id'],control['claim_token'],control['worker_instance_id']))).fetchone()
            if not saved:raise PermissionError('Supervisor lease changed before its facts snapshot')
            await connection.commit()
            return context

    def model_context(self, control, context, current_message=None):
        """Use the captured v1.2 facts directly, without a second legacy snapshot."""
        project=control['project_id']
        facts=self.documents.read(project,context['snapshot_ref'])['payload']
        inputs=self.prompts.read_blob(project,context['input_head_ref'])['inputs']
        phase=context['phase_state']
        documents={}
        for kind,ref in context['accepted_documents'].items():
            # Inline business baselines once. Historical execution packages stay
            # addressable by ref, rather than replaying their old instructions.
            if kind not in {'task_contract','solution_design','implementation_report','review_report'}:continue
            if ref==phase['pending_document_approval_ref']:continue
            documents[kind]={'ref':ref,'payload':self.documents.read(project,ref)['payload']}
        request=control['request_json'] or {}
        return {'meta':{'project_id':project,'task_id':context['task_id'],
                'control_run_id':str(control['run_id']), 'consultation_run_id':str(control['run_id']),
                'trigger':request.get('trigger') or request.get('reason')},
            'prompt_compilation_context':context,'current_facts':facts,
            'read_only_query_policy':{
                'status_and_list_queries':'Directly use available read-only tools and answer with decision=consultation_reply. Do not create a PromptResult or propose Operate for a pure status/list question.',
                'evidence':'Use actual tool results and existing invocation evidence; do not create separate query snapshots.',
                'writes':'Registration, changes, start/stop, execution and publication retain all existing approval gates.',
                'existing_work':'Answer a side question without completing, approving or changing existing engineering work.'},
            'user_inputs':[item for item in inputs if not item['revoked']],
            'accepted_document_contents':documents,
            'consultation_answers':request.get('answer') or {},
            'trigger_message':{'message_id':str((current_message or {}).get('message_id') or ''),
                'direction':str((current_message or {}).get('direction') or ''),
                'text':str(((current_message or {}).get('content_json') or {}).get('text') or '')}}

    async def accept_prompt_result(self,*,control_id,worker_instance_id,claim_token,result,child_result_refs=(),new_task=False,native_child_ref=None,parent_invocation_ref=None,priority='normal'):
        """Kernel-internal admission of the Supervisor's adopted Skill result."""
        contracts().validate('PromptResult',result)
        if result['outcome']!='ready_for_review':raise ValueError('Only a complete proposed draft can produce a PromptProposal')
        if any(item.get('required') for item in result['questions']) or any(item.get('blocking') for item in result['context_gaps']):
            raise PermissionError('Adopted PromptResult still has blocking questions or source gaps')
        async with self.repository.database.connection() as connection:
            basis=await (await connection.execute('SELECT project_id,executor_json FROM assistant.runs WHERE run_id=%s',(control_id,))).fetchone()
            if not basis:raise PermissionError('Supervisor owner missing')
            from assistant_kernel.services.published_agents import PublishedAgentService
            from assistant_kernel.services.task_queue import ProjectTaskQueue
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(PublishedAgentService.key(basis['project_id']),))
            await ProjectTaskQueue._lock(connection,basis['project_id'])
            basis_context=(basis['executor_json'] or {}).get('prompt_compilation_context') or {}
            if basis_context.get('task_id') and not basis_context.get('consultation_only'):
                await connection.execute('SELECT run_id FROM assistant.runs WHERE run_id=%s FOR UPDATE',(basis_context['task_id'],))
            control=await (await connection.execute("""SELECT * FROM assistant.runs WHERE run_id=%s
                AND status='running' AND worker_instance_id=%s AND claim_token=%s
                AND cancel_requested_at IS NULL AND lease_until>now() FOR UPDATE""",
                (control_id,worker_instance_id,claim_token))).fetchone()
            if not control or not control['agent_key'].endswith(':supervisor'):
                raise PermissionError('Prompt proposals require the actual leased Supervisor')
            context=(control['executor_json'] or {}).get('prompt_compilation_context')
            if not context:raise PermissionError('Supervisor has no captured input context')
            input_owner=control if context['consultation_only'] else await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(context['task_id'],))).fetchone()
            if input_owner['project_id']!=control['project_id'] or await self.prompts.input_head_locked(connection,input_owner)!=context['input_head_ref']:
                raise PermissionError('Prompt Skill result belongs to an old facts/input head')
            if new_task:
                from assistant_kernel.services.task_proposals_v12 import TaskProposalsV12
                task=await TaskProposalsV12(self.settings,self.repository).create_locked(connection,control=control,context=context,draft=result['prompt_draft'],priority=priority)
                context={**context,'task_id':str(task['run_id']),'owner_ref':resource_ref(kind='receipt',identity=str(task['run_id']),
                    uri='db://assistant/runs/'+str(task['run_id']),purpose='unconfirmed new-work container'),
                    'state_revision':0,'context_epoch':0,'runtime_package_ref':task['request_json']['pending_runtime_package_ref']}
            else:
                if context['consultation_only'] or not context['task_id']:raise PermissionError('A read-only consultation cannot prepare a PM business Turn')
                task=input_owner
                actions=result['prompt_draft'].get('schedule_actions')
                if actions and not task['request_json'].get('schedule_actions'):
                    if task['attempt'] or task.get('started_at') or task['request_json'].get('accepted_documents'):
                        raise PermissionError('Use a separate daily schedule operation; do not replace existing engineering work')
                    from assistant_kernel.services.schedule_operations import validate_actions
                    await validate_actions(connection,task['project_id'],actions)
                    task=await (await connection.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *',
                        (Jsonb({'schedule_actions':actions}),task['run_id']))).fetchone()
            originals={s['message_id']:s for s in self.prompts.read_blob(control['project_id'],context['input_head_ref'])['inputs'] if not s['revoked']}
            from assistant_kernel.services.source_quotes import actual_source_quote
            result=deepcopy(result)
            quote_bindings=[]
            for alignment in result['source_alignment']:
                if alignment['classification']=='user_original':
                    quote=alignment.get('original_quote')
                    matches=[(ref,actual_source_quote(quote,str(originals[ref['id']]['source'].get('text') or '')))
                        for ref in alignment['source_refs'] if ref['id'] in originals]
                    matched=next(((ref,text) for ref,text in matches if text is not None),None)
                    if not matched:
                        raise ValueError('Claimed original quotation is not in the actual user input')
                    if matched[1]!=quote:
                        quote_bindings.append({'source_ref':matched[0],'submitted_quote':quote,'actual_quote':matched[1],
                            'normalization':'whitespace_and_terminal_period_only'})
                        alignment['original_quote']=matched[1]
            if quote_bindings:
                self.prompts.write_blob(control['project_id'],str(task['run_id']),'source-quote-bindings',
                    {'bindings':quote_bindings,'native_child_ref':native_child_ref,'parent_invocation_ref':parent_invocation_ref})
            assembly_ref=(control['executor_json'] or {}).get('latest_assembly_ref')
            if not assembly_ref:raise PermissionError('Supervisor has no actual configuration/tool assembly receipt')
            assembly=self.prompts.read_blob(control['project_id'],assembly_ref)
            scope=assembly['workspace_scope_ref']
            authority=resource_ref(kind='source',identity='file-authority:'+scope['sha256'],uri=scope['uri'],
                sha256=scope['sha256'],purpose='actual shared Project file agreement used by the parent')
            raw_ref=self.prompts.write_blob(control['project_id'],context['task_id'],'prompt-skill-result',result)
            if native_child_ref:
                record={'delegation_id':native_child_ref['id'],'owner':'supervisor','kind':'prompt_assembly',
                    'parent_invocation_ref':parent_invocation_ref,'input_refs':context['user_message_refs']+[context['snapshot_ref']],
                    'goal':result['understood_goal'],'expected_outputs':['prompt-assembly-result.v1'],
                    'acceptance_criteria':['Valid Skill result, actual parent-linked completion, source/head/scope checked by parent and Kernel'],
                    'declared_write_set':[],'required':True,'inherited_file_authority_ref':authority,
                    'inherited_tool_bindings_ref':assembly_ref,'raw_trace_ref':native_child_ref,
                    'actual_status':'completed','result_ref':raw_ref}
                ref=self.documents.write_scoped_revision(project_id=control['project_id'],scope='task',object_id='delegation-'+native_child_ref['id'],
                    task_id=context['task_id'],run_id=str(control['run_id']),document_kind='delegation-record',document_schema='delegation-record.v1',
                    payload=record,status='submitted',actor_type='kernel',actor_id='kernel',
                    reason='Actual native child result independently adopted by parent; not a business approval',source_refs=[native_child_ref,raw_ref,assembly_ref])
                child_result_refs=[*child_result_refs,ref]
            for ref in child_result_refs:
                child=self.documents.read(control['project_id'],ref)
                if child.get('task_id')!=context['task_id']:raise PermissionError('Child result belongs to another Task')
            payload={'owner_ref':context['owner_ref'],'user_message_refs':context['user_message_refs'],
                'snapshot_ref':context['snapshot_ref'],'basis_state_revision':context['state_revision'],
                'basis_context_epoch':context['context_epoch'],'file_authority_ref':authority,
                'capability_profile_ref':assembly_ref,'draft':deepcopy(result['prompt_draft']),
                'blocking_question_ids':[],'child_result_refs':list(child_result_refs),'clarity':'ready_for_confirmation',
                'scope_change':'material' if result['suggestions'] else 'none','confirmation_source_candidate_refs':[]}
            # Parent assembly is a truthful source, not proof of the target PM's
            # capabilities. Settlement probes that target before any display.
            ref=self.documents.write_scoped_revision(project_id=control['project_id'],scope='task',object_id='prompt-proposal',
                task_id=context['task_id'],run_id=str(control['run_id']),document_kind='prompt-proposal',document_schema='prompt-proposal.v1',
                payload=payload,status='proposed',actor_type='kernel',actor_id='kernel',
                reason='Untrusted Prompt Skill result; parent and target admission remain required',
                source_refs=context['user_message_refs']+[context['snapshot_ref'],assembly_ref])
            if new_task:
                await connection.execute('UPDATE assistant.runs SET executor_json=executor_json || %s WHERE run_id=%s',
                    (Jsonb({'proposed_work_task_id':str(task['run_id'])}),control_id))
            await connection.commit()
            return {'proposal_ref':ref,'raw_result_ref':raw_ref,'status':'proposed','confirmed':False,
                'target_capability_verified':False,'reported_checks_are_claims':True}

    async def receive_parent_result(self, *, control, worker_instance_id, decision):
        """Reuse Artifact refs to receive Skill output; no model submission tool."""
        candidates=[ref for ref in decision['public_result'].get('artifact_refs', [])
            if ref.get('document_schema')=='prompt-assembly-result.v1']
        if not candidates:return decision
        if len(candidates)!=1 or decision.get('prompt_proposal_ref'):
            raise ValueError('Supervisor must adopt one PromptResult, without competing proposal sources')
        ref=candidates[0]
        if ref.get('kind')=='receipt' and ref['uri'].startswith('db://assistant/model_invocations/'):
            result,native_ref=await self.read_adopted_native_child(control=control,ref=ref)
        else:
            prefix='vault://'+control['project_id']+'/runs/'+str(control['run_id'])+'/work/'
            if ref.get('kind')!='source' or not ref['uri'].startswith(prefix):
                raise PermissionError('Adopted PromptResult must be in this Supervisor invocation work directory')
            import hashlib,json
            original=ref
            root=self.documents.runtime.describe(control['project_id']).vault_root
            target=self.documents.runtime.resolve_vault_file(control['project_id'],ref['uri'].removeprefix('vault://'+control['project_id']+'/'))
            if not target.resolve().is_relative_to((root/'runs'/str(control['run_id'])/'work').resolve()):
                raise PermissionError('PromptResult path escapes this invocation work directory')
            encoded=target.read_bytes();digest=hashlib.sha256(encoded).hexdigest()
            if ref.get('sha256') and ref['sha256']!=digest:raise ValueError('Pinned Prompt source hash differs')
            ref={**ref,'sha256':digest};result=json.loads(encoded);native_ref=None
            decision={**decision,'public_result':{**decision['public_result'],
                'artifact_refs':[ref if item==original else item for item in decision['public_result']['artifact_refs']]}}
        contracts().validate('PromptResult',result)
        if result['outcome']!='ready_for_review':
            raise ValueError('A PromptResult with blocking gaps cannot be adopted for execution preparation')
        actions=[item for item in decision.get('proposed_actions',[]) if item['kind']=='queue_task']
        if len(actions)>1:raise ValueError('A PromptResult can propose only one specific new task')
        current=await self.repository.get_run(str(control['run_id']))
        context=current['executor_json']['prompt_compilation_context']
        if actions:
            if actions[0]['goal']!=result['prompt_draft']['objective']:
                raise ValueError('New-work proposal and adopted Prompt objective differ')
            if actions[0]['source_message_id'] not in {item['id'] for item in context['user_message_refs']}:
                raise PermissionError('New work has no actual input source in this evaluation')
        # A first complete Prompt needs a proposed Task container, not another
        # model action repeating its objective. It stays unconfirmed and cannot
        # dispatch. Consultation against an existing Task still needs explicit
        # new-work intent and must never retarget that Task implicitly.
        first_work=not context.get('task_id')
        admitted=await self.accept_prompt_result(control_id=str(control['run_id']),
            worker_instance_id=worker_instance_id,claim_token=str(control['claim_token']),result=result,
            new_task=bool(actions) or first_work,native_child_ref=native_ref,parent_invocation_ref=ref if native_ref else None,
            priority=actions[0]['priority'] if actions else 'normal')
        return {**decision,'prompt_proposal_ref':admitted['proposal_ref'],
            'parent_accepted_prompt_result_ref':native_ref or ref,'kernel_prompt_result_ref':admitted['raw_result_ref']}

    async def read_adopted_native_child(self, *, control, ref):
        """Read an actual child result selected by its parent; no model retyping."""
        import hashlib,json
        from assistant_kernel.services.contracts import strict_json
        invocation=ref['uri'].removeprefix('db://assistant/model_invocations/')
        async with self.repository.database.connection() as connection:
            row=await (await connection.execute("""SELECT * FROM assistant.model_invocations WHERE invocation_id=%s
                AND project_id=%s AND actor_role='goal_supervisor' AND status='succeeded'""",
                (invocation,control['project_id']))).fetchone()
        if not row or not row['native_session_id']:raise PermissionError('Adopted child has no actual successful parent invocation')
        assembly=self.prompts.read_blob(control['project_id'],row['metadata_json']['assembly_ref'])
        context=self.documents.read(control['project_id'],assembly['context_manifest_ref'])
        if context['run_id']!=str(control['run_id']):raise PermissionError('Native child belongs to another Supervisor evaluation')
        runtime=self.documents.runtime.describe(control['project_id']);sessions=runtime.codex_home/'sessions'
        parents=list(sessions.rglob('*'+row['native_session_id']+'.jsonl'))
        if len(parents)!=1:raise PermissionError('Actual native parent archive is missing or ambiguous')
        events=[strict_json(line) for line in parents[0].read_bytes().splitlines()]
        spawned=set()
        spawn_calls={event['payload']['call_id'] for event in events if event.get('type')=='response_item'
            and event['payload'].get('type')=='function_call' and event['payload'].get('name')=='spawn_agent'}
        for event in events:
            body=event.get('payload') or {}
            if body.get('type')=='function_call_output' and body.get('call_id') in spawn_calls:
                try:spawned.add(strict_json(body['output'])['task_name'])
                except (ValueError,KeyError,TypeError):pass
        if ref['id'] not in spawned:raise PermissionError('Adopted child was not actually spawned by this parent')
        found=[]
        for path in parents[0].parent.glob('*.jsonl'):
            encoded=path.read_bytes();rows=[strict_json(line) for line in encoded.splitlines()]
            meta=next((event['payload'] for event in rows if event.get('type')=='session_meta'),{})
            source=meta.get('source');origin=(source.get('subagent') or {}).get('thread_spawn') or {} if isinstance(source,dict) else {}
            if origin.get('parent_thread_id')!=row['native_session_id'] or origin.get('agent_path')!=ref['id']:continue
            complete=any(event.get('type')=='event_msg' and event['payload'].get('type')=='task_complete' for event in rows)
            answers=[event['payload'] for event in rows if event.get('type')=='response_item'
                and event['payload'].get('type')=='message' and event['payload'].get('role')=='assistant'
                and event['payload'].get('phase')=='final_answer']
            if not complete or len(answers)!=1:raise PermissionError('Native child has no unique completed result')
            text=''.join(item.get('text','') for item in answers[0].get('content',[]))
            from assistant_kernel.services.goal_supervisor import GoalSupervisorService
            result=strict_json(GoalSupervisorService._strip_code_fence(text))
            contracts().validate('PromptResult',result)
            native_ref=resource_ref(kind='trace',identity=meta['id'],uri='vault://'+control['project_id']+'/'+path.relative_to(runtime.vault_root).as_posix(),
                sha256=hashlib.sha256(encoded).hexdigest(),purpose='actual parent-linked native Prompt assembly result adopted by Supervisor')
            found.append((result,native_ref))
        if len(found)!=1:raise PermissionError('Adopted native child result is missing or ambiguous; retain facts without dispatch')
        return found[0]

    async def probe_pm_capabilities(self,*,control,context,mode,cancel_event=None):
        """Resolve actual target configuration/model and discover required MCP tools."""
        import json
        from assistant_kernel.harness.codex_cli import CodexCliHarness
        from assistant_kernel.services.execution_plan import ProjectResourceResolver
        from assistant_kernel.services.invocation_config import InvocationConfiguration
        from assistant_kernel.services.mcp_runtime import declared_mcp as discover_mcp
        from assistant_kernel.services.published_agents import PublishedAgentService
        package=PublishedAgentService(self.settings).read_package(control['project_id'],context['runtime_package_ref'])
        selector='project:'+control['project_id']+':manager/'+mode
        config_ref=package['configurations'].get(selector)
        if not config_ref:raise ValueError('Accepted package lacks the target PM phase')
        loader=PublishedAgentService(self.settings).loader
        config=loader.load_snapshot(control['project_id'],config_ref['uri'],config_ref['sha256'])
        base_config_ref=config_ref
        task=await self.repository.get_run(context['task_id'])
        config,skill_sources=PublishedAgentService(self.settings).runtime_skills(control['project_id'],config,package,task)
        if skill_sources:
            uri,sha=loader.snapshot(control['project_id'],config)
            config_ref=resource_ref(kind='source',identity='agent-config:'+config.config_sha256,uri=uri,sha256=sha,purpose='fixed phase configuration with applicable published runtime Skills')
        invocation='prompt-preflight-'+str(control['run_id'])+'-'+uuid.uuid4().hex
        resolver=InvocationConfiguration(self.settings)
        resolved,values,resolved_ref=await resolver.resolve(config,project_id=control['project_id'],
            run_id=str(control['run_id']),invocation_id=invocation)
        role_budget=resolver.check_role_budget(resolved,values)
        harness=CodexCliHarness(loader.runtime)
        harness.model_catalog.cache_ttl_seconds=values['model_catalog_cache_seconds']
        model=await harness.resolve_model_profile(project_id=control['project_id'],provider=resolved.provider or 'openai',
            model=resolved.model,reasoning_effort=resolved.reasoning_effort,context_policy=resolved.context_policy,
            custom_context_window_tokens=resolved.model_context_window_tokens,
            custom_auto_compact_token_limit=resolved.model_auto_compact_token_limit)
        resources=ProjectResourceResolver(self.settings)
        scope_uri,scope_sha=resources.workspace_scopes.snapshot(control['project_id'],str(control['run_id']),None,
            variant=invocation,shared_agreement=True)
        _,servers=resources.resolve_mcp_servers(control['project_id'],resolved)
        environment={**os.environ,'KERNEL_SCOPE_PROJECT_ID':control['project_id'],'KERNEL_SCOPE_RUN_ID':str(control['run_id']),
            'KERNEL_SCOPE_CLAIM_TOKEN':str(control['claim_token']),'KERNEL_SCOPE_WORKER_INSTANCE_ID':str(control['worker_instance_id']),
            'KERNEL_SCOPE_ALLOWED_TOOLS':','.join(resolved.allowed_tools),'KERNEL_SCOPE_STAGE':'',
            'KERNEL_SCOPE_SNAPSHOT_URI':scope_uri,'KERNEL_SCOPE_SNAPSHOT_SHA256':scope_sha,
            'KERNEL_SCOPE_EFFECTIVE_PERMISSIONS_JSON':json.dumps({'file_agreement':'project_shared','native_isolation':False}),
            'ASSISTANT_DATA_ROOT':str(self.settings.data_root),'POSTGRES_DB':self.settings.postgres_database}
        receipts=[]
        if resolved.allowed_tools:
            receipts.append(await discover_mcp({'server_id':'kernel_scoped','transport':'stdio','command':sys.executable,
                'args':['-m','assistant_kernel.mcp_server.scoped_tools'],'enabled_tools':list(resolved.allowed_tools),
                'required':True,'startup_timeout_sec':30},environment=environment,cancel_event=cancel_event))
        for server in servers:
            receipts.append(await discover_mcp(server,environment=os.environ,cancel_event=cancel_event))
        authority=resource_ref(kind='source',identity='file-authority:'+scope_sha,uri=scope_uri,sha256=scope_sha,
            purpose='actually resolved current shared Project agreement')
        capability=self.prompts.write_blob(control['project_id'],context['task_id'],'pm-capability-profile',{
            'schema_version':'kernel-pm-capability-profile.v1','target_actor':resolved.agent_key,'work_mode':mode,
            'config_ref':config_ref,'base_config_ref':base_config_ref,'conditional_skill_source_refs':skill_sources,
            'config_bindings':list(config.config_bindings),'resolved_config_ref':resolved_ref,'model_profile':model.model_dump(),
            'role_budget':role_budget,'mcp_discovery':receipts,'file_authority_ref':authority,
            'native_isolation':False,'business_verified':False,'model_called':False})
        return capability,authority

    async def answer_readonly_locked(self,connection,*,task,current,context,source,decision,raw_ref,required_open):
        """Close only a fresh question container; never accept engineering work here."""
        from assistant_kernel.services.v12_evidence import V12Evidence
        public=decision.get('public_result') or {}
        await V12Evidence(self.settings,self.repository).verify(current['project_id'],public.get('evidence_refs',[]),task=task)
        if not decision.get('summary'):raise ValueError('A read-only answer must contain its actual answer')
        reply=await self.repository._create_outbound_locked(connection,project_id=current['project_id'],session_id=None,
            run_id=str(current['run_id']),channel=source['channel'],chat_id=source['chat_id'],user_id=source['user_id'],
            message_type='reply',content={'text':decision['summary'],'origin':'supervisor','context_visibility':'semantic',
                'raw_result_ref':raw_ref,'dispatch_allowed':False,'interaction_kind':'read_only_answer'},
            reply_to_message_id=source['message_id'],idempotency_key='v12-read-only-reply:'+str(current['run_id'])+':'+str(current['attempt']))
        request=(task or {}).get('request_json') or {}
        fresh=bool(task and not context['consultation_only'] and task['attempt']==0 and not task.get('started_at')
            and not request.get('accepted_documents') and not request.get('requirements_approval')
            and not request.get('design_approval') and not task.get('waiting_question_message_id')
            and not required_open and task['status']=='waiting_supervisor')
        if fresh:
            await connection.execute("""UPDATE assistant.runs SET status='succeeded',ended_at=now(),updated_at=now(),
                request_json=request_json || %s,result_json=%s WHERE run_id=%s""",
                (Jsonb({'interaction_kind':'read_only_query','task_phase':'completed','task_activation':'closed'}),
                 Jsonb({'outcome':'read_only_answered','engineering_acceptance':'not_applicable','reply_message_id':str(reply['message_id']),
                     'summary':decision['summary'],'evidence_refs':public.get('evidence_refs',[])}),task['run_id']))
        return {'query_answered':True,'task_completed':fresh,'engineering_acceptance':'not_applicable','reply_message_id':str(reply['message_id'])}

    async def settle(self,*,control,worker_instance_id,decision,cancel_event=None):
        """Commit one evaluation at its observed head; never use legacy safe dispatch."""
        from assistant_kernel.services.question_groups import QuestionGroupService
        readonly=decision.get('decision')=='consultation_reply' or (decision.get('public_result',{}).get('payload') or {}).get('decision')=='consultation_reply'
        if readonly:
            public=decision.get('public_result') or {}
            if (decision.get('prompt_proposal_ref') or decision.get('questions') or public.get('approval_requests')
                    or (public.get('checkpoint') or {}).get('external_effect_refs')
                    or any(ref.get('document_schema')=='prompt-assembly-result.v1' for ref in public.get('artifact_refs',[]))):
                raise PermissionError('A read-only answer cannot also request execution, a Prompt package or approval')
        else:
            decision=await self.receive_parent_result(control=control,worker_instance_id=worker_instance_id,decision=decision)
        context=(await self.repository.get_run(str(control['run_id'])))['executor_json'].get('prompt_compilation_context')
        if not context:raise PermissionError('Configured Supervisor lacks its pre-model facts context')
        project=control['project_id'];owner_id=context['task_id'] or str(control['run_id'])
        raw_ref=self.prompts.write_blob(project,owner_id,'supervisor-result',decision['public_result'])
        proposed=decision.get('prompt_proposal_ref');prepared_ref=None;capability=None
        probe_error=None
        release_display=None
        from assistant_kernel.services.phase_control_v12 import PhaseControlV12
        phases=PhaseControlV12(self.settings,self.repository)
        observed_task=await self.repository.get_run(context['task_id']) if context['task_id'] else None
        observed_state=(observed_task or {}).get('request_json') or {}
        if (not readonly and observed_task and not context['consultation_only'] and observed_state.get('audit_complete')
                and not decision.get('insufficient_evidence') and observed_state.get('publication_state') not in {'queued','published'}
                and observed_state.get('approval_kind')!='release'):
            _,phase_docs=phases.baseline(observed_task)
            if phase_docs.get('solution_design',{}).get('payload',{}).get('publish_targets'):
                release_display=phases.release_display(observed_task)
        proposal_context=context;new_work_task=None
        if proposed:
            proposal=self.documents.read(project,proposed)
            if proposal.get('schema_version')!='prompt-proposal.v1':raise PermissionError('Supervisor proposed another document type')
            if proposal.get('task_id')!=context['task_id']:
                new_work_task=await self.repository.get_run(proposal['task_id'])
                scope=((new_work_task or {}).get('request_json') or {}).get('task_scope_proposal') or {}
                saved=await self.repository.get_run(str(control['run_id']))
                if (not new_work_task or new_work_task['project_id']!=project or scope.get('confirmed')
                        or scope.get('producer_control_run_id')!=str(control['run_id'])
                        or (saved['executor_json'] or {}).get('proposed_work_task_id')!=str(new_work_task['run_id'])):
                    raise PermissionError('Supervisor proposed another Task without a compiled new-work container')
                async with self.repository.database.connection() as basis_connection:
                    work_head=await self.prompts.input_head_locked(basis_connection,new_work_task)
                proposal_context={**context,'task_id':str(new_work_task['run_id']),'input_head_ref':work_head,
                    'runtime_package_ref':new_work_task['request_json']['pending_runtime_package_ref']}
            elif context['consultation_only']:
                raise PermissionError('Consultation cannot prepare execution against the active Task')
            try:
                capability,authority=await self.probe_pm_capabilities(control=control,context=proposal_context,
                    mode=proposal['payload']['draft']['proposed_work_mode'],cancel_event=cancel_event)
                payload={**proposal['payload'],'capability_profile_ref':capability,'file_authority_ref':authority}
                prepared_ref=self.documents.write_scoped_revision(project_id=project,scope='task',object_id='prompt-proposal',
                    task_id=proposal_context['task_id'],run_id=str(control['run_id']),document_kind='prompt-proposal',document_schema='prompt-proposal.v1',
                    payload=payload,status='proposed',actor_type='kernel',actor_id='kernel',
                    reason='Same proposed draft with actual target configuration/model/tool preflight',
                    source_refs=[proposed,capability,authority],supersedes_ref=proposed)
            except (ValueError,PermissionError,FileNotFoundError) as exc:
                probe_error={'error_type':type(exc).__name__,'reason':str(exc)}
        async with self.repository.database.connection() as connection:
            from assistant_kernel.services.published_agents import PublishedAgentService
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(PublishedAgentService.key(project),))
            from assistant_kernel.services.task_queue import ProjectTaskQueue
            await ProjectTaskQueue._lock(connection,project)
            task=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(context['task_id'],))).fetchone() if context['task_id'] else None
            proposed_task=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(new_work_task['run_id'],))).fetchone() if new_work_task else None
            current=await (await connection.execute("""SELECT * FROM assistant.runs WHERE run_id=%s AND status='running'
                AND worker_instance_id=%s AND claim_token=%s AND cancel_requested_at IS NULL AND lease_until>now() FOR UPDATE""",
                (control['run_id'],worker_instance_id,control['claim_token']))).fetchone()
            if not current:raise PermissionError('Supervisor evaluation lease changed before settlement')
            mainline_changed=bool(task and await self.prompts.input_head_locked(connection,task)!=context.get('mainline_input_head_ref',context['input_head_ref']))
            if context['consultation_only']:
                stale=await self.prompts.input_head_locked(connection,current)!=context['input_head_ref']
            else:
                stale=bool(task and (task.get('cancel_requested_at') or task['status'] in {'succeeded','failed','cancelled','dead_letter'} or mainline_changed))
            stale=stale or int((current['request_json'] or {}).get('consultation_context_epoch') or 0)!=context['consultation_context_epoch']
            source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
                ((current['request_json'] or {}).get('answer_message_id') or current['trigger_message_id'],))).fetchone()
            if not source or source['project_id']!=project:raise PermissionError('Supervisor has no same-Project source event')
            outcome='historical_only' if stale else 'settled';waiting=False;group_ref=None;preparation=None;phase_result=None
            from assistant_kernel.services.supervisor_assessment_v12 import SupervisorAssessmentV12
            assessment_commit=await SupervisorAssessmentV12(self.settings,self.repository).commit_locked(connection,
                control=current,context=context,decision=decision,raw_ref=raw_ref,stale=stale or mainline_changed)
            if not stale:
                group_service=QuestionGroupService(self.settings,self.repository)
                questions=decision['questions']
                required_open=bool(task and await (await connection.execute("""SELECT 1 FROM assistant.messages
                    WHERE message_type='question_group' AND (run_id=%s OR content_json->>'task_id'=%s)
                    AND content_json#>>'{group,interaction_scope}'='pm_business' AND content_json#>>'{group,status}'='open'
                    AND (content_json#>>'{group,blocking}')::boolean LIMIT 1""",(task['run_id'],str(task['run_id'])))).fetchone())
                if readonly:
                    phase_result=await self.answer_readonly_locked(connection,task=task,current=current,context=context,
                        source=source,decision=decision,raw_ref=raw_ref,required_open=required_open)
                elif questions:
                    scope='supervisor_consultation' if context['consultation_only'] else 'pm_business'
                    question_owner=current if context['consultation_only'] else task
                    routes=await group_service.resolve_routes_locked(connection,owner=question_owner,source=source,interaction_scope=scope)
                    canonical=await group_service.create_locked(connection,owner=question_owner,questions=questions,
                        source_message_id=str(source['message_id']),routes=routes,interaction_scope=scope)
                    ids=canonical['content_json']['projection_ids'];waiting=context['consultation_only']
                    group_ref=resource_ref(kind='message',identity=str(canonical['message_id']),
                        uri='db://assistant/messages/'+str(canonical['message_id']),purpose='logical Supervisor question group')
                    await connection.execute("UPDATE assistant.runs SET waiting_question_message_id=%s,result_json=%s,error_json=NULL WHERE run_id=%s",
                        (ids[0],Jsonb({'outcome':'needs_input','question_message_ids':ids,'question_group_ref':group_ref}),current['run_id']))
                    if task and not context['consultation_only']:
                        if task['status'] not in {'running','reconciling','succeeded','cancelled'}:
                            await connection.execute("UPDATE assistant.runs SET status='waiting_user',waiting_question_message_id=%s,request_json=request_json || %s WHERE run_id=%s",
                                (ids[0],Jsonb({'pending_interaction_ref':{'message_id':ids[0],'owner_run_id':str(task['run_id']),'group_ref':group_ref}}),task['run_id']))
                elif proposed_task and prepared_ref:
                    preparation=await self.prompts.prepare_locked(connection,task=proposed_task,proposal_ref=prepared_ref,
                        runtime_package_ref=proposal_context['runtime_package_ref'],input_head_ref=proposal_context['input_head_ref'],capability_ref=capability)
                elif task and not context['consultation_only'] and (required_open or task['status'] in {'running','reconciling'}):
                    phase_result={'waiting_for':'required_answers' if required_open else 'settled_turn_boundary','pm_dispatched':False}
                    if required_open and task['status'] not in {'running','reconciling'}:
                        await connection.execute("UPDATE assistant.runs SET status='waiting_user' WHERE run_id=%s",(task['run_id'],))
                elif task and release_display and not context['consultation_only']:
                    phase_result=await phases.prepare_release_locked(connection,task=task,source=source,
                        automatic=release_display[0],markdown=release_display[1])
                elif task and ((task['request_json'] or {}).get('audit_complete') or
                        (task['request_json'] or {}).get('schedule_actions') and (task['request_json'] or {}).get('pending_task_completion')) and not context['consultation_only'] and not decision.get('insufficient_evidence'):
                    try:
                        phase_result=await phases.complete_locked(connection,task=task,source=source)
                    except (ValueError,PermissionError) as exc:
                        probe_error={'error_type':type(exc).__name__,'reason':str(exc)}
                        if source.get('user_id'):
                            await self.repository._create_outbound_locked(connection,project_id=project,session_id=None,
                                run_id=str(current['run_id']),channel=source['channel'],chat_id=source['chat_id'],user_id=source['user_id'],
                                message_type='kernel_notice',content={'text':'任务尚未完成：'+str(exc),
                                    'origin':'supervisor','context_visibility':'semantic','task_id':str(task['run_id']),
                                    'blocking_reason':probe_error,'raw_result_ref':raw_ref},
                                reply_to_message_id=source['message_id'],
                                idempotency_key='v12-completion-blocked:'+str(current['run_id'])+':'+str(current['attempt']))
                    if prepared_ref and (not phase_result or phase_result.get('status')=='rework_required'):
                        preparation=await self.prompts.prepare_locked(connection,task=task,proposal_ref=prepared_ref,
                            runtime_package_ref=context['runtime_package_ref'],input_head_ref=context['input_head_ref'],capability_ref=capability)
                elif task and prepared_ref and not context['consultation_only']:
                    preparation=await self.prompts.prepare_locked(connection,task=task,proposal_ref=prepared_ref,
                        runtime_package_ref=context['runtime_package_ref'],input_head_ref=context['input_head_ref'],capability_ref=capability)
                elif source.get('user_id'):
                    text=decision.get('summary') or '本轮未形成可派发的具体执行包。'
                    if context['consultation_only'] and mainline_changed:text+='\n以上依据本轮读取的固定资料；主线状态已更新，本次咨询没有修改主线。'
                    if probe_error:text+='\n能力核对尚未通过：'+probe_error['reason']
                    await self.repository._create_outbound_locked(connection,project_id=project,session_id=None,
                        run_id=str(current['run_id']),channel=source['channel'],chat_id=source['chat_id'],user_id=source['user_id'],
                        message_type='reply',content={'text':text,'origin':'supervisor','context_visibility':'semantic',
                            'raw_result_ref':raw_ref,'dispatch_allowed':False},reply_to_message_id=source['message_id'],
                        idempotency_key='v12-supervisor-reply:'+str(current['run_id'])+':'+str(current['attempt']))
            result={'outcome':outcome,'protocol_revision':'1.2','raw_result_ref':raw_ref,'stale':stale,'mainline_facts_changed':mainline_changed,
                'question_group_ref':group_ref,'prompt_preparation':preparation,'capability_gap':probe_error,
                'phase_settlement':phase_result,'pm_dispatched':False}
            result['assessment_commit']=assessment_commit
            if waiting:
                await connection.execute("""UPDATE assistant.runs SET status='waiting_user',
                    result_json=result_json || %s,worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                    process_pid=NULL,process_start_ticks=NULL,updated_at=now() WHERE run_id=%s""",(Jsonb(result),current['run_id']))
            else:
                await connection.execute("""UPDATE assistant.runs SET status='succeeded',result_json=%s,error_json=NULL,
                    worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,process_pid=NULL,
                    process_start_ticks=NULL,ended_at=now(),updated_at=now() WHERE run_id=%s""",(Jsonb(result),current['run_id']))
            await connection.commit()
        if phase_result and phase_result.get('task_completed'):
            result['next_foreground']=await ProjectTaskQueue(self.settings,self.repository).activate_next(project)
        return result
