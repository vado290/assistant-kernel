"""Deterministic validation, final-requirement Review and completion gates."""
from copy import deepcopy
from datetime import datetime,timezone
import asyncio
import hashlib

from psycopg.types.json import Jsonb

from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.prompt_protocol import PromptProtocol


class PhaseControlV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository
        self.documents=TaskDocumentService(settings);self.prompts=PromptProtocol(settings,repository)

    @staticmethod
    def requires_runtime_delivery(contract,design):
        """C20: actual confirmed obligations, not descriptive runtime entries."""
        return bool(contract.get('schedule_requirements') or design.get('schedule_plans')
            or set(contract['delivery_obligations']) & {'runtime','long_term_effect'}
            or any(item['required'] and item['applicability'] in {'runtime','long_term_effect'}
                for item in contract['acceptance_criteria'])
            or design.get('activation_plan',{}).get('applicable') is True)

    def baseline(self,task):
        accepted=(task['request_json'] or {}).get('accepted_documents') or {}
        refs={name:accepted.get(name) for name in ('task_contract','solution_design','implementation_report','review_report')}
        docs={name:self.documents.read(task['project_id'],ref) for name,ref in refs.items() if ref}
        for name,document in docs.items():
            if document.get('task_id')!=str(task['run_id']):raise PermissionError('Phase document belongs to another Task: '+name)
        return refs,docs

    async def validate_build(self,*,task,expected_input_head,running_claim=None,cancel_event=None):
        """Run existing candidate validation outside the control transaction."""
        refs,docs=self.baseline(task)
        if not all(name in docs for name in ('task_contract','solution_design','implementation_report')):
            raise ValueError('Validation needs the accepted requirements, design and implementation')
        implementation=docs['implementation_report']['payload'];design=docs['solution_design']['payload']
        required_schedules=docs['task_contract']['payload'].get('schedule_requirements') or []
        if required_schedules:
            plans={p['schedule_id']:p for p in design.get('schedule_plans',[])}
            if any(item['schedule_id'] not in plans or plans[item['schedule_id']]!=item for item in required_schedules):
                raise PermissionError('Accepted schedule requirements were omitted or changed by the design')
        from assistant_kernel.services.schedule_delivery import validate_handoffs
        validate_handoffs(self.settings,self.repository,task['project_id'],design,implementation)
        from assistant_kernel.services.document_runtime import same_resource_ref
        if not same_resource_ref(implementation['task_contract_ref'],refs['task_contract']) or not same_resource_ref(implementation['solution_design_ref'],refs['solution_design']):
            raise PermissionError('Implementation is based on older requirements/design')
        async with self.repository.database.connection() as connection:
            current=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s',(task['run_id'],))).fetchone()
            if (not current or current.get('cancel_requested_at')
                    or await self.prompts.input_head_locked(connection,current)!=expected_input_head):
                return {'validation':None,'sealed_bundle':None,'stale':True,'input_head_ref':expected_input_head,'acceptance_patch':{}}
        request=task['request_json'] or {};policy=dict(request.get('validation_policy') or {})
        sealed=None
        if implementation.get('candidate_ref') or design['publish_targets']:
            from assistant_kernel.services.candidate_bundle import CandidateBundleService
            from assistant_kernel.services.validation_process_v12 import ValidationProcessV12
            bundle=await asyncio.to_thread(CandidateBundleService(self.settings).seal,task['project_id'],str(task['run_id']))
            declared=implementation.get('candidate_ref')
            if declared and declared['uri'] not in {bundle['bundle_uri'],
                    'vault://'+task['project_id']+'/runs/'+str(task['run_id'])+'/candidate/manifest.json'}:
                raise PermissionError('Implementation candidate is not the actual Task candidate')
            if declared and declared['sha256']!=(bundle['bundle_file_sha256'] if declared['uri']==bundle['bundle_uri'] else bundle['manifest_sha256']):
                raise PermissionError('Implementation candidate hash differs from the actual sealed bytes')
            if not running_claim:raise PermissionError('Executable validation requires the current claimed Task')
            validation=await ValidationProcessV12(self.settings,self.repository).run(task=task,policy=policy,bundle=bundle,cancel_event=cancel_event)
            sealed={'uri':bundle['bundle_uri'],'sha256':bundle['bundle_file_sha256'],'bundle_sha256':bundle['bundle_sha256'],
                'kind':'candidate_bundle','required':True,'size':bundle['bundle_size']}
        else:
            # Semantic-only delivery has no formal-file bundle. Verify every
            # actual deliverable hash; this is structure evidence, not Review.
            implementation_refs=implementation['actual_changes']
            if not implementation_refs:
                # An inline report can legitimately make no file changes. Use
                # the actual archived response, not a manufactured change Ref.
                inline_ref=request.get('pending_pm_result_ref')
                inline=self.prompts.read_blob(task['project_id'],inline_ref) if inline_ref else {}
                if (not str(inline.get('summary') or '').strip()
                        or inline.get('payload',{}).get('report_payload')!=implementation):
                    raise ValueError('Semantic delivery has no actual deliverable references or matching inline result')
                implementation_refs=[inline_ref]
            from assistant_kernel.services.v12_evidence import V12Evidence
            await V12Evidence(self.settings,self.repository).verify(task['project_id'],implementation_refs,task=task)
            evidence=self.prompts.write_blob(task['project_id'],str(task['run_id']),'semantic-validation',{
                'schema_version':'kernel-semantic-validation.v1','task_contract_ref':refs['task_contract'],
                'solution_design_ref':refs['solution_design'],'implementation_ref':refs['implementation_report'],
                'deliverable_refs':implementation_refs,'verified_at':datetime.now(timezone.utc).isoformat(),
                'formal_publication':'not_applicable','scope':'structure','business_acceptance':'pending_independent_review'})
            validation={**evidence,'release_gate':'pass','kind':'semantic_validation','scope':'structure'}
        async with self.repository.database.connection() as connection:
            current=await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(task['run_id'],))).fetchone()
            stale=not current or await self.prompts.input_head_locked(connection,current)!=expected_input_head
            if not stale:
                owns_running=bool(running_claim and current['status']=='running'
                    and str(current['claim_token'])==running_claim['claim_token']
                    and str(current['worker_instance_id'])==running_claim['worker_instance_id'])
                if (current['status'] in {'running','reconciling','cancelled','succeeded'} and not owns_running) or current.get('cancel_requested_at'):
                    stale=True
        # The expensive work produces evidence only. Its acceptance belongs to
        # the same transaction as the typed Turn and supervision obligation.
        patch={'validation_artifact':validation,'sealed_candidate_bundle':sealed,
            'validation_design_sha256':refs['solution_design']['sha256'],
            'validation_requirements_sha256':refs['task_contract']['sha256'],
            'validation_implementation_sha256':refs['implementation_report']['sha256'],
            'validation_feedback':None if validation['release_gate']=='pass' else validation,
            'validation_gate_status':validation['release_gate']}
        if sealed:
            patch['candidate_manifest_ref']={'uri':'vault://'+task['project_id']+'/runs/'+str(task['run_id'])+'/candidate/manifest.json',
                'sha256':bundle['manifest_sha256'],'kind':'candidate_manifest','required':True}
        return {'validation':validation,'sealed_bundle':sealed,'stale':stale,
            'input_head_ref':expected_input_head,'acceptance_patch':patch}

    def review_checks(self,task):
        """Require complete final-baseline coverage; source existence alone is not acceptance."""
        refs,docs=self.baseline(task)
        research_only=bool(docs.get('task_contract',{}).get('payload',{}).get('delivery_obligations')==['research'] and 'implementation_report' not in docs and 'solution_design' not in docs)
        required_names=('task_contract','review_report') if research_only else tuple(refs)
        if not all(name in docs for name in required_names):raise ValueError('Independent Review has missing fixed phase documents')
        review=docs['review_report']['payload'];requirement=docs['task_contract']['payload']
        from assistant_kernel.services.document_runtime import same_resource_ref
        if not all(any(same_resource_ref(refs[name],ref) for ref in review['fixed_input_refs']) for name in ('task_contract','solution_design','implementation_report') if refs[name]):
            raise PermissionError('Review does not cover the currently effective final baseline')
        criteria={c['criterion_id']:c for c in requirement['acceptance_criteria']}
        if len(criteria)!=len(requirement['acceptance_criteria']):raise ValueError('Final requirements have duplicate acceptance identities')
        requirements={r['requirement_id']:r for r in requirement['requirements']}
        if len(requirements)!=len(requirement['requirements']):raise ValueError('Final requirements have duplicate requirement identities')
        for identity,item in requirements.items():
            if item['priority']=='must' and not item['acceptance_ids']:raise ValueError('A final mandatory requirement has no acceptance mapping')
            for criterion_id in item['acceptance_ids']:
                if criterion_id not in criteria or identity not in criteria[criterion_id]['requirement_ids']:
                    raise ValueError('Final requirement-to-acceptance mapping is incomplete')
        for criterion in criteria.values():
            if not criterion['requirement_ids'] or any(identity not in requirements or criterion['criterion_id'] not in requirements[identity]['acceptance_ids']
                    for identity in criterion['requirement_ids']):
                raise ValueError('Acceptance-to-final-requirement reverse mapping is incomplete')
        checks={}
        runtime_only=[check for check in review['runtime_checks'] if check['result']=='not_applicable']
        for check in review['per_item_checks']+review['runtime_checks']:
            for identity in check['criterion_ids']:
                # Preserve extra observations/Prompt-local checks in the report;
                # they cannot count toward any final required criterion.
                if identity not in criteria:continue
                # "No runtime delivery applies" is not a waiver of a separate
                # artifact criterion that the per-item Review must still pass.
                if (check in runtime_only and criteria[identity]['applicability'] not in {'runtime','long_term_effect'}):continue
                if identity in checks and checks[identity]['result']!=check['result']:
                    raise ValueError('Review has conflicting conclusions for a criterion')
                checks[identity]=check
        missing=[identity for identity,c in criteria.items() if c['required'] and identity not in checks]
        if missing:raise ValueError('Review omitted final required criteria: '+','.join(missing))
        unresolved=[issue for issue in review['issues'] if issue['resolution']=='open' and issue['severity'] in {'blocking','major'}]
        return refs,docs,criteria,checks,unresolved

    async def review_execution(self,task,invocation_id):
        """Use Kernel invocation facts to prove Audit did not reuse a builder session."""
        async with self.repository.database.connection() as connection:
            invocation=await (await connection.execute('SELECT * FROM assistant.model_invocations WHERE invocation_id=%s',
                (invocation_id,))).fetchone()
            if (not invocation or invocation['status']!='succeeded' or invocation['project_id']!=task['project_id']
                    or str(invocation['run_id'])!=str(task['run_id']) or not invocation.get('session_id')
                    or not invocation.get('native_session_id') or not invocation.get('trace_uri') or not invocation.get('trace_sha256')):
                raise PermissionError('Review has no actual successful native invocation and pinned trace')
            metadata=invocation['metadata_json'] or {}
            if metadata.get('task_stage')!='audit' or metadata.get('confirmed_package_sha256')!=(task['request_json'].get('confirmed_turn_package_ref') or {}).get('sha256'):
                raise PermissionError('Review invocation did not execute the confirmed Audit package')
            history=await (await connection.execute('SELECT metadata_json FROM assistant.model_invocations WHERE session_id=%s ORDER BY started_at,invocation_id',
                (invocation['session_id'],))).fetchall()
            if any((item['metadata_json'] or {}).get('task_stage')!='audit' for item in history):
                raise PermissionError('Independent Review reused a non-Audit physical session')
            if not history or (history[0]['metadata_json'] or {}).get('native_continuation_requested') is not False:
                raise PermissionError('Independent Review session has no proven cold initial invocation')
        return self.prompts.write_blob(task['project_id'],str(task['run_id']),'independent-review-execution',{
            'invocation_id':invocation_id,'session_id':str(invocation['session_id']),
            'native_session_id':invocation['native_session_id'],'trace_uri':invocation['trace_uri'],
            'trace_sha256':invocation['trace_sha256'],'confirmed_package_ref':task['request_json']['confirmed_turn_package_ref'],
            'independent_from_implementation':True})

    def admit_review(self,task):
        refs,docs,criteria,checks,unresolved=self.review_checks(task)
        review=docs['review_report']['payload'];request=task['request_json'] or {}
        if not request.get('review_execution_ref'):raise PermissionError('Independent Review has no accepted execution provenance')
        research_only=not refs['solution_design'] and not refs['implementation_report'] and docs['task_contract']['payload']['delivery_obligations']==['research']
        if research_only:
            validation=request.get('research_validation_artifact')
            if not validation or self.prompts.read_blob(task['project_id'],validation)['task_contract_ref']!=refs['task_contract']:
                raise PermissionError('Research Review lacks its pinned actual-deliverable validation')
        elif request.get('validation_gate_status')!='pass' or any(request.get(key)!=refs[name]['sha256'] for key,name in (
                ('validation_design_sha256','solution_design'),('validation_requirements_sha256','task_contract'),
                ('validation_implementation_sha256','implementation_report'))):
            raise PermissionError('Review cannot accept an unvalidated or different design baseline')
        if review['conclusion']!='pass' or unresolved:raise ValueError('Independent Review has unresolved blocking findings')
        for identity,criterion in criteria.items():
            if not criterion['required']:continue
            check=checks[identity]
            if criterion['applicability'] in {'research','artifact'} and check['result']!='passed':
                raise ValueError('Required pre-delivery acceptance did not pass: '+identity)
            if check['result']=='not_applicable':raise ValueError('A required obligation cannot be waived after implementation: '+identity)
        return {'audit_complete':True,'reviewed_bundle_sha256':(request.get('sealed_candidate_bundle') or {}).get('bundle_sha256'),
            'reviewed_design_sha256':(refs['solution_design'] or {}).get('sha256'),'reviewed_requirements_sha256':refs['task_contract']['sha256'],
            'reviewed_implementation_sha256':(refs['implementation_report'] or {}).get('sha256'),'review_report_ref':refs['review_report']}

    def completion_checks(self,task):
        refs,docs,criteria,checks,unresolved=self.review_checks(task)
        request=task['request_json'] or {};accepted=request.get('accepted_documents') or {}
        if not request.get('audit_complete') or unresolved or docs['review_report']['payload']['conclusion']!='pass':
            raise PermissionError('Task completion requires independent final-requirement Review')
        if any(request.get(key)!=(refs[name] or {}).get('sha256') for key,name in (
                ('reviewed_design_sha256','solution_design'),('reviewed_requirements_sha256','task_contract'),
                ('reviewed_implementation_sha256','implementation_report'))):
            raise PermissionError('Task completion Review does not cover the current baseline')
        if request.get('publication_state') not in {'published','not_applicable'}:
            raise PermissionError('Applicable publication has not settled')
        for kind in ('activation','operation_report'):
            ref=accepted.get(kind)
            if not ref:continue
            document=self.documents.read(task['project_id'],ref)
            if document.get('task_id')!=str(task['run_id']):raise PermissionError('Completion evidence belongs to another Task')
            body=document['payload']
            for check in body.get('per_item_checks') or []:
                for identity in check['criterion_ids']:
                    if identity not in criteria:raise ValueError('Activation refers to unknown acceptance criteria')
                    if criteria[identity]['applicability'] not in {'runtime','long_term_effect'}:
                        raise ValueError('Activation cannot replace independent artifact/research Review')
                    checks[identity]=check
        unmet=[identity for identity,c in criteria.items() if c['required'] and checks.get(identity,{}).get('result')!='passed']
        if unmet:raise PermissionError('Task retains required research/artifact/runtime/effect obligations: '+','.join(unmet))
        if self.requires_runtime_delivery(docs['task_contract']['payload'],docs.get('solution_design',{}).get('payload',{})) and not accepted.get('activation'):
            raise PermissionError('Required runtime activation has no accepted actual receipt')
        return refs,docs,checks

    def release_display(self,task):
        """Reuse the actual sealed-diff reader; no second publication mechanism."""
        from assistant_kernel.services.run_processor import RunProcessor
        processor=RunProcessor(self.settings,self.repository,None)
        refs,docs=self.baseline(task)
        review=docs['review_report']['payload']
        if review['publication_recommendation']!='publish':raise PermissionError('Independent Review has not recommended this publication')
        import json
        automatic=processor._automatic_low_risk_release(task)
        markdown=processor._release_approval_markdown(task,json.dumps(review,ensure_ascii=False,indent=2))
        return automatic,markdown

    async def prepare_release_locked(self,connection,*,task,source,automatic,markdown):
        request=task['request_json'] or {}
        if request.get('publication_state') in {'queued','published'}:return None
        if automatic:
            return await self.repository._queue_approved_publication_locked(connection,task,str(source['message_id']),authorization_kind='design')
        head=await self.prompts.input_head_locked(connection,task)
        sealed=request['sealed_candidate_bundle']
        display=await self.repository._create_outbound_locked(connection,project_id=task['project_id'],
            session_id=str(task['session_id']) if task.get('session_id') else None,run_id=str(task['run_id']),
            channel=source['channel'],chat_id=source['chat_id'],user_id=source['user_id'],message_type='approval',
            content={'text':'独立 Review 已通过，请审阅并批准这份具体发布差异。','document_markdown':markdown,
                'approval_question':'是否批准发布这份密封候选？','interaction_seq':int(task['interaction_seq'])+1,
                'publication_bundle_ref':sealed,'review_ref':request['accepted_documents']['review_report'],'input_head_ref':head},
            reply_to_message_id=source['message_id'],idempotency_key='v12-release-approval:'+str(task['run_id'])+':'+sealed['sha256'])
        await connection.execute("""UPDATE assistant.runs SET status='waiting_user',waiting_question_message_id=%s,
            interaction_seq=interaction_seq+1,request_json=request_json || %s WHERE run_id=%s""",
            (display['message_id'],Jsonb({'approval_kind':'release','publication_input_head_ref':head}),task['run_id']))
        return {'display_message_id':str(display['message_id']),'publication_state':'awaiting_specific_approval'}

    async def complete_locked(self,connection,*,task,source):
        """Accept references/ownership and close the same Task in one DB commit."""
        if task.get('cancel_requested_at') or task['status'] in {'running','reconciling','succeeded','failed','cancelled','dead_letter'}:
            raise PermissionError('Only a settled active Task can complete')
        pending=await (await connection.execute("""SELECT 1 FROM assistant.messages WHERE message_type='question_group'
            AND (run_id=%s OR content_json->>'task_id'=%s) AND content_json#>>'{group,interaction_scope}'='pm_business'
            AND content_json#>>'{group,status}'='open' AND (content_json#>>'{group,blocking}')::boolean LIMIT 1""",(task['run_id'],str(task['run_id'])))).fetchone()
        if pending:raise PermissionError('A required business question remains unanswered')
        if (task['request_json'] or {}).get('schedule_actions'):
            from assistant_kernel.services.schedule_operations import complete
            return await complete(connection,self.settings,self.repository,task,source)
        request=deepcopy(task['request_json'] or {});refs,docs=self.baseline(task)
        contract=docs['task_contract']['payload']
        design=docs['solution_design']['payload'] if 'solution_design' in docs else {'publish_targets':[],'runbook_actions':[]}
        if not design['publish_targets'] and request.get('publication_state') not in {'published','not_applicable'}:
            if docs['review_report']['payload']['publication_recommendation']!='not_applicable':
                raise PermissionError('No formal targets but Review has not settled publication applicability')
            applicability=self.prompts.write_blob(task['project_id'],str(task['run_id']),'publication-not-applicable',{
                'solution_design_ref':refs['solution_design'],'review_ref':refs['review_report'],
                'reason':'Approved scope has no formal publication targets; original delivery obligations remain'})
            request.update(publication_state='not_applicable',publication_applicability_ref=applicability)
            await connection.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
                (Jsonb({'publication_state':'not_applicable','publication_applicability_ref':applicability}),task['run_id']))
            task['request_json']=request
        checked={**task,'request_json':request};refs,docs,checks=self.completion_checks(checked)
        if design.get('schedule_plans'):
            from assistant_kernel.services.schedule_delivery import verify_activation_locked
            activation_ref=request['accepted_documents'].get('activation')
            if not activation_ref:raise PermissionError('Schedule delivery requires an actual activation report')
            await verify_activation_locked(connection,self.settings,self.repository,task['project_id'],design,
                docs['implementation_report']['payload'],self.documents.read(task['project_id'],activation_ref)['payload'])
        from assistant_kernel.services.v12_evidence import V12Evidence
        verifier=V12Evidence(self.settings,self.repository)
        await verifier.verify(task['project_id'],[docs['review_report']['payload'],request.get('review_execution_ref')],task=task)
        unfinished=await (await connection.execute("""SELECT run_id FROM assistant.runs WHERE parent_run_id=%s
            AND (request_json ? 'operation_action' OR COALESCE((request_json->>'required_for_completion')::boolean,false))
            AND status NOT IN ('succeeded','cancelled') LIMIT 1""",(task['run_id'],))).fetchone()
        if unfinished:raise PermissionError('A required action has not settled; reconcile it without replay')
        from assistant_kernel.services.acceptance_feedback_v12 import AcceptanceFeedbackV12
        feedback=await AcceptanceFeedbackV12(self.settings,self.repository).ensure_locked(connection,task=task,refs=refs,docs=docs,source=source)
        if feedback['status'] in {'waiting_user_feedback','rework_required'}:return feedback
        acceptance=self.prompts.write_blob(task['project_id'],str(task['run_id']),'task-acceptance',{
            'schema_version':'kernel-task-acceptance.v1','task_id':str(task['run_id']),
            'final_document_refs':refs,'per_item_checks':list({c['check_id']:c for c in checks.values()}.values()),
            'independent_review_execution_ref':request['review_execution_ref'],
            'publication_state':request['publication_state'],'user_feedback':feedback,'accepted_at':datetime.now(timezone.utc).isoformat()})
        # Research/artifact-only Tasks close without creating an operational
        # baseline. Existing Project operations keep their existing ownership.
        runtime_required=self.requires_runtime_delivery(contract,design)
        baseline_ref=None
        if runtime_required:
            activation_ref=request['accepted_documents'].get('activation')
            if not activation_ref:raise PermissionError('Runtime delivery has no accepted activation')
            activation=self.documents.read(task['project_id'],activation_ref)['payload']
            if activation['unverified']:raise PermissionError('Runtime activation still contains unverified obligations')
            await verifier.verify(task['project_id'],activation,task=task)
            ownership=request.get('runtime_ownership_receipt_ref')
            if not ownership:raise PermissionError('Runtime objects have not been handed to Project ownership')
            await verifier.verify(task['project_id'],ownership,task=task)
            creating=resource_ref(kind='receipt',identity=str(task['run_id']),uri='db://assistant/runs/'+str(task['run_id']),purpose='creating Task')
            payload={'creating_task_ref':creating,'task_contract_ref':refs['task_contract'],'solution_design_ref':refs['solution_design'],
                'publication_ref':request['accepted_documents'].get('publication') or request['publication_applicability_ref'],
                'activation_ref':activation_ref,'acceptance_record_ref':acceptance,'actual_version_refs':activation['actual_version_refs'],
                'runbook_actions':design['runbook_actions'],'verified_at':datetime.now(timezone.utc).isoformat()}
            baseline_ref=self.documents.write_scoped_revision(project_id=task['project_id'],scope='project',object_id='operational-baseline',
                document_kind='operational-baseline',document_schema='operational-baseline.v1',
                payload=payload,status='settled',actor_type='kernel',actor_id='kernel',reason='All applicable final obligations and ownership verified',
                source_refs=[acceptance,activation_ref,ownership])
            request['operational_baseline_ref']=baseline_ref
        from assistant_kernel.services.task_queue import ProjectTaskQueue
        queue=ProjectTaskQueue(self.settings,self.repository)
        metadata=await queue._meta(connection,task['project_id'])
        if metadata.get('active_task_id') not in {None,str(task['run_id'])}:raise PermissionError('Another foreground Task owns the Project pointer')
        patch={'active_task_id':None}
        if baseline_ref:patch['operational_baseline_ref']=baseline_ref
        await queue._patch_meta(connection,task['project_id'],patch)
        request.update(task_phase='completed',task_activation='closed',pending_task_completion=False,acceptance_record_ref=acceptance,
            state_revision=int(request.get('state_revision') or 0)+1)
        await connection.execute("UPDATE assistant.runs SET status='succeeded',request_json=%s,ended_at=now(),updated_at=now() WHERE run_id=%s",
            (Jsonb(request),task['run_id']))
        if baseline_ref:
            from assistant_kernel.services.document_projections_v12 import DocumentProjectionsV12
            await DocumentProjectionsV12(self.settings,self.repository).record_locked(connection,project=task['project_id'],
                owner={**task,'request_json':request},refs=[baseline_ref],source_refs=[acceptance],guard='operational_baseline')
        await self.repository._create_outbound_locked(connection,project_id=task['project_id'],session_id=None,run_id=str(task['run_id']),
            channel=source['channel'],chat_id=source['chat_id'],user_id=source['user_id'],message_type='reply',
            content={'text':'本任务已按最终需求完成适用验收。','origin':'kernel','acceptance_record_ref':acceptance,
                'operational_baseline_ref':baseline_ref},reply_to_message_id=source['message_id'],
            idempotency_key='v12-task-completed:'+str(task['run_id'])+':'+acceptance['sha256'])
        return {'task_completed':True,'acceptance_record_ref':acceptance,'operational_baseline_ref':baseline_ref}

    async def publication_locked(self,connection,*,publication,parent,result,reply_id):
        """Actual publication is evidence; it does not finish remaining obligations."""
        request=deepcopy(parent['request_json'] or {});accepted=deepcopy(request.get('accepted_documents') or {})
        if request.get('publish_run_id')!=str(publication['run_id']):raise PermissionError('Publication is not the currently approved Task operation')
        creating=resource_ref(kind='receipt',identity=str(parent['run_id']),uri='db://assistant/runs/'+str(parent['run_id']),purpose='creating Task')
        actual=[]
        changes_ref=self.prompts.write_blob(parent['project_id'],str(parent['run_id']),'publication-effects',result)
        for item in result.get('files') or []:
            digest=item.get('candidate_sha256') or item.get('after_sha256')
            if item.get('operation')=='delete':
                # A deletion's actual version is the pinned absence receipt,
                # never a made-up hash for a file that no longer exists.
                actual.append(changes_ref)
                continue
            if digest and item.get('target_uri'):
                actual.append(resource_ref(kind='source',identity='published:'+hashlib.sha256(item['target_uri'].encode()).hexdigest(),
                    uri=item['target_uri'],sha256=digest,purpose='actual Publisher-applied version'))
        if not actual:raise ValueError('Typed publication has no actual published version evidence')
        authorization_id=(publication['request_json'] or {}).get('approval_message_id') or str(publication['trigger_message_id'])
        authorization=resource_ref(kind='message',identity=authorization_id,uri='db://assistant/messages/'+authorization_id,purpose='actual specific publication authority')
        sealed=request.get('sealed_candidate_bundle') or {}
        candidate=resource_ref(kind='source',identity='candidate:'+str(sealed.get('bundle_sha256')),
            uri=sealed.get('uri'),sha256=sealed.get('sha256'),purpose='actual independently reviewed sealed candidate')
        payload={'project_id':parent['project_id'],'creating_task_ref':creating,'candidate_ref':candidate,
            'review_ref':accepted['review_report'],'authorization_ref':authorization,'actual_changes':[changes_ref],
            'actual_version_refs':actual,'published_at':datetime.now(timezone.utc).isoformat()}
        ref=self.documents.write_scoped_revision(project_id=parent['project_id'],scope='task',object_id='publication',
            task_id=str(parent['run_id']),run_id=str(publication['run_id']),document_kind='publication',document_schema='publication-receipt.v1',
            payload=payload,status='settled',actor_type='publisher',actor_id='kernel',reason='Actual Publisher result; runtime/effect obligations remain',
            source_refs=[authorization,accepted['review_report'],changes_ref])
        accepted['publication']=ref
        stopped=bool(parent.get('cancel_requested_at') or parent['status']=='cancelled')
        updated=await (await connection.execute("""UPDATE assistant.runs SET status=%s,ended_at=CASE WHEN %s THEN ended_at ELSE NULL END,
            request_json=request_json || %s,result_json=COALESCE(result_json,'{}'::jsonb) || %s,updated_at=now()
            WHERE run_id=%s RETURNING *""",('cancelled' if stopped else 'waiting_supervisor',stopped,Jsonb({'accepted_documents':accepted,'publication_state':'published',
                'task_phase':'waiting_supervisor','task_activation':'active','state_revision':int(request.get('state_revision') or 0)+1,
                'pending_supervision_source_id':reply_id,'pending_supervision_turn':int(request.get('task_turn_count') or 0)}),
                Jsonb({'publication_receipt':result,'publication_ref':ref}),parent['run_id']))).fetchone()
        if not stopped:await self.repository._queue_supervision_locked(connection,updated,reply_id,reason='publication_settled')
        from assistant_kernel.services.document_projections_v12 import DocumentProjectionsV12
        await DocumentProjectionsV12(self.settings,self.repository).record_locked(connection,project=parent['project_id'],owner=updated,
            refs=[ref],source_refs=[changes_ref])
        return updated
