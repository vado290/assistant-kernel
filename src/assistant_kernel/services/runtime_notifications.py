"""One per-attempt failure obligation in the existing Messages/Outbox."""
from pathlib import Path
import yaml

from assistant_kernel.services.python_evidence import PythonEvidence


class RuntimeNotifications:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository

    async def pm_route(self,connection,project,*,role='pm',channel=None):
        agent='project:'+project+(':'+role if role=='supervisor' else ':manager')
        rows=await (await connection.execute("""WITH latest AS (
            SELECT DISTINCT ON(channel,chat_id,user_id) * FROM assistant.messages
            WHERE direction='system' AND message_type='route_binding'
            ORDER BY channel,chat_id,user_id,created_at DESC,message_id DESC)
            SELECT channel,chat_id,user_id FROM latest WHERE project_id=%s
              AND content_json->>'status'='active'
              AND COALESCE(content_json->>'agent_key',%s)=%s""",
            (project,agent,agent))).fetchall()
        path=self.settings.data_root/'config/channel-bindings.yaml'
        configured=(yaml.safe_load(path.read_text('utf-8')) or {}).get('bindings',[]) if path.is_file() else []
        rows+= [x for x in configured if x.get('project_id')==project
            and x.get('agent_key','project:'+project+':manager')==agent]
        routes={(str(x['channel']),str(x['chat_id']),x.get('user_id')) for x in rows
            if x.get('channel') in {'discord','feishu'} and (channel is None or x.get('channel')==channel)}
        # A missing or ambiguous route remains a durable obligation, never a
        # guessed fallback to another Project or to the Supervisor channel.
        return next(iter(routes)) if len(routes)==1 else None

    async def failure_locked(self,connection,run,receipt):
        key=f"python-failure:{run['run_id']}:{receipt['attempt']}"
        if await (await connection.execute('SELECT 1 FROM assistant.messages WHERE idempotency_key=%s',(key,))).fetchone():return
        evidence=PythonEvidence(self.settings,run);stderr=evidence.root/'stderr.log'
        stderr.touch(exist_ok=True)
        error=receipt.get('stop_reason') or 'ProcessFailure'
        payload={'notification_id':key,'kind':'script_failure','project_id':run['project_id'],
            'script_name':Path((run['request_json'] or {}).get('source_uri') or 'python').name,
            'runtime_unit_ref':(run['request_json'] or {}).get('runtime_unit_ref'),
            'run_ref':evidence.run_ref,'attempt':receipt['attempt'],'instance_ref':None,
            'occurred_at':receipt['ended_at'],'captured_error':error,'returncode':receipt['exit_code'],
            'controlled_stop':False,'stderr_ref':evidence.file_ref(stderr,'captured attempt stderr'),
            # Do not mirror raw stderr, user input, or secret values into public channels.
            'error_excerpt':f"{error}; exit_code={receipt['exit_code']}; cleanup={receipt['cleanup']['status']}",
            'dedup_key':key,'target_role':'pm','delivery_state_owner':'messages_outbox'}
        ref=evidence.document('script-failure-notice',payload,[payload['stderr_ref']])
        from assistant_kernel.services.operation_records import OperationRecords
        from assistant_kernel.repositories import change_ledger
        if await change_ledger.enabled(connection):
            await OperationRecords(self.settings,self.repository).record_event_locked(connection,project=run['project_id'],
                event_key='python-failure:'+str((run['request_json'] or {}).get('logical_execution_id') or run['run_id']),scene='python_execution',source_ref=ref,
                target_ref=(run['request_json'] or {}).get('runtime_unit_ref') or (run['request_json'] or {}).get('manifest_ref'),
                occurred_at=receipt['ended_at'],summary=payload['error_excerpt'])
        route=await self.pm_route(connection,run['project_id'])
        await self.repository._create_outbound_locked(connection,project_id=run['project_id'],session_id=None,
            run_id=str(run['run_id']),channel=route[0] if route else 'internal',
            chat_id=route[1] if route else 'pending-project-pm',user_id=route[2] if route else None,
            message_type='reply',content={'interaction_kind':'script_failure','notice_ref':ref,
                'attempt':receipt['attempt'],'pending_pm_route':not bool(route),
                'text':f"Python 执行失败：{payload['script_name']}\nRun：{run['run_id']} / attempt {receipt['attempt']}\n"
                    +payload['error_excerpt']+'\n本次日志已保存，结果尚未通过；后续处理以该 Run 状态为准。'},
            reply_to_message_id=None,idempotency_key=key)

    async def route_pending(self):
        async with self.repository.database.connection() as connection:
            rows=await (await connection.execute("""SELECT * FROM assistant.messages WHERE channel='internal'
                AND direction='outbound' AND delivery_status='pending'
                AND content_json->>'pending_pm_route'='true' FOR UPDATE SKIP LOCKED LIMIT 100""")).fetchall()
            for row in rows:
                route=await self.pm_route(connection,row['project_id'])
                if route:await connection.execute("""UPDATE assistant.messages SET channel=%s,chat_id=%s,user_id=%s,
                    content_json=jsonb_set(content_json,'{pending_pm_route}','false'::jsonb),updated_at=now()
                    WHERE message_id=%s""",(*route,row['message_id']))
            await connection.commit()
