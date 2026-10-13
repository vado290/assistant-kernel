"""Facts and assessments have separate freshness; hypotheses never grant authority."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone

from psycopg.types.json import Jsonb

from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.prompt_protocol import PromptProtocol
from assistant_kernel.services.v12_evidence import V12Evidence


class SupervisorAssessmentV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.documents=TaskDocumentService(settings)
        self.prompts=PromptProtocol(settings,repository)

    @staticmethod
    def key(project):return 'v12-project-snapshot-head:'+project

    async def previous_locked(self,connection,project):
        row=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(self.key(project),))).fetchone()
        return self.documents.read(project,row['value']['snapshot_ref'])['payload'] if row else None

    @staticmethod
    def visible_intent(intent,now=None):
        result=deepcopy(intent);now=now or datetime.now(timezone.utc)
        result['hypotheses']=[h for h in result['hypotheses'] if datetime.fromisoformat(h['expires_at'].replace('Z','+00:00'))>now]
        return result

    async def intent_locked(self,connection,project,intent,previous):
        contracts().validate('UserIntent',intent)
        result=deepcopy(intent);old={h['hypothesis_id']:h for h in (previous or {}).get('user_intent_context',{}).get('hypotheses',[])}
        old_preferences={item['statement']:item for item in (previous or {}).get('user_intent_context',{}).get('confirmed_preferences',[])}
        for collection in ('explicit_goals','confirmed_preferences','current_concerns','hypotheses'):
            for claim in result[collection]:
                observed=[]
                for ref in claim['source_refs']:
                    if ref['kind']!='message':raise PermissionError('User intent must be grounded in original user messages')
                    source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(ref['id'],))).fetchone()
                    if not source or source['project_id']!=project or source['direction']!='inbound' or not source['user_id']:
                        raise PermissionError('User intent has no original same-Project human expression')
                    if source['content_json'].get('context_visibility') in {'ui_only','trace_only'}:
                        raise PermissionError('Navigation, delivery and waiting are not evidence of user intent')
                    observed.append(source['created_at'])
                if not observed:raise ValueError('User intent has no supporting source')
                if collection=='hypotheses':
                    basis=max(observed);expires=basis+timedelta(days=7)
                    prior=old.get(claim['hypothesis_id'])
                    if prior:expires=min(expires,datetime.fromisoformat(prior['expires_at'].replace('Z','+00:00')))
                    claim['observed_at']=basis.isoformat();claim['expires_at']=expires.isoformat();claim['is_authorization']=False
                elif collection=='confirmed_preferences':
                    prior=old_preferences.get(claim['statement'])
                    if prior and claim['status']=='withdrawn' and claim['source_refs']==prior['source_refs'] and prior['status']!='withdrawn':
                        raise PermissionError('Withdrawing a confirmed preference needs a new actual user source')
                    claim['confirmed_at']=prior['confirmed_at'] if prior and claim['source_refs']==prior['source_refs'] else max(observed).isoformat()
        incoming={item['statement'] for item in result['confirmed_preferences']}
        result['confirmed_preferences'] += [deepcopy(item) for text,item in old_preferences.items() if text not in incoming]
        return self.visible_intent(result)

    async def commit_locked(self,connection,*,control,context,decision,raw_ref,stale):
        project=control['project_id'];original=self.documents.read(project,context['snapshot_ref'])['payload']
        assessment=deepcopy(decision['project_assessment']);intent=deepcopy(decision['user_intent_context'])
        contracts().validate('Assessment',assessment);contracts().validate('UserIntent',intent)
        await V12Evidence(self.settings,self.repository).verify(project,assessment)
        facts=original['facts_basis'];known=[*facts['source_refs'],context['facts_basis_ref'],context['snapshot_ref'],context['input_head_ref']]
        # Compatibility for a completed pre-patch evaluation: its fixed control
        # trigger was also supplied in model_context, even though facts omitted it.
        trigger=context.get('trigger_message_ref') or resource_ref(kind='message',identity=str(control['trigger_message_id']),
            uri='db://assistant/messages/'+str(control['trigger_message_id']),purpose='actual frozen control trigger')
        known.append(trigger)
        def identity(ref):
            return tuple(ref.get(key) for key in ('kind','id','uri','document_schema','revision_id','sha256'))
        by_identity={}
        for ref in known:by_identity.setdefault(identity(ref),ref)
        if any(identity(ref) not in by_identity for ref in assessment['covered_source_refs']):
            raise PermissionError('Assessment claims source coverage outside its captured facts')
        assessment['covered_source_refs']=[by_identity[identity(ref)] for ref in assessment['covered_source_refs']]
        if any(version not in facts['task_versions'] for version in assessment['covered_task_versions']):
            raise PermissionError('Assessment covers a different Task version')
        previous=await self.previous_locked(connection,project)
        head=await (await connection.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(self.key(project),))).fetchone()
        stale=stale or (head['value']['snapshot_ref'] if head else None)!=context.get('previous_snapshot_ref')
        intent_error=None
        try:
            await V12Evidence(self.settings,self.repository).verify(project,intent)
            intent=await self.intent_locked(connection,project,intent,previous)
        except (ValueError,PermissionError,FileNotFoundError) as exc:
            # User understanding cannot grant authority. Reject this auxiliary
            # update without blocking a separately validated business handoff.
            intent_error=str(exc)
            intent=self.visible_intent((previous or original)['user_intent_context'])
        now=datetime.now(timezone.utc).isoformat()
        if stale:assessment['status']='stale'
        elif assessment['status']=='evaluated' and original['coverage']['status']!='complete':assessment['status']='partial'
        if assessment['status']!='facts_only':
            assessment['assessed_at']=now;assessment['evaluated_source_batch_id']=facts['source_batch_id']
        else:
            assessment['assessed_at']=None;assessment['evaluated_source_batch_id']=None
        snapshot={**original,'project_assessment':assessment,'user_intent_context':intent}
        if intent_error:
            snapshot['coverage']=deepcopy(original['coverage'])
            snapshot['coverage']['notes'].append('User-intent update rejected; prior accepted context retained: '+intent_error)
        ref=self.documents.write_scoped_revision(project_id=project,scope='project',object_id='project-snapshot',
            document_kind='project-snapshot',
            document_schema='project-snapshot.v1',payload=snapshot,status='historical' if stale else 'settled',
            actor_type='supervisor',actor_id=control['agent_key'],reason='Evaluation accepted only at its captured facts head',
            source_refs=[context['snapshot_ref'],raw_ref])
        receipt={'assessment_ref':ref,'status':'stale' if stale else 'accepted','accepted_input_head_ref':context['input_head_ref'],
            'prompt_preparation_ref':None,'owner_disposition':'history_only' if stale else 'wait'}
        contracts().validate('AssessmentCommit',receipt)
        if not stale:
            await connection.execute("""INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s)
                ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",(self.key(project),Jsonb({'snapshot_ref':ref,'assessment_commit':receipt})))
            from assistant_kernel.services.document_projections_v12 import DocumentProjectionsV12
            owner=await self.repository.get_run(context['task_id']) if context['task_id'] else control
            await DocumentProjectionsV12(self.settings,self.repository).record_locked(connection,project=project,owner=owner,
                refs=[ref],source_refs=[raw_ref],guard='project_snapshot')
        return receipt
