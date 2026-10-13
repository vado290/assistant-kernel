"""One logical question group over Messages; channel cards are reference projections.

The existing questionnaire engine edits drafts. This service alone accepts final
answers and never queues a PM directly; the repository supervision gate owns that.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re
import uuid

from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.questionnaire import advance_questionnaire, make_questionnaire

GROUP_SCHEMA = 'kernel-question-group.v1'
PROJECTION_SCHEMA = 'question-group-projection.v1'
REFRESH_SCHEMA = 'question-projection-refresh.v1'
POLICIES = {'pm_business':'pm_both', 'supervisor_consultation':'supervisor_primary',
    'runtime_business':'runtime_both', 'runtime_maintenance':'runtime_pm_primary',
    'system_configuration':'kernel_management'}


def projection_ref(content):
    return content.get('question_projection', {}).get('group_ref') if content.get('schema_version') == PROJECTION_SCHEMA else None


def render_group(group, *, sequence=0, correlation=None):
    """Hydrate at read/send time; the stored projection contains no copied draft."""
    body = make_questionnaire(group['questions'], summary='', sequence=sequence, correlation=correlation or {})
    state = deepcopy(group['draft'])
    state['answers'] = {answer['question_id']: answer['value'] for answer in group['accepted_answers']} | state['answers']
    state['status'] = 'draft' if group['status'] == 'open' else 'submitted'
    body['questionnaire'] = state
    from assistant_kernel.services.questionnaire import render_questionnaire
    body['text'] = render_questionnaire(body)
    if group['status'] in {'cancelled','superseded'}:
        body['text'] = '此问题组已'+('取消' if group['status']=='cancelled' else '由新版本替代')+'。\n\n'+body['text']
    return body


class QuestionGroupService:
    def __init__(self, settings, repository):
        self.settings = settings; self.repository = repository

    async def resolve_routes_locked(self,connection,*,owner,source,interaction_scope):
        """Resolve registered primary interfaces; ambiguous/missing routes are a gap."""
        import yaml
        project=owner['project_id'];channel=source['channel'];user=source.get('user_id')
        if not user:raise PermissionError('Question route has no identified original user')
        if interaction_scope=='system_configuration':
            if project!='kernel':raise PermissionError('System configuration questions belong to Kernel management')
            return [{'channel':channel,'chat_id':source['chat_id'],'user_id':user,'interface_role':'kernel','is_origin':True}]
        wanted={'supervisor'} if interaction_scope=='supervisor_consultation' else {'pm','supervisor'}
        candidates={role:set() for role in wanted}
        from assistant_kernel.services.discord_interfaces import DiscordInterfaceRegistry
        registry=DiscordInterfaceRegistry(self.settings)
        if channel=='discord' and 'supervisor' in wanted:
            interface=registry.find(project_id=project,role='supervisor')
            if interface:candidates['supervisor'].add(str(interface['channel_id']))
        rows=await (await connection.execute("""WITH latest AS (
            SELECT DISTINCT ON(channel,chat_id,user_id) channel,chat_id,user_id,project_id,content_json
            FROM assistant.messages WHERE direction='system' AND message_type='route_binding' AND channel=%s
            ORDER BY channel,chat_id,user_id,created_at DESC,message_id DESC)
            SELECT * FROM latest WHERE project_id=%s AND content_json->>'status'='active'
                AND (user_id IS NULL OR user_id=%s)""",(channel,project,user))).fetchall()
        for row in rows:
            actor=str(row['content_json'].get('agent_key') or 'project:'+project+':manager')
            role='supervisor' if actor.endswith(':supervisor') else 'pm' if actor.endswith(':manager') else None
            if role in wanted:candidates[role].add(row['chat_id'])
        path=self.settings.data_root/'config/channel-bindings.yaml'
        entries=(yaml.safe_load(path.read_text('utf-8')) or {}).get('bindings') or [] if path.is_file() else []
        for entry in entries:
            if entry.get('project_id')!=project or entry.get('channel')!=channel or entry.get('user_id') not in {None,user}:continue
            actor=str(entry.get('agent_key') or 'project:'+project+':manager')
            role='supervisor' if actor.endswith(':supervisor') else 'pm' if actor.endswith(':manager') else None
            if role in wanted:candidates[role].add(str(entry['chat_id']))
        root_id=(owner.get('request_json') or {}).get('task_id') or owner['run_id']
        root=await (await connection.execute('SELECT r.agent_key,m.channel,m.chat_id,m.user_id FROM assistant.runs r JOIN assistant.messages m ON m.message_id=r.trigger_message_id WHERE r.run_id=%s',(root_id,))).fetchone()
        if 'pm' in wanted and not candidates['pm'] and root and root['channel']==channel and root['user_id']==user:
            actual=registry.resolve(root['chat_id']) if channel=='discord' else None
            if not actual and root['agent_key'].endswith(':manager'):candidates['pm'].add(root['chat_id'])
        if any(len(candidates[role])!=1 for role in wanted):
            raise ValueError('Missing or ambiguous primary PM/Supervisor interface; configure the actual route before asking')
        return [{'channel':channel,'chat_id':next(iter(candidates[role])),'user_id':user,'interface_role':role,
            'is_origin':next(iter(candidates[role]))==source['chat_id']} for role in sorted(wanted)]

    async def create_locked(self, connection, *, owner, questions, source_message_id,
            routes, interaction_scope='pm_business', blocking=True, runtime_owner=None):
        """Trusted caller provides resolved routes; model output cannot set them."""
        project = owner['project_id']; service_owner=owner.get('service_instance_id')
        owner_id = ('service:'+service_owner+':'+owner['logical_execution_id']+':'+str(owner.get('question_epoch',0))) if service_owner else str(owner['run_id'])
        database_run_id=None if service_owner else owner_id
        if interaction_scope not in POLICIES or not routes: raise ValueError('Question scope or resolved routes missing')
        if interaction_scope=='system_configuration' and project!='kernel':
            raise PermissionError('System configuration questions belong to Kernel management')
        source = await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
            (source_message_id,))).fetchone()
        if not source or source['project_id'] != project:
            raise PermissionError('Question source belongs to another Project or is missing')
        questions=deepcopy(questions)
        for question in questions:
            if question.get('topic') in {'operation_monitor_policy','operation_monitor_policy_disable'}:
                if interaction_scope!='pm_business':raise PermissionError('Operation policy decisions belong to the PM business flow')
                refs=[x for x in question['source_refs'] if x.get('document_schema')=='operation-monitor-policy.v1']
                if len(refs)!=1:raise ValueError('Policy question needs one exact policy document')
                policy=__import__('assistant_kernel.services.document_runtime',fromlist=['TaskDocumentService']).TaskDocumentService(self.settings).read(project,refs[0])
                action='停用' if question['topic'].endswith('_disable') else '启用'
                question.update(question_text='是否'+action+'以下固定项目提醒规则？\n'+__import__('json').dumps(policy['payload'],ensure_ascii=False,indent=2),
                    options=[{'id':'approve','label':'批准','description':''},{'id':'reject','label':'拒绝','description':''}],recommended_option_id=None,recommendation_reason='由您决定')
            contracts().validate('Question', question)
        if not questions or len({q['question_id'] for q in questions}) != len(questions):
            raise ValueError('A group requires nonempty unique questions')
        policy = POLICIES[interaction_scope]
        roles = {route['interface_role'] for route in routes}
        if policy in {'pm_both','runtime_both'} and roles != {'pm','supervisor'}:
            raise ValueError('Business questions require resolved PM and Supervisor interfaces')
        if policy in {'runtime_pm_primary','supervisor_primary','kernel_management'}:
            expected = {'runtime_pm_primary':'pm','supervisor_primary':'supervisor','kernel_management':'kernel'}[policy]
            if roles != {expected}: raise ValueError('This question scope has a single approved interface role')
        users = {route['user_id'] for route in routes}
        if len(users) != 1 or None in users or '' in users: raise PermissionError('Question projections need one identified owner')
        if source.get('user_id') is not None and source['user_id'] not in users:
            raise PermissionError('Question source and projections have different users')
        if len({(r['channel'],r['chat_id']) for r in routes}) != len(routes):
            raise ValueError('One route cannot represent two different question interfaces')
        version = 'content-'+hashlib.sha256(canonical_json(questions)).hexdigest()
        key = 'question-group:'+owner_id+':'+str(source_message_id)+':'+interaction_scope+':'+version
        existing = await (await connection.execute('SELECT * FROM assistant.messages WHERE idempotency_key=%s', (key,))).fetchone()
        if existing:
            prior = await (await connection.execute('SELECT channel,chat_id,user_id,content_json FROM assistant.messages WHERE message_id=ANY(%s::uuid[])',
                (existing['content_json']['projection_ids'],))).fetchall()
            expected = {(r['channel'],r['chat_id'],r['user_id'],r['interface_role']) for r in routes}
            actual = {(r['channel'],r['chat_id'],r['user_id'],r['content_json']['question_projection']['interface_role']) for r in prior}
            if expected != actual: raise ValueError('Question identity reused with different resolved routes')
            return existing
        identity = str(uuid.uuid4())
        group = {'group_id':identity, 'owner_ref':resource_ref(kind='receipt', identity=owner_id,
                uri=('db://assistant/service_instances/'+service_owner+'#work/'+hashlib.sha256(owner['logical_execution_id'].encode()).hexdigest()) if service_owner else 'db://assistant/runs/'+owner_id, purpose='logical question owner'),
            'owner_role':'kernel' if interaction_scope=='system_configuration' else 'supervisor' if interaction_scope in {'supervisor_consultation','runtime_business'} else 'pm',
            'content_version':version, 'questions':deepcopy(questions),
            'draft':{'revision':0,'cursor':0,'answers':{},'choices':{},'additional_text':''},
            'accepted_answers':[], 'status':'open','blocking':blocking,'interaction_scope':interaction_scope,
            'display_policy':policy, 'runtime_owner':runtime_owner}
        contracts().validate('V12_QuestionGroup', group)
        wrapper = {'schema_version':GROUP_SCHEMA,'protocol_revision':'1.2','group':group,
            'owner_user_id':next(iter(users)), 'source_message_id':str(source_message_id), 'projection_ids':[]}
        wrapper['legacy_messages']=[]
        if service_owner:wrapper.update(service_instance_id=service_owner,logical_execution_id=owner['logical_execution_id'])
        if interaction_scope=='pm_business':
            wrapper['task_id']=str((owner.get('request_json') or {}).get('task_id') or owner_id)
        await connection.execute("""INSERT INTO assistant.messages(message_id,project_id,run_id,channel,chat_id,user_id,
            direction,message_type,reply_to_message_id,content_json,delivery_status,idempotency_key)
            VALUES (%s,%s,%s,'internal',%s,%s,'system','question_group',%s,%s,'received',%s)""",
            (identity,project,database_run_id,owner_id,wrapper['owner_user_id'],source_message_id,Jsonb(wrapper),key))
        ref = resource_ref(kind='message',identity=identity,uri='db://assistant/messages/'+identity,
            purpose='canonical logical question group; projections never own answers')
        for route in routes:
            projection = {'group_ref':ref,'content_version':version,'render_version':'draft-0',
                'interface_role':route['interface_role'],'view_status':'active'}
            content={'schema_version':PROJECTION_SCHEMA,'question_projection':projection,
                'context_visibility':'structured','interaction_seq':int(owner.get('interaction_seq') or 0)}
            if service_owner:content.update(service_instance_id=service_owner,logical_execution_id=owner['logical_execution_id'])
            if route.get('existing_message_id'):
                old=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',(route['existing_message_id'],))).fetchone()
                if (not old or old['project_id']!=project or str(old['run_id'])!=owner_id or old['direction']!='outbound'
                        or (old['channel'],old['chat_id'],old['user_id'])!=(route['channel'],route['chat_id'],route['user_id'])):
                    raise PermissionError('Legacy question projection does not belong to the selected owner/route')
                wrapper['legacy_messages'].append({'message_id':str(old['message_id']),'content_json':deepcopy(old['content_json']),
                    'content_sha256':hashlib.sha256(canonical_json(old['content_json'])).hexdigest()})
                outbound=await (await connection.execute('UPDATE assistant.messages SET content_json=%s WHERE message_id=%s RETURNING *',
                    (Jsonb(content),old['message_id']))).fetchone()
            else:
                outbound = await self.repository._create_outbound_locked(connection, project_id=project,
                session_id=str(owner['session_id']) if owner.get('session_id') else None,run_id=database_run_id,
                channel=route['channel'],chat_id=route['chat_id'],user_id=route['user_id'],message_type='question',
                content=content,
                reply_to_message_id=source_message_id if route.get('is_origin') else None,
                idempotency_key='question-projection:'+identity+':'+route['channel']+':'+route['chat_id'])
            wrapper['projection_ids'].append(str(outbound['message_id']))
        await connection.execute('UPDATE assistant.messages SET content_json=%s WHERE message_id=%s',(Jsonb(wrapper),identity))
        if interaction_scope=='supervisor_consultation':
            await connection.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s',
                (Jsonb({'interaction_scope':'supervisor_consultation','mainline_context_only':True}),owner_id))
        return await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(identity,))).fetchone()

    async def bridge_legacy_locked(self,connection,*,owner,message_id,routes):
        """Explicit adoption/migration only; retain old identity and unsubmitted data."""
        from assistant_kernel.services.questionnaire import is_questionnaire
        from assistant_kernel.services.published_agents import PublishedAgentService
        if not await PublishedAgentService(self.settings).foreground_package_locked(connection,owner,prefer_accepted_head=True):
            raise PermissionError('Legacy bridge requires an explicitly eligible complete Project package')
        old=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',(message_id,))).fetchone()
        if not old or old['project_id']!=owner['project_id'] or str(old['run_id'])!=str(owner['run_id']):
            raise PermissionError('Legacy question has another owner')
        existing=projection_ref(old['content_json'])
        if existing:return await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(existing['id'],))).fetchone()
        body=old['content_json'];draft=body.get('questionnaire') or {}
        if not is_questionnaire(body) or draft.get('status')!='draft':
            raise ValueError('Only an unanswered ordinary questionnaire uses this bridge; historical and dedicated inputs stay readable')
        if str(owner.get('waiting_question_message_id'))!=str(message_id):raise ValueError('Legacy question is no longer the active pending question')
        questions=[]
        for item in body['questions']:
            if item.get('input_schema'):raise ValueError('Dedicated secret/config input must retain its existing controlled entry')
            questions.append({'question_id':item['question_id'],'question_text':item['question_text'],
                'topic':item.get('topic') or '', 'required':item.get('required',True),
                'options':[{'id':option['id'],'label':option['label'],'description':option.get('description') or ''} for option in item.get('options') or []],
                'recommended_option_id':item.get('recommended_option_id'),
                'recommendation_reason':item.get('recommendation_reason') or '', 'source_refs':item.get('source_refs') or []})
        selected=[]
        for route in routes:
            same=(route['channel'],route['chat_id'],route['user_id'])==(old['channel'],old['chat_id'],old['user_id'])
            selected.append({**route,**({'existing_message_id':str(message_id)} if same else {})})
        if sum(bool(route.get('existing_message_id')) for route in selected)!=1:raise ValueError('Legacy primary route must be retained exactly once')
        canonical=await self.create_locked(connection,owner=owner,questions=questions,source_message_id=str(owner['trigger_message_id']),routes=selected)
        wrapper=deepcopy(canonical['content_json'])
        wrapper['group']['draft']={key:deepcopy(draft[key]) for key in ('revision','cursor','answers','choices','additional_text')}
        contracts().validate('V12_QuestionGroup',wrapper['group'])
        await connection.execute('UPDATE assistant.messages SET content_json=%s WHERE message_id=%s',(Jsonb(wrapper),canonical['message_id']))
        await connection.execute("UPDATE assistant.messages SET content_json=jsonb_set(content_json,'{question_projection,render_version}',%s::jsonb) WHERE message_id=ANY(%s::uuid[])",
            (Jsonb('draft-'+str(draft['revision'])),wrapper['projection_ids']))
        return {**dict(canonical),'content_json':wrapper}

    async def hydrate(self, connection, projection):
        if projection['content_json'].get('schema_version') == REFRESH_SCHEMA:
            target = await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',
                (projection['content_json']['target_message_id'],))).fetchone()
            if not target or target['project_id'] != projection['project_id']:
                raise PermissionError('Question refresh target is missing or belongs to another Project')
            hydrated = await self.hydrate(connection,target)
            return {**dict(projection),'content_json':{**hydrated['content_json'],
                'target_message_id':str(target['message_id']),
                'update_external_message_id':target.get('external_message_id'),'ui_update_only':True}}
        ref = projection_ref(projection['content_json'])
        if not ref: return projection
        row = await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(ref['id'],))).fetchone()
        if not row or row['project_id'] != projection['project_id'] or row['content_json'].get('schema_version') != GROUP_SCHEMA:
            raise PermissionError('Question projection points to another owner or missing group')
        group = row['content_json']['group']; view = projection['content_json']['question_projection']
        if view['content_version'] != group['content_version']: raise ValueError('Question projection content version is stale')
        body = render_group(group, sequence=int(projection['content_json'].get('interaction_seq') or 0),
            correlation={'run_id':str(projection['run_id']),'group_id':group['group_id']})
        if view['view_status'] != 'active':
            body['questionnaire']['status'] = 'submitted'
            body['text'] = '此投影已关闭；已保存的问题和答案仍可追溯。\n\n'+body['text']
        return {**dict(projection),'content_json':{**projection['content_json'],**body}}

    async def act_locked(self, connection, *, canonical, event, expected_revision, page, action, value=None):
        """Caller locks owner/group after persisting the original ingress event."""
        wrapper = deepcopy(canonical['content_json']); group = wrapper['group']
        if event['project_id'] != canonical['project_id'] or event['user_id'] != wrapper['owner_user_id']:
            raise PermissionError('Only the original owner may answer this group')
        if group['status'] != 'open': raise ValueError('Question group is no longer open')
        if group['draft']['revision'] != expected_revision: raise ValueError('Question draft changed; refresh the card')
        formal = {answer['question_id'] for answer in group['accepted_answers']}
        if action in {'choice','text'} and 0 <= page < len(group['questions']) and group['questions'][page]['question_id'] in formal:
            raise ValueError('A formal answer requires an explicit correction, not draft editing')
        body = render_group(group)
        updated = advance_questionnaire(body, action=action, page=page, value=value)
        draft = updated['questionnaire']
        group['draft'] = {key:deepcopy(draft[key]) for key in ('revision','cursor','answers','choices','additional_text')}
        # Accepted answers only live in the formal ledger; rendering merged them.
        group['draft']['answers'] = {key:v for key,v in group['draft']['answers'].items() if key not in formal}
        if action == 'submit':
            await self.accept_values_locked(connection, group=group, event=event,
                values=group['draft']['answers'], source_kind='card_submit')
            group['draft']['answers'] = {}; group['draft']['choices'] = {}
        contracts().validate('V12_QuestionGroup',group)
        await self.write_group_locked(connection, canonical['message_id'], wrapper)
        return group

    async def write_group_locked(self, connection, identity, wrapper):
        group = wrapper['group']; contracts().validate('V12_QuestionGroup', group)
        await connection.execute('UPDATE assistant.messages SET content_json=%s,updated_at=now() WHERE message_id=%s',
            (Jsonb(wrapper),identity))
        await connection.execute("""UPDATE assistant.messages
            SET content_json=jsonb_set(jsonb_set(content_json,'{question_projection,render_version}',%s::jsonb),
                '{question_projection,view_status}',CASE WHEN content_json->'question_projection'->>'view_status'='withdrawn'
                    THEN '"withdrawn"'::jsonb ELSE %s::jsonb END),updated_at=now()
            WHERE message_id=ANY(%s::uuid[])""", (Jsonb('draft-'+str(group['draft']['revision'])),
                Jsonb('active' if group['status']=='open' else 'completed' if group['status']=='answered' else group['status']),
                wrapper['projection_ids']))
        await self.refresh_projections_locked(connection,wrapper)

    async def refresh_projections_locked(self,connection,wrapper):
        """Existing Outbox carries reference-only edits, never a new LLM invocation."""
        group=wrapper['group']
        rows=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=ANY(%s::uuid[])',
            (wrapper['projection_ids'],))).fetchall()
        for row in rows:
            await self.repository._create_outbound_locked(connection,project_id=row['project_id'],
                session_id=str(row['session_id']) if row.get('session_id') else None,run_id=str(row['run_id']) if row['run_id'] else None,
                channel=row['channel'],chat_id=row['chat_id'],user_id=row['user_id'],
                message_type='question',content={'schema_version':REFRESH_SCHEMA,'context_visibility':'ui_only',
                    'target_message_id':str(row['message_id']),'render_version':group['draft']['revision'],
                    'view_status':row['content_json']['question_projection']['view_status']},
                reply_to_message_id=None,idempotency_key='question-refresh:'+str(row['message_id'])+':'+
                    str(group['draft']['revision'])+':'+row['content_json']['question_projection']['view_status'])

    async def close_locked(self,connection,*,group_id,status,source_message_id):
        """Only the compiled cancellation/replacement path closes logical questions."""
        if status not in {'cancelled','superseded'}: raise ValueError('Invalid logical question end state')
        row=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',(group_id,))).fetchone()
        source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(source_message_id,))).fetchone()
        if not row or row['content_json'].get('schema_version')!=GROUP_SCHEMA or not source or source['project_id']!=row['project_id']:
            raise PermissionError('Logical question closure requires a same-Project source')
        wrapper=deepcopy(row['content_json'])
        if wrapper['group']['status'] in {'cancelled','superseded'}:
            if wrapper['group']['status']!=status: raise ValueError('Logical question already has a different end state')
            return wrapper
        wrapper['group']['status']=status;wrapper['closure_message_id']=str(source_message_id)
        wrapper['group']['draft']['revision']+=1
        await self.write_group_locked(connection,group_id,wrapper)
        return wrapper

    async def withdraw_views_locked(self,connection,*,owner_id,source_message_id,channel=None,chat_id=None):
        """Ordinary new chat retires displays without cancelling unanswered semantics."""
        rows=await (await connection.execute("SELECT * FROM assistant.messages WHERE run_id=%s AND message_type='question_group' FOR UPDATE",(owner_id,))).fetchall()
        for row in rows:
            if row['content_json'].get('schema_version')!=GROUP_SCHEMA or row['content_json']['group']['status']!='open': continue
            wrapper=deepcopy(row['content_json']);wrapper['view_withdrawal_source_id']=str(source_message_id)
            await connection.execute("UPDATE assistant.messages SET content_json=jsonb_set(content_json,'{question_projection,view_status}','\"withdrawn\"'::jsonb),updated_at=now() WHERE message_id=ANY(%s::uuid[]) AND (%s::text IS NULL OR channel=%s) AND (%s::text IS NULL OR chat_id=%s)",
                (wrapper['projection_ids'],channel,channel,chat_id,chat_id))
            await connection.execute('UPDATE assistant.messages SET content_json=%s WHERE message_id=%s',(Jsonb(wrapper),row['message_id']))
            await self.refresh_projections_locked(connection,wrapper)

    async def restore_view_locked(self,connection,*,owner,projection,source_message_id,key):
        canonical=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',
            (projection_ref(projection['content_json'])['id'],))).fetchone()
        wrapper=deepcopy(canonical['content_json']);group=wrapper['group']
        if group['status']!='open':raise ValueError('Logical question is not waiting for answers')
        body=deepcopy(projection['content_json']);body['interaction_seq']=int(owner['interaction_seq'])+1
        body['question_projection'].update(view_status='active',content_version=group['content_version'])
        restored=await self.repository._create_outbound_locked(connection,project_id=owner['project_id'],session_id=None,
            run_id=str(owner['run_id']),channel=projection['channel'],chat_id=projection['chat_id'],user_id=projection['user_id'],
            message_type='question',content=body,reply_to_message_id=source_message_id,idempotency_key=key)
        if str(restored['message_id']) not in wrapper['projection_ids']:wrapper['projection_ids'].append(str(restored['message_id']))
        await connection.execute('UPDATE assistant.messages SET content_json=%s WHERE message_id=%s',(Jsonb(wrapper),canonical['message_id']))
        await connection.execute('UPDATE assistant.runs SET waiting_question_message_id=%s,interaction_seq=interaction_seq+1 WHERE run_id=%s',
            (restored['message_id'],owner['run_id']))
        return await self.hydrate(connection,restored)

    async def received_event(self, *, project_id, channel, chat_id, user_id, external_event_id, message_id, request):
        """Durable ingress precedes semantic rejection; no missing/late button is relabelled as chat."""
        identity = str(uuid.uuid4()); key = 'question-input:'+channel+':'+external_event_id
        content = {'schema_version':'question-group-input.v1','context_visibility':'ui_only',
            'requested_projection_id':str(message_id),'event_request':deepcopy(request)}
        async with self.repository.database.connection() as connection:
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
            previous = await (await connection.execute('SELECT * FROM assistant.messages WHERE idempotency_key=%s',(key,))).fetchone()
            if previous:
                if (previous['project_id'],previous['channel'],previous['chat_id'],previous['user_id']) != (project_id,channel,chat_id,user_id) or previous['content_json']['event_request'] != request:
                    raise ValueError('Input event identity reused with different ownership/content')
                return previous
            try: valid_id = str(uuid.UUID(str(message_id)))
            except ValueError: valid_id = None
            existing = await (await connection.execute('SELECT project_id,run_id FROM assistant.messages WHERE message_id=%s',(valid_id,))).fetchone() if valid_id else None
            if existing and existing.get('run_id'):
                await self.repository._require_active_run(connection, str(existing['run_id']))
            reply = message_id if existing and existing['project_id']==project_id else None
            row = await (await connection.execute("""INSERT INTO assistant.messages(message_id,project_id,channel,chat_id,user_id,
                external_event_id,direction,message_type,reply_to_message_id,content_json,delivery_status,idempotency_key)
                VALUES (%s,%s,%s,%s,%s,%s,'inbound','action',%s,%s,'received',%s) RETURNING *""",
                (identity,project_id,channel,chat_id,user_id,external_event_id,reply,Jsonb(content),key))).fetchone()
            await connection.commit();return row

    async def record_action(self, *, message_id, project_id, channel, chat_id, user_id,
            external_event_id, revision, page, action, value=None, content_version=None, original_text=None):
        event = await self.received_event(project_id=project_id,channel=channel,chat_id=chat_id,user_id=user_id,
            external_event_id=external_event_id,message_id=message_id,
            request={'revision':revision,'page':page,'action':action,'value':value,
                'content_version':content_version,'original_text':original_text})
        error = None; result = None
        async with self.repository.database.connection() as connection:
            await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('question-event:'+str(event['message_id']),))
            event = await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',(event['message_id'],))).fetchone()
            verdict = event['content_json'].get('acceptance')
            if verdict:
                if verdict['status']=='rejected': raise ValueError(verdict['reason'])
                projection = await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(message_id,))).fetchone()
                return {'message':await self.hydrate(connection,projection),'duplicate':True,'resumed':False,
                    'accepted_into_supervision':False,'input_message_id':str(event['message_id'])}
            await connection.execute('SAVEPOINT question_action')
            try:
                uuid.UUID(str(message_id))
                projection = await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(message_id,))).fetchone()
                if not projection or (projection['project_id'],projection['channel'],projection['chat_id'],projection['user_id']) != (project_id,channel,chat_id,user_id):
                    raise PermissionError('Question projection belongs to another owner or route')
                ref = projection_ref(projection['content_json'])
                if not ref: raise ValueError('This is not a canonical question projection')
                # Linked mainline questions take the Task before the controller
                # and group, matching cancellation and Supervisor settlement.
                owner_basis=await (await connection.execute('SELECT request_json FROM assistant.runs WHERE run_id=%s',(projection['run_id'],))).fetchone()
                basis=(owner_basis or {}).get('request_json') or {}
                root_id=basis.get('task_id') if not basis.get('mainline_context_only') else None
                if root_id and str(root_id)!=str(projection['run_id']):
                    await connection.execute('SELECT run_id FROM assistant.runs WHERE run_id=%s FOR UPDATE',(root_id,))
                service_owner=projection['content_json'].get('service_instance_id')
                if service_owner:
                    instance=await (await connection.execute('SELECT * FROM assistant.service_instances WHERE instance_id=%s FOR UPDATE',(service_owner,))).fetchone()
                    logical=projection['content_json']['logical_execution_id']
                    if not instance or instance['project_id']!=project_id or logical not in instance['state_json']['work_control']['items']:raise PermissionError('Service work owner unavailable')
                    owner={'run_id':None,'project_id':project_id,'service_instance_id':service_owner,'logical_execution_id':logical,
                        'status':'waiting_user','agent_key':'project:'+project_id+':runtime','request_json':{}}
                else:owner = await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s FOR UPDATE',(projection['run_id'],))).fetchone()
                canonical = await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s FOR UPDATE',(ref['id'],))).fetchone()
                if not owner or not canonical or canonical['project_id']!=project_id:
                    raise PermissionError('Question owner unavailable')
                if owner.get('cancel_requested_at') or owner['status'] in {'succeeded','failed','cancelled','dead_letter'}:
                    raise ValueError('Question owner is terminal or cancelled')
                view = projection['content_json']['question_projection']; wrapper=canonical['content_json']
                text_input = action in {'explicit_text','correction','unmapped_text'}
                if (not text_input and view['view_status']!='active' or view['content_version']!=wrapper['group']['content_version']
                        or str(message_id) not in wrapper['projection_ids']):
                    raise ValueError('Question card is stale')
                if text_input:
                    group = deepcopy(wrapper['group'])
                    if content_version != group['content_version']:
                        raise ValueError('Explicit answer refers to an old question content version')
                    if group['status'] not in {'open','answered'} or action=='explicit_text' and group['status']!='open':
                        raise ValueError('Question group is not accepting this kind of answer')
                    if not original_text or action!='unmapped_text' and not isinstance(value,dict):
                        raise ValueError('Explicit answers require the original text and identified question values')
                    if action!='unmapped_text':
                        await self.accept_values_locked(connection,group=group,event=event,values=value,source_kind=action)
                        group['draft']['revision'] += 1
                        for qid in value:
                            group['draft']['answers'].pop(qid,None);group['draft']['choices'].pop(qid,None)
                        wrapper = {**wrapper,'group':group}
                        await self.write_group_locked(connection,canonical['message_id'],wrapper)
                else:
                    group = await self.act_locked(connection,canonical=canonical,event=event,
                        expected_revision=revision,page=page,action=action,value=value)
                resumed = False
                if action=='submit' or text_input:
                    payload={'answers':{a['question_id']:a['value'] for a in group['accepted_answers']},
                        'questions':group['questions'],'additional_text':group['draft']['additional_text'],
                        'question_group_ref':ref,'content_version':group['content_version']}
                    await connection.execute("UPDATE assistant.messages SET message_type=%s,run_id=%s,content_json=content_json || %s WHERE message_id=%s",
                            ('text' if action=='unmapped_text' else 'answer',owner['run_id'],
                            Jsonb({'context_visibility':'semantic','text':original_text,'formal_answers_accepted':action!='unmapped_text',**payload}),event['message_id']))
                    # Runtime work must never be replayed by queuing its owner Run.
                    runtime_input = group['interaction_scope'] in {'runtime_business','runtime_maintenance'}
                    consultation_input=group['interaction_scope'] in {'supervisor_consultation','system_configuration'}
                    in_flight=owner['status'] in {'running','reconciling'}
                    supervisor_owner=str(owner['agent_key']).endswith(':supervisor')
                    next_status = owner['status'] if runtime_input or in_flight else 'queued' if consultation_input or supervisor_owner else 'waiting_supervisor'
                    request_patch={'answer_message_id':str(event['message_id']),'answer':payload,'answer_state':payload['answers']}
                    if consultation_input:
                        request_patch.update(consultation_context_epoch=int((owner['request_json'] or {}).get('consultation_context_epoch') or 0)+1,
                            pending_consultation_input_id=str(event['message_id']))
                    patched = None if service_owner else await (await connection.execute("""UPDATE assistant.runs SET waiting_question_message_id=NULL,
                            status=%s,request_json=request_json || %s,max_attempts=attempt+4,updated_at=now() WHERE run_id=%s RETURNING *""",
                            (next_status,Jsonb(request_patch),owner['run_id']))).fetchone()
                    if group['status']=='open' and not service_owner:
                        await connection.execute('UPDATE assistant.runs SET waiting_question_message_id=%s WHERE run_id=%s',
                            (owner.get('waiting_question_message_id'),owner['run_id']))
                    if not runtime_input and not consultation_input:
                        await self.repository._supervise_answer_locked(connection,patched,str(event['message_id']))
                        resumed = True  # accepted into supervision, never directly queued as a PM Turn
                    elif consultation_input:resumed=True
                result={'message':await self.hydrate(connection,projection),'duplicate':False,'resumed':False,
                    'accepted_into_supervision':resumed,'input_message_id':str(event['message_id']),
                    'unmapped':action=='unmapped_text',
                    'group_ref':ref,'projection_ids':wrapper['projection_ids']}
            except (ValueError,PermissionError) as exc:
                error=exc
                await connection.execute('ROLLBACK TO SAVEPOINT question_action')
            await connection.execute('RELEASE SAVEPOINT question_action')
            # Successful semantic transaction commits its result with the ingress verdict.
            await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
                (Jsonb({'acceptance':{'status':'rejected','reason':str(error)} if error else {'status':'accepted'}}),event['message_id']))
            await connection.commit()
        if error: raise error
        if group['interaction_scope']=='runtime_business' and group['status']=='answered' and not owner.get('service_instance_id'):
            from assistant_kernel.services.business_interactions import BusinessInteractions
            resumed_run=await BusinessInteractions(self.settings,self.repository).resume(str(owner['run_id']))
            if resumed_run:result['runtime_resume_run_id']=str(resumed_run['run_id'])
        return result

    async def record_text(self,*,projection,channel,chat_id,user_id,external_event_id,text):
        """Accept named answers (or one unambiguous question); preserve extra input."""
        async with self.repository.database.connection() as connection:
            row=await self.hydrate(connection,projection)
            ref=projection_ref(projection['content_json'])
            canonical=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(ref['id'],))).fetchone()
        group=canonical['content_json']['group'];questions={q['question_id'] for q in group['questions']}
        values={};correction=False;extra=[]
        for line in text.splitlines():
            match=re.fullmatch(r'\s*(?:(更正|correction)\s+)?([\w.-]+)\s*[:：]\s*(.+)',line)
            if match and match[2] in questions:
                if match[2] in values:
                    return await self.record_action(message_id=str(projection['message_id']),project_id=projection['project_id'],
                        channel=channel,chat_id=chat_id,user_id=user_id,external_event_id=external_event_id,
                        revision=-1,page=-1,action='invalid_repeated_question',original_text=text)
                values[match[2]]=match[3];correction=correction or bool(match[1])
            elif line.strip(): extra.append(line)
        if not values and len(questions)==1 and group['status']=='open' and text.strip():
            values={next(iter(questions)):text.strip()};extra=[]
        if values:
            return await self.record_action(message_id=str(projection['message_id']),project_id=projection['project_id'],
                channel=channel,chat_id=chat_id,user_id=user_id,external_event_id=external_event_id,
                revision=-1,page=-1,action='correction' if correction else 'explicit_text',value=values,
                content_version=group['content_version'],original_text=text)
        return await self.record_action(message_id=str(projection['message_id']),project_id=projection['project_id'],
            channel=channel,chat_id=chat_id,user_id=user_id,external_event_id=external_event_id,
            revision=-1,page=-1,action='unmapped_text',content_version=group['content_version'],original_text=text)

    async def accept_values_locked(self, connection, *, group, event, values, source_kind):
        """Partial explicit text can accept only identified questions; no guessing."""
        allowed = {question['question_id'] for question in group['questions']}
        accepted = {answer['question_id'] for answer in group['accepted_answers']}
        if not values or set(values)-allowed: raise ValueError('Answer names unknown or no questions')
        if source_kind=='correction':
            if not set(values)<=accepted: raise ValueError('Correction requires an existing accepted answer')
        elif set(values)&accepted: raise ValueError('Question already accepted; use an explicit correction')
        now = datetime.now(timezone.utc).isoformat()
        for qid, value in values.items():
            if value is None or isinstance(value,str) and not value.strip(): raise ValueError('Formal answer must have a value')
            answer = {'question_id':qid,'content_version':group['content_version'],'value':deepcopy(value),
                'source_kind':source_kind,'source_message_ref':resource_ref(kind='message',identity=str(event['message_id']),
                    uri='db://assistant/messages/'+str(event['message_id']),purpose='original identified answer input'),
                'accepted_at':now,'supersedes_answer_ref':None}
            if source_kind=='correction':
                prior=next(a for a in reversed(group['accepted_answers']) if a['question_id']==qid)
                answer['supersedes_answer_ref']=resource_ref(kind='receipt',
                    identity='answer-'+hashlib.sha256(canonical_json(prior)).hexdigest(),
                    uri=prior['source_message_ref']['uri']+'#answer/'+qid,
                    sha256=hashlib.sha256(canonical_json(prior)).hexdigest(),purpose='preserved prior accepted answer')
            contracts().validate('QuestionAnswer',answer);group['accepted_answers'].append(answer)
            question=next(q for q in group['questions'] if q['question_id']==qid)
            if question.get('topic') in {'operation_monitor_policy','operation_monitor_policy_disable'}:
                from assistant_kernel.repositories.core import approval_decision
                decision=approval_decision({'text':value})
                if decision:
                    subject=next(x for x in question['source_refs'] if x.get('document_schema')=='operation-monitor-policy.v1')
                    record={'decision':decision,'document_ref':subject,'policy_action':'deactivate' if question['topic'].endswith('_disable') else 'activate',
                        'question_group_id':group['group_id'],'content_version':group['content_version']}
                    await connection.execute("UPDATE assistant.messages SET content_json=jsonb_set(content_json,'{document_approvals}',COALESCE(content_json->'document_approvals','[]'::jsonb) || %s::jsonb) WHERE message_id=%s",
                        (Jsonb([record]),event['message_id']))
        required = {question['question_id'] for question in group['questions'] if question['required']}
        if required <= {answer['question_id'] for answer in group['accepted_answers']}: group['status']='answered'
