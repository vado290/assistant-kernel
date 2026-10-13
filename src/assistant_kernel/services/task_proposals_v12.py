"""New work can be prepared without taking the active Task or starting a PM."""
import uuid

from psycopg.types.json import Jsonb

from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.task_queue import ProjectTaskQueue,WORK_SCHEMA


class TaskProposalsV12:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.queue=ProjectTaskQueue(settings,repository)

    async def create_locked(self,connection,*,control,context,draft,priority='normal'):
        if priority not in {'normal','urgent'}:raise ValueError('Unsupported work priority')
        project=control['project_id'];refs=context['user_message_refs']
        if not refs:raise PermissionError('A work proposal needs original human input')
        source=await (await connection.execute('SELECT * FROM assistant.messages WHERE message_id=%s',(refs[-1]['id'],))).fetchone()
        if not source or source['project_id']!=project or source['direction']!='inbound' or not source['user_id']:
            raise PermissionError('New work must have an actual same-Project user source')
        if source['content_json'].get('context_visibility') in {'ui_only','trace_only'}:
            raise PermissionError('A button/navigation event alone is not a new business goal')
        schedule_actions=draft.get('schedule_actions')
        if schedule_actions:
            if draft['proposed_work_mode']!='operate':raise PermissionError('Daily schedule actions require Operate')
            from assistant_kernel.services.schedule_operations import validate_actions
            await validate_actions(connection,project,schedule_actions)
        elif draft['proposed_work_mode']!='requirements':raise PermissionError('New work without accepted phase documents starts at requirements')
        metadata=await self.queue._meta(connection,project)
        from assistant_kernel.repositories.core import canonical_sha256
        key='v12-work-proposal:'+str(control['run_id'])+':'+canonical_sha256([draft['objective'],refs,priority])
        existing=await (await connection.execute('SELECT * FROM assistant.messages WHERE idempotency_key=%s',(key,))).fetchone()
        if existing:
            return await (await connection.execute('SELECT * FROM assistant.runs WHERE run_id=%s',(existing['run_id'],))).fetchone()
        task_id=str(uuid.uuid4());work_id=str(uuid.uuid4())
        source_ref=resource_ref(kind='message',identity=str(source['message_id']),uri='db://assistant/messages/'+str(source['message_id']),purpose='original proposed work source')
        content={'schema_version':WORK_SCHEMA,'goal':draft['objective'],'text':draft['objective'],'priority':priority,
            'task_kind':'delivery','work_status':'proposed','source_message_id':str(source['message_id']),
            'source_ref':source_ref,'report_window':None,'work_idempotency_key':key,'bound_task_id':task_id}
        await connection.execute("""INSERT INTO assistant.messages(message_id,project_id,channel,chat_id,user_id,direction,
            message_type,reply_to_message_id,content_json,delivery_status,idempotency_key)
            VALUES (%s,%s,%s,%s,%s,'system','work_request',%s,%s,'received',%s)""",
            (work_id,project,source['channel'],source['chat_id'],source['user_id'],source['message_id'],Jsonb(content),key))
        request={'protocol_revision':'1.2','task_protocol':'single-agent.v2','phase_protocol':'five-stage.v1',
            'task_root_run_id':task_id,'task_goal':draft['objective'],'task_activation':'queued','task_kind':'delivery',
            'current_stage':'requirements','work_mode':'requirements','task_turn_count':0,'state_revision':0,
            'supervisor_context_epoch':0,'task_phase':'waiting_supervisor','design_required':True,'accepted_documents':{},
            'input_source_message_ids':[ref['id'] for ref in refs],'work_request_message_id':work_id,
            'task_scope_proposal':{'kind':'new_task','producer_control_run_id':str(control['run_id']),
                'expected_active_task_id':metadata.get('active_task_id'),
                'disposition':'suspend_current_then_start' if priority=='urgent' else 'after_current_task','confirmed':False}}
        if schedule_actions:request.update(schedule_actions=schedule_actions,current_stage='operate',work_mode='operate',design_required=False)
        task=await (await connection.execute("""INSERT INTO assistant.runs(run_id,project_id,agent_key,trigger_message_id,
            run_type,status,conversation_key,request_json,executor_json,idempotency_key)
            VALUES (%s,%s,%s,%s,'chat','waiting_supervisor',%s,%s,'{}',%s) RETURNING *""",
            (task_id,project,'project:'+project+':manager',source['message_id'],'project-task:'+project+':'+task_id,Jsonb(request),key+':task'))).fetchone()
        task=await self.repository._persist_scheduling(connection,task)
        from assistant_kernel.services.published_agents import PublishedAgentService
        package=await PublishedAgentService(self.settings).foreground_package_locked(connection,task,prefer_accepted_head=True)
        if not package:raise PermissionError('New work requires the complete accepted Project method package')
        task=await (await connection.execute('UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s RETURNING *',
            (Jsonb({'pending_runtime_package_ref':package[0]}),task_id))).fetchone()
        return task

    async def confirm_locked(self,connection,*,task,event_ref):
        proposal=task['request_json']['task_scope_proposal'];project=task['project_id']
        metadata=await self.queue._meta(connection,project)
        current=metadata.get('active_task_id')
        if current not in {None,proposal['expected_active_task_id']}:
            raise PermissionError('Foreground Task changed after the new-work package was displayed')
        work_id=task['request_json']['work_request_message_id']
        await connection.execute('UPDATE assistant.messages SET content_json=content_json || %s WHERE message_id=%s',
            (Jsonb({'work_status':'queued' if current else 'activated','task_id':str(task['run_id']),
                'scope_confirmation_ref':event_ref,
                'interrupts_task_id':current if proposal['disposition']=='suspend_current_then_start' else None}),work_id))
        scope={**proposal,'confirmed':True,'confirmation_ref':event_ref}
        request_patch={'task_scope_proposal':scope,'task_activation':'queued' if current else 'active',
            'task_phase':'queued' if current else 'working'}
        await connection.execute('UPDATE assistant.runs SET status=%s,request_json=request_json || %s WHERE run_id=%s',
            ('waiting_supervisor' if current else 'queued',Jsonb(request_patch),task['run_id']))
        if not current:await self.queue._patch_meta(connection,project,{'active_task_id':str(task['run_id'])})
        return {'disposition':'queued_after_current' if current else 'activated','active_task_id':current or str(task['run_id'])}
