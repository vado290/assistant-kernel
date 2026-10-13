"""Prepare, display and confirm one immutable PM business package.

This compiler owns routing, Task state and confirmation. Model proposals remain
data. Receiving an answer or a Supervisor ``safe`` assessment cannot dispatch.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from psycopg.types.json import Jsonb

from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref, validate_resource_ref
from assistant_kernel.services.published_agents import PublishedAgentService


class PromptProtocol:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository
        self.documents=TaskDocumentService(settings);self.packages=PublishedAgentService(settings)

    def write_blob(self,project_id,task_id,kind,payload):
        encoded=canonical_json(payload);digest=hashlib.sha256(encoded).hexdigest()
        relative=Path('runs')/str(uuid.UUID(task_id))/'prompt-protocol'/kind/(digest+'.json')
        target=self.documents.runtime.ensure(project_id).vault_root/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and target.read_bytes()!=encoded: raise ValueError('Immutable Prompt package collision')
        if not target.exists():
            temporary=target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            temporary.write_bytes(encoded);temporary.replace(target)
        return resource_ref(kind='source',identity=kind+':'+digest,uri='vault://'+project_id+'/'+relative.as_posix(),
            sha256=digest,purpose='immutable '+kind)

    def read_blob(self,project_id,ref):
        ref=validate_resource_ref(ref);contracts().validate('PinnedRef',ref)
        prefix='vault://'+project_id+'/'
        if not str(ref['uri']).startswith(prefix): raise PermissionError('Prompt input belongs to another Project')
        target=self.documents.runtime.resolve_vault_file(project_id,ref['uri'][len(prefix):])
        encoded=target.read_bytes()
        if hashlib.sha256(encoded).hexdigest()!=ref['sha256']: raise ValueError('Pinned Prompt source hash differs')
        return json.loads(encoded)

    def recheck_capabilities(self,project,capability,*,model_profile,mcp_discovery,authority_ref):
        chosen=('provider','model','reasoning_effort','context_policy','model_context_window_tokens',
            'model_auto_compact_token_limit','effective_context_window_tokens')
        if 'config_bindings' in capability:
            fixed={item['path'].rsplit('/',1)[-1]:item['override_value'] for item in capability['config_bindings'] if item['mode']=='override'}
            names={'model_context_window_tokens':'custom_context_window_tokens',
                'model_auto_compact_token_limit':'native_auto_compact_token_limit'}
            chosen=tuple(key for key in chosen if names.get(key,key) in fixed and fixed[names.get(key,key)] is not None)
        if any(capability['model_profile'].get(key)!=model_profile.get(key) for key in chosen):
            raise PermissionError('Selected model capabilities changed after Prompt confirmation')
        def tools(receipts):
            return {item['server_id']:{'transport':item['transport'],
                'requested_tools':sorted(item.get('requested_tools',item.get('loaded_tools',[])))} for item in receipts}
        if tools(capability['mcp_discovery'])!=tools(mcp_discovery):
            raise PermissionError('MCP bindings or tool schemas changed after Prompt confirmation')
        previous={item['server_id']:item for item in capability['mcp_discovery']}
        for actual in mcp_discovery:
            expected=previous[actual['server_id']]
            if actual.get('status')!='declared' and expected.get('status')!='declared':
                keys=('loaded_tools','missing_tools','status','tool_schema_sha256')
                if any(actual.get(k)!=expected.get(k) for k in keys):raise PermissionError('MCP tool schemas or readiness changed')
            elif actual.get('tool_schema_sha256') and expected.get('tool_schema_sha256'):
                if actual['tool_schema_sha256']!=expected['tool_schema_sha256']:raise PermissionError('Pinned MCP schemas changed')
        expected=self.read_blob(project,capability['file_authority_ref']);actual=self.read_blob(project,authority_ref)
        def roots(snapshot):return {root['key']:root for root in snapshot['roots'] if root['key'] not in {'run_work','candidate'}}
        wanted=roots(expected);current=roots(actual)
        def authority(root):return {key:value for key,value in root.items() if key!='description'}
        if any(key not in current or authority(root)!=authority(current[key]) for key,root in wanted.items()):
            raise PermissionError('Shared Project file agreement was revoked or changed incompatibly after Prompt confirmation')

    async def input_head_locked(self,connection,task):
        """Hash immutable source facts, not delivery/navigation/acceptance metadata."""
        request=task['request_json'] or {}
        sources=[str(task['trigger_message_id'])]
        sources += [str(uuid.UUID(str(identity))) for identity in request.get('input_source_message_ids') or []]
        for field in ('source_message_id','answer_message_id'):
            if request.get(field):sources.append(str(uuid.UUID(str(request[field]))))
        rows=await (await connection.execute("""SELECT * FROM assistant.messages
            WHERE project_id=%s AND direction='inbound' AND (message_id=ANY(%s::uuid[]) OR run_id=%s OR content_json->>'task_id'=%s)
              AND COALESCE(content_json->>'context_visibility','semantic') NOT IN ('ui_only','trace_only')
            ORDER BY created_at,message_id""",(task['project_id'],sources,task['run_id'],str(task['run_id'])))).fetchall()
        facts=[]
        from assistant_kernel.services.public_background import PublicBackgroundService
        revoked=PublicBackgroundService(self.settings.data_root,self.settings).revoked_source_message_ids()
        for row in rows:
            body=row['content_json'] or {}
            facts.append({'message_id':str(row['message_id']),'channel':row['channel'],'chat_id':row['chat_id'],
                'user_id':row['user_id'],'external_event_id':row['external_event_id'],
                'source':{k:body[k] for k in ('text','raw','event_request','source_content','answers','choice_ids',
                    'additional_text','question_group_ref','content_version','formal_answers_accepted','execution_instruction') if k in body},
                'revoked':bool(str(row['message_id']) in revoked or str(row.get('reply_to_message_id') or '') in revoked
                    or body.get('context_revoked') or body.get('source_revoked'))})
        return self.write_blob(task['project_id'],str(task['run_id']),'prompt-input-head',{
            'schema_version':'kernel-prompt-input-head.v1','task_id':str(task['run_id']),
            'state_revision':int(request.get('state_revision') or 0),
            'context_epoch':int(request.get('supervisor_context_epoch') or 0),'inputs':facts})

    async def gates_locked(self,connection,task,mode,*,required_child_refs=(),pending_approval_refs=None):
        request=task['request_json'] or {};accepted=request.get('accepted_documents') or {}
        validation=(task['executor_json'] or {}).get('validation_process')
        if validation and validation.get('cleanup',{}).get('status')!='verified':
            raise PermissionError('Validator identity/cleanup is unresolved; reconcile actual results without replay')
        if mode not in {'requirements','design','build','audit','operate'}: raise ValueError('Unknown PM phase')
        if task['status'] in {'running','reconciling','succeeded','failed','cancelled','dead_letter'} or task.get('cancel_requested_at'):
            raise PermissionError('PM dispatch requires a settled nonterminal Task')
        preparing_new=bool(request.get('task_scope_proposal') and request.get('task_activation')=='queued')
        if request.get('task_activation','active')!='active' and not preparing_new: raise PermissionError('A suspended Task needs its explicit resume boundary')
        if request.get('task_phase') in {'publishing','publication_reconciling'}:
            raise PermissionError('Publication must settle before PM dispatch')
        open_group=await (await connection.execute("""SELECT 1 FROM assistant.messages WHERE (run_id=%s OR content_json->>'task_id'=%s)
            AND message_type='question_group' AND content_json->'group'->>'interaction_scope'='pm_business'
            AND content_json->'group'->>'status'='open' AND (content_json->'group'->>'blocking')::boolean LIMIT 1""",
            (task['run_id'],str(task['run_id'])))).fetchone()
        if open_group: raise PermissionError('A required business question remains unanswered')
        if request.get('schedule_actions'):
            from assistant_kernel.services.schedule_operations import gates
            return await gates(connection,self.settings,task,mode)
        bindings={}
        requirement_ref=accepted.get('task_contract')
        requirement=self.documents.read(task['project_id'],requirement_ref)['payload'] if requirement_ref else None
        research_only=bool(requirement and requirement['delivery_obligations']==['research']
            and not accepted.get('solution_design') and not accepted.get('implementation_report'))
        for kind,approval_key in [('task_contract','requirements_approval'),('solution_design','design_approval')]:
            applicable=(kind=='task_contract' and mode in {'design','build','audit','operate'} or
                kind=='solution_design' and mode in {'build','audit','operate'} and not (research_only and mode=='audit'))
            if not applicable: continue
            ref=accepted.get(kind);approval=request.get(approval_key) or {}
            approval_source_id=approval.get('message_id') or approval.get('source_message_id')
            if (pending_approval_refs or {}).get(kind)==ref and ref and not approval_source_id:
                document=self.documents.read(task['project_id'],ref)
                if document.get('task_id')!=str(task['run_id']):raise PermissionError('Approval subject belongs to another Task')
                bindings[kind]={'ref':ref,'approval_message_id':None,'pending_specific_confirmation':True}
                continue
            if not ref or approval.get('approved_document_sha256')!=ref.get('sha256') or not approval_source_id:
                raise PermissionError('The effective '+kind+' lacks its concrete user approval')
            document=self.documents.read(task['project_id'],ref)
            if document.get('task_id')!=str(task['run_id']): raise PermissionError('Approved document belongs to another Task')
            source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
                (approval_source_id,))).fetchone()
            if not source or source['project_id']!=task['project_id'] or source['direction']!='inbound' or not source['user_id']:
                raise PermissionError('Approval has no real same-Project user source')
            from assistant_kernel.repositories.core import approval_decision
            specific=any(item.get('decision')=='approve' and item.get('document_ref')==ref
                for item in source['content_json'].get('document_approvals') or [])
            if (source['content_json'].get('context_revoked') or source['content_json'].get('source_revoked')
                    or not specific and approval_decision(source['content_json'])!='approve'):
                raise PermissionError('Approval source was not accepted by the approval controller')
            bindings[kind]={'ref':ref,'approval_message_id':str(source['message_id'])}
        for ref in required_child_refs:
            document=self.documents.read(task['project_id'],ref)
            native=document.get('schema_version')=='delegation-record.v1'
            body=document.get('payload') or {}
            complete=body.get('actual_status')=='completed' and body.get('result_ref') and body.get('raw_trace_ref') if native else document.get('status') in {'completed','accepted'}
            if document.get('task_id')!=str(task['run_id']) or not complete:
                raise PermissionError('Required child result is missing, stale or belongs to another Task')
            if native:
                from assistant_kernel.services.v12_evidence import V12Evidence
                await V12Evidence(self.settings,self.repository).verify(task['project_id'],body,task=task)
        design_ref=accepted.get('solution_design')
        design=self.documents.read(task['project_id'],design_ref)['payload'] if design_ref else None
        needs_bundle=bool(design and design.get('publish_targets'))
        if mode=='audit' and research_only:
            artifacts=[ref for ref in task['artifact_refs'] or [] if isinstance(ref,dict)
                and ref.get('schema_version')=='resource-ref.v1' and ref.get('document_schema') not in {'task-contract.v1','turn-record.v1'}]
            if not artifacts:raise PermissionError('Research Review requires actual pinned research deliverables')
            from assistant_kernel.services.v12_evidence import V12Evidence
            await V12Evidence(self.settings,self.repository).verify(task['project_id'],artifacts,task=task)
            bindings['research_delivery_validation']=self.write_blob(task['project_id'],str(task['run_id']),'research-validation',{
                'task_contract_ref':requirement_ref,'actual_deliverable_refs':artifacts,'scope':'structure',
                'business_acceptance':'pending_independent_review','publication':'not_applicable'})
        elif mode=='audit' and not (request.get('validation_artifact') and request.get('validation_gate_status')=='pass'
                and (request.get('sealed_candidate_bundle') or not needs_bundle)):
            raise PermissionError('Independent Review requires the sealed candidate and actual Kernel validation')
        if mode=='operate' and request.get('publication_state') not in {'published','not_applicable'}:
            raise PermissionError('Operation requires settled applicable publication')
        return bindings

    async def prepare_locked(self,connection,*,task,proposal_ref,runtime_package_ref,input_head_ref,
            capability_ref,required_child_refs=(),session_request=None):
        """Trusted Supervisor settlement calls after locking package head then Task."""
        project=task['project_id'];task_id=str(task['run_id']);request=task['request_json'] or {}
        proposal=self.documents.read(project,proposal_ref)
        if (proposal.get('schema_version')!='prompt-proposal.v1' or proposal.get('task_id')!=task_id
                or proposal.get('envelope_version')!='doc-envelope.v3'):
            raise PermissionError('Prompt proposal is not the Kernel-written scoped contract')
        payload=proposal['payload'];contracts().validate('PromptProposal',payload)
        if not required_child_refs:
            required_child_refs=[ref for ref in payload['child_result_refs']
                if self.documents.read(project,ref).get('schema_version')=='delegation-record.v1'
                and self.documents.read(project,ref)['payload']['required']]
        if payload['owner_ref']['id']!=task_id or payload['basis_state_revision']!=int(request.get('state_revision') or 0) or payload['basis_context_epoch']!=int(request.get('supervisor_context_epoch') or 0):
            raise PermissionError('Prompt proposal belongs to an old Task head')
        if payload['clarity']!='ready_for_confirmation' or payload['blocking_question_ids']:
            raise PermissionError('Prompt proposal still needs clarification')
        if payload['capability_profile_ref']!=capability_ref: raise PermissionError('Prompt capability binding differs')
        self.read_blob(project,capability_ref);self.read_blob(project,payload['file_authority_ref'])
        head=await self.input_head_locked(connection,task)
        if head!=input_head_ref: raise PermissionError('Prompt preparation input head is stale')
        originals=self.read_blob(project,head)['inputs'];known={x['message_id']:x for x in originals}
        if not payload['user_message_refs'] or any(x['id'] not in known or known[x['id']]['revoked'] for x in payload['user_message_refs']):
            raise PermissionError('Prompt proposal refers to missing or revoked user input')
        users={known[x['id']]['user_id'] for x in payload['user_message_refs']}
        if len(users)!=1 or None in users or '' in users: raise PermissionError('Prompt confirmation needs one identified original user')
        package=self.packages.read_package(project,runtime_package_ref)
        published=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',
            (self.packages.key(project),))).fetchone()
        fixed=(task['executor_json'] or {}).get('task_runtime_package_ref')
        if runtime_package_ref!=fixed and (not published or published['value']['package_ref']!=runtime_package_ref):
            raise PermissionError('Prompt package is neither fixed to the Task nor the accepted publication head')
        mode=payload['draft']['proposed_work_mode'];selector=str(task['agent_key'])+'/'+mode
        if payload['draft'].get('schedule_actions')!=request.get('schedule_actions'):
            raise PermissionError('Daily schedule scope differs from its proposed Task')
        config_ref=package['configurations'].get(selector)
        if not config_ref: raise PermissionError('Accepted package lacks this required phase')
        actual_capability=self.read_blob(project,capability_ref)
        if actual_capability.get('config_ref'):
            if actual_capability.get('base_config_ref',actual_capability['config_ref'])!=config_ref:
                raise PermissionError('Actual capability configuration has another published phase basis')
            config_ref=actual_capability['config_ref']
        config=self.packages.loader.load_snapshot(project,config_ref['uri'],config_ref['sha256'])
        if config.foundation_revision!='1.2': raise PermissionError('Prompt compiler cannot mix new and legacy actor contracts')
        pending_approvals={}
        pending_kind=request.get('approval_kind')
        if not pending_kind and mode in {'design','build'}:
            # A complete submitted report still needs concrete user approval,
            # even when the PM called its own Turn "completed".
            pending_kind='requirements' if mode=='design' else 'design'
        if (pending_kind,mode) in {('requirements','design'),('design','build')}:
            kind='task_contract' if pending_kind=='requirements' else 'solution_design'
            ref=(request.get('accepted_documents') or {}).get(kind)
            key='requirements_approval' if kind=='task_contract' else 'design_approval'
            if ref and not request.get(key):pending_approvals[kind]=ref
        approvals=await self.gates_locked(connection,task,mode,required_child_refs=required_child_refs,pending_approval_refs=pending_approvals)
        if any(x not in payload['child_result_refs'] for x in required_child_refs):
            raise PermissionError('Prompt package omits required child results')
        trigger=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
            (task['trigger_message_id'],))).fetchone()
        session_scope={'agent_key':task['agent_key'],'channel':trigger['channel'],
            'chat_id':trigger['chat_id']+':task:'+str(request.get('task_root_run_id') or task_id)}
        previous=await (await connection.execute("""SELECT session_id FROM assistant.sessions WHERE project_id=%s
            AND agent_key=%s AND channel=%s AND chat_id=%s AND status IN ('active','rotation_required')
            ORDER BY updated_at DESC LIMIT 1""",(project,*session_scope.values()))).fetchone()
        old_mode=request.get('current_stage') or 'requirements'
        reason='phase_changed' if mode!=old_mode else 'independent_review' if mode=='audit' else 'new_task' if not previous else 'none'
        boundary={'task_ref':payload['owner_ref'],'from_mode':old_mode,'target_mode':mode,
            'expected_state_revision':int(request.get('state_revision') or 0),
            'expected_context_epoch':int(request.get('context_epoch') or 0),
            'action':'keep' if reason=='none' else 'fresh','reason':reason,'source_refs':[proposal_ref],
            'mandatory_child_refs':list(required_child_refs),'context_policy_ref':config_ref}
        contracts().validate('SessionRequest',boundary)
        if session_request:
            contracts().validate('SessionRequest',session_request)
            for key in ('from_mode','target_mode','expected_state_revision','expected_context_epoch','action','reason'):
                if session_request[key]!=boundary[key]:raise PermissionError('Supervisor SessionRequest differs from the actual Task/session boundary')
            if session_request['task_ref']['id']!=task_id or session_request['mandatory_child_refs']!=list(required_child_refs):
                raise PermissionError('Supervisor SessionRequest has another owner or required results')
        frozen={'schema_version':'kernel-confirmed-turn-package.v1','protocol_revision':'1.2','project_id':project,
            'task_id':task_id,'prompt_ref':proposal_ref,'draft_sha256':hashlib.sha256(canonical_json(payload['draft'])).hexdigest(),
            'input_head_ref':head,'runtime_package_ref':runtime_package_ref,'config_ref':config_ref,
            'capability_ref':capability_ref,'file_authority_ref':payload['file_authority_ref'],
            'required_child_result_refs':list(required_child_refs),'approvals':approvals,'work_mode':mode,
            'session_request':boundary,'session_scope':session_scope,
            'turn_number':int(request.get('task_turn_count') or 0)+1,
            'state_revision':payload['basis_state_revision'],'context_epoch':payload['basis_context_epoch']}
        if request.get('task_scope_proposal'):frozen['task_scope_proposal']=request['task_scope_proposal']
        if pending_approvals:frozen['approval_subjects']=pending_approvals
        frozen_ref=self.write_blob(project,task_id,'confirmed-turn-package',frozen)
        source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
            (payload['user_message_refs'][-1]['id'],))).fetchone()
        draft_text=json.dumps(payload['draft'],ensure_ascii=False,indent=2)
        if pending_approvals:
            documents={name:{'ref':ref,'payload':self.documents.read(project,ref)['payload']} for name,ref in pending_approvals.items()}
            draft_text='确认将同时批准下列固定文档和后续执行 Prompt，分别记录批准。\n\n'+json.dumps(documents,ensure_ascii=False,indent=2)+'\n\n'+draft_text
            if task.get('waiting_question_message_id'):
                await connection.execute("UPDATE assistant.messages SET content_json=content_json || %s,delivery_status=CASE WHEN delivery_status='pending' THEN 'cancelled' ELSE delivery_status END WHERE message_id=%s",
                    (Jsonb({'action_status':'superseded','superseded_by_package_ref':frozen_ref}),task['waiting_question_message_id']))
        if request.get('task_scope_proposal') and not request['task_scope_proposal'].get('confirmed'):
            active=request['task_scope_proposal']['expected_active_task_id']
            if active and request['task_scope_proposal']['disposition']=='suspend_current_then_start':
                notice='确认后将在当前回合结算且可安全切换时挂起任务 '+str(active)+'，优先执行此新任务；原问题和回答保留，新任务结束后恢复原任务。'
            else:notice='确认后将此新任务加入待办，当前任务结束后启动。当前任务：'+str(active) if active else '确认后创建并启动此新任务。'
            draft_text=notice+'\n\n'+draft_text
        prior_display=request.get('prompt_display_message_id')
        if prior_display and request.get('prepared_turn_package_ref')!=frozen_ref:
            await connection.execute("""UPDATE assistant.messages SET content_json=content_json || %s,
                delivery_status=CASE WHEN delivery_status='pending' THEN 'cancelled' ELSE delivery_status END,
                updated_at=now() WHERE message_id=%s AND project_id=%s""",
                (Jsonb({'action_status':'superseded','superseded_by_package_ref':frozen_ref}),prior_display,project))
        display=await self.repository._create_outbound_locked(connection,project_id=project,
            session_id=str(task['session_id']) if task.get('session_id') else None,run_id=task_id,
            channel=source['channel'],chat_id=source['chat_id'],user_id=next(iter(users)),message_type='kernel_notice',
            content={'interaction_kind':'prompt_confirmation','context_visibility':'structured','action_status':'open',
                'package_ref':frozen_ref,'prompt_ref':proposal_ref,'input_head_ref':head,
                'text':'请确认这份具体执行内容。正式答案不会自动批准下一轮。\n\n'+draft_text,
                'document_markdown':'# 待确认执行内容\n\n```json\n'+draft_text+'\n```\n\n执行包 SHA: '+frozen_ref['sha256'],
                'actions':[{'id':'confirm','label':'确认并执行'},{'id':'revise','label':'需要修改'},{'id':'cancel','label':'取消本包'}]},
            reply_to_message_id=source['message_id'],idempotency_key='prompt-display:'+task_id+':'+frozen_ref['sha256'])
        display_ref=resource_ref(kind='message',identity=str(display['message_id']),
            uri='db://assistant/messages/'+str(display['message_id']),purpose='full exact Prompt package display')
        preparation={'owner_ref':payload['owner_ref'],'prompt_ref':proposal_ref,'capability_ref':capability_ref,
            'input_head_ref':head,'confirmation_state':'awaiting_confirmation','display_ref':display_ref,'missing_refs':[]}
        contracts().validate('PromptPreparation',preparation)
        await connection.execute("UPDATE assistant.runs SET status='waiting_user',waiting_question_message_id=CASE WHEN %s THEN NULL ELSE waiting_question_message_id END,request_json=request_json || %s WHERE run_id=%s",
            (bool(pending_approvals),Jsonb({'prompt_preparation':preparation,'prepared_turn_package_ref':frozen_ref,
                'prompt_display_message_id':str(display['message_id'])}),task['run_id']))
        faithful=await self.faithful_instruction_locked(connection,task=task,proposal=payload,package=frozen,source=source)
        if faithful:
            ready=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task['run_id'],))).fetchone()
            confirmation=await self.bind_confirmed_locked(connection,task=ready,package=frozen,package_ref=frozen_ref,event=source,approvals=approvals)
            receipt={'prompt_ref':proposal_ref,'user_event_ref':faithful,'input_head_ref':head,'status':'accepted','confirmation_ref':confirmation}
            contracts().validate('ConfirmationReceipt',receipt)
            await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                (Jsonb({'confirmation_receipt':receipt,'direct_instruction_binding':{'package_ref':frozen_ref,'prompt_ref':proposal_ref}}),source['message_id']))
            await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                (Jsonb({'action_status':'confirmed','actions':[],'text':'已按您的明确原指令安排本轮，执行内容如下。\n\n'+draft_text,
                    'document_markdown':'# 本轮执行内容\n\n```json\n'+draft_text+'\n```\n\n执行包 SHA: '+frozen_ref['sha256'],
                    'confirmation_source_ref':faithful}),display['message_id']))
            preparation={**preparation,'confirmation_state':'confirmed'}
        return preparation

    async def faithful_instruction_locked(self,connection,*,task,proposal,package,source):
        """Bind a literal complete instruction, never a model's own safe/faithful claim."""
        body=source['content_json'] or {};draft=proposal['draft']
        if (source['direction']!='inbound' or source['message_type'] not in {'text','command'} or not source['user_id']
                or body.get('direct_instruction_binding') or proposal['scope_change']!='none' or package.get('approval_subjects')):
            return None
        fixed=(task['executor_json'] or {}).get('task_runtime_package_ref')
        if fixed and fixed!=package['runtime_package_ref']:return None # A method change still needs its concrete display.
        owner=await (await connection.execute('SELECT user_id FROM assistant.messages WHERE message_id=%s',(task['trigger_message_id'],))).fetchone()
        if not owner or owner['user_id']!=source['user_id']:return None
        previous=(task['request_json'] or {}).get('accepted_documents',{}).get('last_turn')
        if previous:
            from datetime import datetime
            at=self.documents.read(task['project_id'],previous)['created_at']
            if source['created_at']<=datetime.fromisoformat(at.replace('Z','+00:00')):return None
        explicit=body.get('execution_instruction') or {}
        literal_json=explicit.get('action')=='execute' and explicit.get('draft')==draft
        text=str(body.get('text') or '').strip()
        literal_text=(text.startswith(('请','帮我','执行','完成','继续','生成','制作','Please ','please ','Execute ','execute '))
            and draft['objective']==text and len(draft['work_items'])==1 and draft['work_items'][0]['description']==text
            and draft['authorization_boundary']['permitted_work']==[text]
            and not draft['authorization_boundary']['required_approval_subjects']
            and all(item['text'] in text for item in draft['constraints'])
            and all(item['description'] in text for item in draft['deliverables'])
            and all(item['criterion'] in text and not item['requires_user_feedback'] for item in draft['acceptance'])
            and not draft['background'])
        if not literal_json and not literal_text:return None
        return resource_ref(kind='message',identity=str(source['message_id']),uri='db://assistant/messages/'+str(source['message_id']),
            purpose='literal complete original instruction for this specific Turn')


    async def bind_confirmed_locked(self,connection,*,task,package,package_ref,event,approvals,approval_patch=None):
        project_id=task['project_id'];request=deepcopy(task['request_json'] or {});approval_patch=approval_patch or {}
        subjects=package.get('approval_subjects') or {}
        event_ref=resource_ref(kind='message',identity=str(event['message_id']),uri='db://assistant/messages/'+str(event['message_id']),purpose='actual specific instruction/confirmation')
        current=(task['executor_json'] or {}).get('task_runtime_package_ref')
        if current!=package['runtime_package_ref']:
            await self.packages.pin_task_package_locked(connection,project_id,str(task['run_id']),
                package['runtime_package_ref'],expected_previous_ref=current)
        confirmation_ref=resource_ref(kind='receipt',identity='prompt-confirmation:'+str(event['message_id']),
            uri='db://assistant/messages/'+str(event['message_id']),sha256=package_ref['sha256'],purpose='accepted exact Prompt package')
        next_epoch=await self.repository._advance_supervisor_context_epoch_locked(connection,task=task,
            reason='prompt_confirmation',source_id=str(event['message_id']))
        mode=package['work_mode'];old_mode=request.get('current_stage') or 'requirements'
        patch={'protocol_revision':'1.2','task_protocol':'single-agent.v2','phase_protocol':'five-stage.v1',
            'design_required':True,'work_mode':mode,'current_stage':mode,'execution_plan_variant':mode,'audit_mode':mode=='audit',
            'confirmed_turn_package_ref':package_ref,'prompt_confirmation_ref':confirmation_ref,
            'prompt_confirmation_message_id':str(event['message_id']),
            'prompt_preparation':{**request['prompt_preparation'],'confirmation_state':'confirmed'},
            'state_revision':int(request.get('state_revision') or 0)+1,
            'confirmed_context_epoch':next_epoch,
            'supervisor_action_status':'confirmed','pending_supervision_turn':None,'task_phase':'working'}
        patch.update(approval_patch)
        if subjects:patch.update(approval_kind=None,confirmed_package_approval_bindings=approvals)
        patch['accepted_documents']={**(request.get('accepted_documents') or {}),'confirmed_prompt':package['prompt_ref']}
        if approval_patch:
            await connection.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
                (Jsonb(approval_patch),task['run_id']))
        from assistant_kernel.services.session_boundaries import SessionBoundaryService
        async def transition_gate(conn,current,body):
            admitted=await self.gates_locked(conn,current,mode,required_child_refs=package['required_child_result_refs'])
            if admitted!=approvals:raise PermissionError('Session boundary approval/result basis changed')
            self.packages.loader.load_snapshot(project_id,body['context_policy_ref']['uri'],body['context_policy_ref']['sha256'])
        boundary=await SessionBoundaryService(self.settings).commit_locked(connection,project_id,package['session_request'],
            session_scope=package['session_scope'],validate_transition=transition_gate)
        patch.update(session_boundary_receipt=boundary,session_request=package['session_request'],
            context_epoch=boundary['context_epoch'],state_revision=boundary['state_revision'],force_session_rotation=None)
        executor=deepcopy(task['executor_json'] or {})
        for key in ('resolved_plan_uri','resolved_plan_sha256','prompt_bundle_uri','prompt_bundle_sha256',
                'native_permission_profile','native_permission_profile_sha256','resolved_config_snapshot_ref',
                'public_background_sha256'):
            executor.pop(key,None)
        capability=self.read_blob(project_id,package['capability_ref'])
        if capability.get('model_profile'):executor['model_profile']=capability['model_profile']
        executor.update({'task_runtime_package_ref':package['runtime_package_ref'],
            'config_snapshot_uri':package['config_ref']['uri'],'config_bundle_hash':package['config_ref']['sha256']})
        await connection.execute("""UPDATE assistant.runs SET status='queued',available_at=now(),
            request_json=request_json || %s,executor_json=%s,max_attempts=attempt+4,updated_at=now()
            WHERE run_id=%s""",(Jsonb(patch),Jsonb(executor),task['run_id']))
        if request.get('task_scope_proposal') and not request['task_scope_proposal'].get('confirmed'):
            from assistant_kernel.services.task_proposals_v12 import TaskProposalsV12
            await TaskProposalsV12(self.settings,self.repository).confirm_locked(connection,task=task,event_ref=event_ref)
        from assistant_kernel.services.document_projections_v12 import DocumentProjectionsV12
        updated=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s',(task['run_id'],))).fetchone()
        await DocumentProjectionsV12(self.settings,self.repository).record_locked(connection,project=project_id,owner=updated,
            refs=[package['prompt_ref']],source_refs=[event_ref])
        return confirmation_ref

    async def record_confirmation(self,*,display_message_id,project_id,channel,chat_id,user_id,
            external_event_id,action,expected_package_sha256):
        from assistant_kernel.services.question_groups import QuestionGroupService
        event=await QuestionGroupService(self.settings,self.repository).received_event(project_id=project_id,
            channel=channel,chat_id=chat_id,user_id=user_id,external_event_id=external_event_id,
            message_id=display_message_id,request={'interaction_kind':'prompt_confirmation','action':action,
                'package_sha256':expected_package_sha256})
        error=None;receipt=None
        async with self.repository.database.connection() as connection:
            # Publication head precedes Task; every compiler uses this lock order.
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(self.packages.key(project_id),))
            from assistant_kernel.services.task_queue import ProjectTaskQueue
            await ProjectTaskQueue._lock(connection,project_id)
            event=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',(event['message_id'],))).fetchone()
            verdict=event['content_json'].get('acceptance')
            if verdict and verdict['status']=='rejected': raise ValueError(verdict['reason'])
            previous=event['content_json'].get('confirmation_receipt')
            if previous:
                return {**previous,'status':'duplicate'}
            await connection.execute('SAVEPOINT prompt_confirmation')
            try:
                display=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(display_message_id,))).fetchone()
                if not display or (display['project_id'],display['channel'],display['chat_id'],display['user_id'])!=(project_id,channel,chat_id,user_id):
                    raise PermissionError('Prompt display belongs to another user or route')
                body=display['content_json'];package_ref=body.get('package_ref')
                if (body.get('interaction_kind')!='prompt_confirmation' or not package_ref
                        or package_ref['sha256']!=expected_package_sha256):
                    raise ValueError('Prompt confirmation refers to a different displayed package')
                package=self.read_blob(project_id,package_ref)
                task=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(package['task_id'],))).fetchone()
                if not task or task['project_id']!=project_id: raise PermissionError('Prompt Task owner unavailable')
                request=deepcopy(task['request_json'] or {})
                if (request.get('prepared_turn_package_ref')!=package_ref or request.get('prompt_display_message_id')!=str(display_message_id)
                        or body.get('action_status')!='open'):
                    raise ValueError('Prompt display was superseded or already closed')
                if display['delivery_status']!='sent': raise PermissionError('Full Prompt display has not been delivered')
                head=await self.input_head_locked(connection,task)
                if head!=package['input_head_ref']: raise PermissionError('User input or Task state changed after Prompt display')
                if action not in {'confirm','revise','cancel'}: raise ValueError('Unknown Prompt confirmation action')
                owner=await (await connection.execute('SELECT user_id FROM assistant.messages WHERE message_id=%s',(task['trigger_message_id'],))).fetchone()
                if not owner or owner['user_id']!=user_id: raise PermissionError('Only the Task original user may confirm its package')
                event_ref=resource_ref(kind='message',identity=str(event['message_id']),
                    uri='db://assistant/messages/'+str(event['message_id']),purpose='actual Prompt confirmation input')
                confirmation_ref=None
                if action=='confirm':
                    subjects=package.get('approval_subjects') or {}
                    approval_patch={}
                    if subjects:
                        pending=await self.gates_locked(connection,task,package['work_mode'],
                            required_child_refs=package['required_child_result_refs'],pending_approval_refs=subjects)
                        if pending!=package['approvals']:raise PermissionError('Document approval subjects changed after display')
                        decisions=[]
                        for kind,ref in subjects.items():
                            if (request.get('accepted_documents') or {}).get(kind)!=ref:
                                raise PermissionError('Combined confirmation no longer refers to the effective document')
                            key={'task_contract':'requirements_approval','solution_design':'design_approval'}.get(kind)
                            if not key:raise PermissionError('Unsupported combined approval subject')
                            approval_patch[key]={'message_id':str(event['message_id']),'approved_document_sha256':ref['sha256'],
                                'approved_document_ref':ref,'package_ref':package_ref,'origin':'specific_combined_confirmation'}
                            decisions.append({'decision':'approve','document_ref':ref,'package_ref':package_ref})
                        await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                            (Jsonb({'document_approvals':decisions}),event['message_id']))
                        request.update(approval_patch);task={**task,'request_json':request}
                    approvals=await self.gates_locked(connection,task,package['work_mode'],
                        required_child_refs=package['required_child_result_refs'])
                    if not subjects and approvals!=package['approvals']: raise PermissionError('Approval basis changed after Prompt display')
                    self.read_blob(project_id,package['capability_ref']);self.read_blob(project_id,package['file_authority_ref'])
                    confirmation_ref=await self.bind_confirmed_locked(connection,task=task,package=package,package_ref=package_ref,
                        event=event,approvals=approvals,approval_patch=approval_patch)
                else:
                    cancelling_new=bool(action=='cancel' and request.get('task_scope_proposal') and not request['task_scope_proposal']['confirmed'])
                    await connection.execute("UPDATE assistant.runs SET status=%s,request_json=request_json || %s,updated_at=now() WHERE run_id=%s",
                        ('cancelled' if cancelling_new else 'waiting_supervisor',Jsonb({'prompt_preparation':None,'prepared_turn_package_ref':None,
                            'prompt_revision_input_id':str(event['message_id']),**({'task_activation':'closed'} if cancelling_new else {})}),task['run_id']))
                    if cancelling_new:
                        await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                            (Jsonb({'work_status':'cancelled','cancellation_ref':event_ref}),request['work_request_message_id']))
                receipt={'prompt_ref':package['prompt_ref'],'user_event_ref':event_ref,'input_head_ref':head,
                    'status':'accepted','confirmation_ref':confirmation_ref}
                if action!='confirm':
                    # This is an accepted cancellation/revision input, not an accepted confirmation.
                    receipt['status']='rejected'
                contracts().validate('ConfirmationReceipt',receipt)
                await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                    (Jsonb({'action_status':'confirmed' if action=='confirm' else action}),display_message_id))
                if action=='revise':
                    root=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s',(task['run_id'],))).fetchone()
                    await self.repository._queue_supervision_locked(connection,root,str(event['message_id']),reason='prompt_revision')
            except (ValueError,PermissionError) as exc:
                error=exc;await connection.execute('ROLLBACK TO SAVEPOINT prompt_confirmation')
            await connection.execute('RELEASE SAVEPOINT prompt_confirmation')
            verdict={'status':'rejected','reason':str(error)} if error else {'status':'accepted','action':action}
            await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                (Jsonb({'acceptance':verdict,**({'confirmation_receipt':receipt} if receipt else {})}),event['message_id']))
            await connection.commit()
        if error: raise error
        return receipt

    async def validate_dispatch_locked(self,connection,task):
        request=task['request_json'] or {};ref=request.get('confirmed_turn_package_ref')
        if task['status'] not in {'queued','running'} or task.get('cancel_requested_at') or request.get('task_activation','active')!='active':
            raise PermissionError('Stopped, suspended or unsettled PM work cannot start a model invocation')
        if request.get('protocol_revision')!='1.2' or not ref or not request.get('prompt_confirmation_ref'):
            raise PermissionError('New-protocol PM Turn has no confirmed immutable package')
        package=self.read_blob(task['project_id'],ref)
        if package['task_id']!=str(task['run_id']) or package['work_mode']!=request.get('current_stage'):
            raise PermissionError('PM dispatch package owner or phase differs')
        if package['turn_number']!=int(request.get('task_turn_count') or 0)+1:
            raise PermissionError('A settled PM business Turn cannot consume the previous confirmation again')
        if int(request.get('confirmed_context_epoch',-1))!=int(request.get('supervisor_context_epoch') or 0):
            raise PermissionError('PM dispatch was overtaken by newer semantic input')
        current_head=self.read_blob(task['project_id'],await self.input_head_locked(connection,task))
        prepared_head=self.read_blob(task['project_id'],package['input_head_ref'])
        if current_head['inputs']!=prepared_head['inputs']:
            raise PermissionError('PM dispatch source inputs changed after confirmation')
        confirmation=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
            (request['prompt_confirmation_message_id'],))).fetchone()
        receipt=(confirmation or {}).get('content_json',{}).get('confirmation_receipt')
        if not receipt or receipt['status']!='accepted' or receipt['prompt_ref']!=package['prompt_ref']:
            raise PermissionError('PM dispatch has no accepted exact-Prompt confirmation receipt')
        if (task['executor_json'] or {}).get('task_runtime_package_ref')!=package['runtime_package_ref']:
            raise PermissionError('PM dispatch actor package changed after confirmation')
        executor=task['executor_json'] or {}
        if (executor.get('config_snapshot_uri'),executor.get('config_bundle_hash'))!=(package['config_ref']['uri'],package['config_ref']['sha256']):
            raise PermissionError('PM dispatch Role/Skill/profile snapshot differs from the confirmed package')
        approvals=await self.gates_locked(connection,{**task,'status':'queued'},package['work_mode'],
            required_child_refs=package['required_child_result_refs'])
        expected_approvals=request.get('confirmed_package_approval_bindings') if package.get('approval_subjects') else package['approvals']
        if approvals!=expected_approvals:raise PermissionError('PM approval basis changed after confirmation')
        proposal=self.documents.read(task['project_id'],package['prompt_ref'])
        if hashlib.sha256(canonical_json(proposal['payload']['draft'])).hexdigest()!=package['draft_sha256']:
            raise PermissionError('PM business Prompt bytes differ from the confirmed draft')
        return package,proposal['payload']['draft']
