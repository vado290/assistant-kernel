"""Typed PM business Turn settlement over the existing Run, Messages and documents."""
from copy import deepcopy
import json

from psycopg.types.json import Jsonb

from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.services.question_groups import QuestionGroupService


REPORTS={'requirements':('task_contract','task-contract.v1'), 'design':('solution_design','solution-design.v1'),
    'build':('implementation_report','implementation-report.v1'),'audit':('review_report','review-report.v1'),
    'operate':('operation_report','operation-report.v1')}


def bind_review_inputs(report,accepted):
    """Kernel owns the fixed baselines actually supplied to this Audit invocation."""
    from assistant_kernel.services.document_runtime import same_resource_ref
    result=deepcopy(report)
    for kind in ('task_contract','solution_design','implementation_report'):
        ref=accepted.get(kind)
        if not ref:continue
        declared=[item for item in result['fixed_input_refs'] if item.get('document_schema')==ref.get('document_schema')]
        if any(not same_resource_ref(item,ref) for item in declared):
            raise PermissionError('Review explicitly names a different '+kind+' baseline')
        if not declared:result['fixed_input_refs'].append(deepcopy(ref))
    return result


class PMTurnsV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)
        self.prompts=PromptProtocol(settings,repository)

    def verify_ref(self,project,ref):
        contracts().validate('Ref',ref)
        if ref['kind']=='document':
            document=self.documents.read(project,ref)
            if document['project_id']!=project:raise PermissionError('PM result document belongs to another Project')
        elif ref.get('sha256') and str(ref.get('uri') or '').startswith('vault://'):
            # Generic pinned files can contain traces or other text formats.
            import hashlib
            prefix='vault://'+project+'/'
            if not ref['uri'].startswith(prefix):raise PermissionError('PM result file belongs to another Project')
            target=self.documents.runtime.resolve_vault_file(project,ref['uri'][len(prefix):])
            if hashlib.sha256(target.read_bytes()).hexdigest()!=ref['sha256']:raise ValueError('PM result source hash differs')

    async def settle(self,*,run,worker_instance_id,public_result,usage_snapshot,file_effects=None,cancel_event=None):
        project=run['project_id'];task_id=str(run['run_id']);request=run['request_json'] or {};phase=request['current_stage']
        contracts().validate_agent_result(public_result,expected_phase=phase)
        package=self.prompts.read_blob(project,request['confirmed_turn_package_ref'])
        prepared_head=self.prompts.read_blob(project,package['input_head_ref'])
        execution_head=self.prompts.write_blob(project,task_id,'prompt-input-head',{
            **prepared_head,'state_revision':int(request.get('state_revision') or 0),
            'context_epoch':int(request.get('supervisor_context_epoch') or 0)})
        raw_ref=self.prompts.write_blob(project,task_id,'pm-result',public_result)
        refs=public_result['artifact_refs']+public_result['evidence_refs']+public_result['delegation_results']
        for ref in refs:self.verify_ref(project,ref)
        payload=public_result['payload'];kind,schema=REPORTS[phase];report_payload=payload.get('report_payload')
        if payload.get('report_ref'):
            from_ref=self.documents.read(project,payload['report_ref'])
            if from_ref.get('task_id')!=task_id or from_ref.get('schema_version')!=schema:
                raise PermissionError('PM phase report belongs to another Task or schema')
            if report_payload is not None and report_payload!=from_ref['payload']:raise ValueError('PM report has two conflicting sources')
            report_payload=from_ref['payload']
        from assistant_kernel.services.v12_evidence import V12Evidence
        await V12Evidence(self.settings,self.repository).verify(project,public_result,task=run)
        if report_payload is not None:await V12Evidence(self.settings,self.repository).verify(project,report_payload,task=run)
        accepted=deepcopy(request.get('accepted_documents') or {});report_ref=None
        if report_payload is not None:
            if phase=='audit':report_payload=bind_review_inputs(report_payload,accepted)
            contracts().validate(contracts().document_definition(schema),report_payload)
            from assistant_kernel.services.document_runtime import same_resource_ref
            if phase=='design' and not same_resource_ref(report_payload['task_contract_ref'],accepted.get('task_contract')):
                raise PermissionError('Design uses another requirements revision')
            if phase=='build' and (not same_resource_ref(report_payload['task_contract_ref'],accepted.get('task_contract')) or
                    not same_resource_ref(report_payload['solution_design_ref'],accepted.get('solution_design'))):
                raise PermissionError('Implementation uses another requirements/design baseline')
            prior_validation=(run.get('executor_json') or {}).get('validation_process') or {}
            prior_report=prior_validation.get('implementation_report_ref') if phase=='build' and prior_validation.get('original_pm_result_ref')==raw_ref else None
            if prior_report:
                prior_document=self.documents.read(project,prior_report)
                if prior_document.get('task_id')!=task_id or prior_document['payload']!=report_payload:
                    raise PermissionError('Interrupted validation report differs from the preserved PM result')
                report_ref=prior_report
            else:report_ref=self.documents.write_scoped_revision(project_id=project,scope='task',object_id=kind.replace('_','-'),
                task_id=task_id,run_id=task_id,document_kind=kind.replace('_','-'),document_schema=schema,payload=report_payload,
                status='submitted',actor_type='pm',actor_id=run['agent_key'],reason='Typed '+phase+' report; model assertions remain unverified'+
                    ('; Kernel binds the actual fixed Audit inputs, original model output retained separately' if phase=='audit' else ''),
                source_refs=[package['prompt_ref'],raw_ref],supersedes_ref=accepted.get(kind))
            accepted[kind]=report_ref
        outcome=public_result['outcome']
        if outcome in {'needs_approval','ready_for_validation','ready_for_release','completed'} and not report_ref:
            raise ValueError('This phase outcome requires its complete typed report')
        validation_result=None;review_patch={}
        if outcome=='ready_for_validation' and phase not in {'build','audit'}:
            raise ValueError('This phase has no validation result to admit')
        if outcome=='ready_for_validation' and phase=='build':
            from assistant_kernel.services.phase_control_v12 import PhaseControlV12
            turn_evidence=self.prompts.write_blob(project,task_id,'pm-turn-evidence',{'usage_snapshot':usage_snapshot,'file_effects':file_effects})
            virtual={**run,'request_json':{**request,'accepted_documents':accepted,'pending_pm_result_ref':raw_ref,
                'pending_turn_evidence_ref':turn_evidence}}
            validation_result=await PhaseControlV12(self.settings,self.repository).validate_build(task=virtual,
                expected_input_head=execution_head,running_claim={'claim_token':str(run['claim_token']),'worker_instance_id':worker_instance_id},cancel_event=cancel_event)
        # Audit validates a Review report; it never launches Build validation.
        # Kernel acceptance depends on the fixed final criteria and provenance,
        # not on whether the model calls its finished report validation/release.
        if phase=='audit' and outcome in {'completed','ready_for_validation','ready_for_release'}:
            from assistant_kernel.services.phase_control_v12 import PhaseControlV12
            phases=PhaseControlV12(self.settings,self.repository)
            execution=await phases.review_execution(run,usage_snapshot.get('invocation_id'))
            research_validation=package['approvals'].get('research_delivery_validation')
            review_patch=phases.admit_review({**run,'request_json':{**request,'accepted_documents':accepted,'review_execution_ref':execution,
                'research_validation_artifact':research_validation}})
            review_patch['review_execution_ref']=execution
            if research_validation:review_patch['research_validation_artifact']=research_validation
        effect_refs=deepcopy(public_result['checkpoint']['external_effect_refs'])
        for ref in effect_refs:self.verify_ref(project,ref)
        if file_effects:
            effect_refs.append(resource_ref(kind='receipt',identity='file-effects:'+str(run['run_id'])+':'+str(run['attempt']),
                uri=file_effects['uri'],sha256=file_effects.get('sha256'),purpose='actual observed file effects'))
        proposal=self.documents.read(project,package['prompt_ref'])['payload']
        turn_payload={'turn_number':package['turn_number'],'input_refs':proposal['user_message_refs'],'prompt_ref':package['prompt_ref'],
            'capability_profile_ref':package['capability_ref'],'file_authority_ref':package['file_authority_ref'],
            'work_performed':public_result['summary'] or 'No completed work was reported','outcome':outcome,
            'tool_receipt_refs':public_result['evidence_refs'],'effect_receipt_refs':effect_refs,
            'artifact_refs':public_result['artifact_refs']+([report_ref] if report_ref else []),
            'delegation_refs':public_result['delegation_results'],'checkpoint':public_result['checkpoint'],
            'previous_turn_ref':accepted.get('last_turn')}
        turn_ref=self.documents.write_scoped_revision(project_id=project,scope='task',object_id='turn-record',task_id=task_id,
            run_id=task_id,document_kind='turn-record',document_schema='turn-record.v1',payload=turn_payload,status='submitted',
            actor_type='kernel',actor_id='kernel',reason='Preserve original typed result and actual Turn evidence',source_refs=[raw_ref])
        accepted['last_turn']=turn_ref
        async with self.repository.database.connection() as connection:
            owner=await (await connection.execute("""SELECT * FROM assistant.runs WHERE run_id=%s AND status='running'
                AND worker_instance_id=%s AND claim_token=%s AND cancel_requested_at IS NULL FOR UPDATE""",
                (task_id,worker_instance_id,run['claim_token']))).fetchone()
            if not owner:raise PermissionError('PM Turn lease changed before settlement')
            current=deepcopy(owner['request_json'] or {})
            stale=await self.prompts.input_head_locked(connection,owner)!=execution_head
            stale=stale or current.get('accepted_documents',{})!=request.get('accepted_documents',{})
            later_inputs=await self.repository._settle_turn_inputs(connection,owner)
            stale=stale or bool(later_inputs)
            if stale:
                accepted=deepcopy(current.get('accepted_documents') or {})
                accepted['last_turn']=turn_ref
            patch={'task_turn_count':package['turn_number'],'state_revision':int(current.get('state_revision') or 0)+1,
                'accepted_documents':accepted,'pending_supervision_turn':package['turn_number'],
                'pm_result_ref':raw_ref,'task_phase':'waiting_supervisor','pending_task_completion':bool(payload['task_completion_proposed']),
                'supervision_failure':None}
            if not stale:patch.update(review_patch)
            if not stale:
                from assistant_kernel.services.operation_records import OperationRecords
                operation_refs=await OperationRecords(self.settings,self.repository).accept_locked(connection,owner,payload,report_payload)
                if operation_refs:patch['accepted_operation_refs']=operation_refs
            if not stale and payload['runtime_handoffs']:
                from assistant_kernel.services.runtime_handoffs import RuntimeHandoffs
                ownership=await RuntimeHandoffs(self.settings,self.repository).apply_locked(connection,
                    task={**owner,'request_json':{**current,'accepted_documents':accepted}},handoffs=payload['runtime_handoffs'],turn_ref=turn_ref)
                if ownership:patch['runtime_ownership_receipt_ref']=ownership
                activation=await RuntimeHandoffs(self.settings,self.repository).accept_activation_locked(connection,
                    task={**owner,'request_json':{**current,'accepted_documents':accepted}},handoffs=payload['runtime_handoffs'])
                if activation:accepted['activation']=activation
            if phase=='build' and report_ref and not stale:
                patch.update(audit_complete=False,reviewed_bundle_sha256=None,reviewed_design_sha256=None,
                    reviewed_requirements_sha256=None,reviewed_implementation_sha256=None)
            if phase=='audit' and not stale and outcome in {'request_changes','return_to_requirements','return_to_design'}:
                patch.update(audit_complete=False,validation_gate_status=None,validation_artifact=None,
                    reviewed_bundle_sha256=None,reviewed_design_sha256=None,reviewed_requirements_sha256=None,
                    rework_class='requirements' if outcome=='return_to_requirements' else 'design' if outcome=='return_to_design' else 'implementation')
            if not stale and outcome=='return_to_requirements':patch.update(requirements_approval=None,design_approval=None,audit_complete=False)
            if not stale and outcome=='return_to_design':patch.update(design_approval=None,audit_complete=False)
            if validation_result:
                patch['validation_settlement']={k:v for k,v in validation_result.items() if k!='acceptance_patch'}
                if not stale and not validation_result['stale']:patch.update(validation_result['acceptance_patch'])
            if phase in {'requirements','design'} and report_ref and not stale:
                approval_key='requirements_approval' if phase=='requirements' else 'design_approval'
                if (current.get(approval_key) or {}).get('approved_document_sha256')!=report_ref['sha256']:
                    patch[approval_key]=None
                    if phase=='requirements':patch['design_approval']=None
                patch['task_contract_ref' if phase=='requirements' else 'solution_design_ref']=report_ref
                patch['task_contract_sha256' if phase=='requirements' else 'solution_design_sha256']=report_ref['sha256']
            status='waiting_supervisor';question_ids=[];reply=None
            # Each Turn answers its specific confirmation, not the Task's first
            # message (which can already have a reply from a previous phase).
            source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
                (current['prompt_confirmation_message_id'],))).fetchone()
            if not source or source['project_id']!=project:raise PermissionError('Turn confirmation source is unavailable')
            if public_result['questions'] and not stale:
                groups=QuestionGroupService(self.settings,self.repository)
                routes=await groups.resolve_routes_locked(connection,owner=owner,source=source,interaction_scope='pm_business')
                canonical=await groups.create_locked(connection,owner=owner,questions=public_result['questions'],
                    source_message_id=str(source['message_id']),routes=routes)
                question_ids=canonical['content_json']['projection_ids'];status='waiting_user'
            elif outcome in {'completed','needs_approval'} and phase in {'requirements','design'} and report_ref and not stale:
                patch['approval_kind']=phase
                reply=await self.repository._create_outbound_locked(connection,project_id=project,
                    session_id=str(owner['session_id']) if owner.get('session_id') else None,run_id=task_id,
                    channel=source['channel'],chat_id=source['chat_id'],user_id=source['user_id'],message_type='approval',
                    content={'text':public_result['summary'],'document_markdown':json.dumps(report_payload,ensure_ascii=False,indent=2),
                        'approval_question':'是否批准这份完整'+('需求' if phase=='requirements' else '设计')+'文档？',
                        'report_ref':report_ref,'interaction_seq':int(owner['interaction_seq'])+1},
                    reply_to_message_id=source['message_id'],idempotency_key='v12-report-approval:'+task_id+':'+report_ref['sha256'])
                question_ids=[str(reply['message_id'])];status='waiting_user'
            else:
                reply=await self.repository._create_outbound_locked(connection,project_id=project,
                    session_id=str(owner['session_id']) if owner.get('session_id') else None,run_id=task_id,
                    channel=source['channel'],chat_id=source['chat_id'],user_id=source['user_id'],message_type='reply',
                    content={'text':public_result['summary'],'origin':'pm','context_visibility':'semantic','turn_ref':turn_ref,
                        'report_ref':report_ref,'document_markdown':json.dumps(report_payload,ensure_ascii=False,indent=2) if report_payload else None},
                    reply_to_message_id=source['message_id'],idempotency_key='v12-turn-reply:'+task_id+':'+str(package['turn_number']))
            patch['pending_supervision_source_id']=str(reply['message_id']) if reply else question_ids[0]
            executor=deepcopy(owner.get('executor_json') or {})
            validator=executor.get('validation_process') or {}
            if validator.get('original_pm_result_ref')==raw_ref and validator.get('cleanup',{}).get('status')=='verified':
                executor['validation_process']={**validator,'settled_turn_ref':turn_ref}
            settled=await (await connection.execute("""UPDATE assistant.runs SET status=%s,request_json=request_json || %s,error_json=NULL,
                result_json=%s,artifact_refs=%s,interaction_seq=interaction_seq+1,waiting_question_message_id=%s,executor_json=%s,
                worker_id=NULL,worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,process_pid=NULL,
                process_start_ticks=NULL,updated_at=now() WHERE run_id=%s RETURNING *""",
                (status,Jsonb(patch),Jsonb({**public_result,'question_message_ids':question_ids,'token_usage':usage_snapshot}),
                 Jsonb(turn_payload['artifact_refs']),question_ids[0] if question_ids else None,Jsonb(executor),task_id))).fetchone()
            supervisor=await self.repository._queue_supervision_locked(connection,settled,patch['pending_supervision_source_id'],reason='after_agent_turn')
            if not supervisor:raise RuntimeError('Typed Turn cannot settle without its durable supervision obligation')
            from assistant_kernel.services.document_projections_v12 import DocumentProjectionsV12
            adopted_refs=[turn_ref]+([report_ref] if report_ref and not stale else [])
            if accepted.get('activation') and accepted.get('activation')!=(request.get('accepted_documents') or {}).get('activation'):
                adopted_refs.append(accepted['activation'])
            await DocumentProjectionsV12(self.settings,self.repository).record_locked(connection,project=project,owner=settled,
                refs=adopted_refs,source_refs=[raw_ref])
            receipt={'turn_ref':turn_ref,'raw_result_ref':raw_ref,'effect_receipt_refs':effect_refs,
                'status':'partial' if stale else 'failed' if outcome=='failed' else 'settled',
                'supervisor_run_ref':resource_ref(kind='receipt',identity=str(supervisor['run_id']),
                    uri='db://assistant/runs/'+str(supervisor['run_id']),purpose='durable required Supervisor evaluation'),
                'pending_supervision':True}
            contracts().validate('TurnSettlement',receipt);await connection.commit()
        return {'outcome':outcome,'status':status,'run_id':task_id,'settlement':receipt,'token_usage':usage_snapshot}
