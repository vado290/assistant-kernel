"""Bounded deterministic scans. A caller-held session lock owns all dispatch writes."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from psycopg.types.json import Jsonb

from assistant_kernel.services.schedules import Schedules,DEFAULTS,TERMINAL
from assistant_kernel.services.schedule_time import next_time,instant
from assistant_kernel.services.runtime_admission import RuntimeAdmission
from assistant_kernel.services.system_config import SystemConfigService
from assistant_kernel.services.document_runtime import resource_ref,same_resource_ref
from assistant_kernel.services.v12_evidence import V12Evidence

LEADER_KEY='kernel-business-scheduler:v1'


class Scheduler:
    def __init__(self,settings,repository):
        self.settings=settings;self.repository=repository;self.schedules=Schedules(settings,repository)
        self.settlement_cursor=None

    async def configuration(self):
        service=SystemConfigService(self.settings);state=await service.state()
        if not state:return deepcopy(DEFAULTS),None
        return {**DEFAULTS,**service._config(state['desired_ref'])['kernel'].get('scheduler',{})},state['desired_ref']

    async def acquire(self,c):
        row=await (await c.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired',(LEADER_KEY,))).fetchone()
        await c.commit();return row['acquired']

    async def quarantine(self,c,observed,error):
        """One invalid definition cannot stop other Projects' clocks."""
        from assistant_kernel.services.runtime_notifications import RuntimeNotifications
        async with c.transaction():
            await RuntimeAdmission.lock(c)
            row=await self.schedules.locked(c,observed['project_id'],observed['schedule_id'])
            if row['control_revision']!=observed['control_revision']:return
            await c.execute("UPDATE assistant.project_schedules SET state='paused',next_fire_at=NULL,control_revision=control_revision+1,control_json=control_json || %s WHERE project_id=%s AND schedule_id=%s",
                (Jsonb({'pause_reason':'definition_unavailable:'+type(error).__name__}),row['project_id'],row['schedule_id']))
            route=await RuntimeNotifications(self.settings,self.repository).pm_route(c,row['project_id'])
            await self.repository._create_outbound_locked(c,project_id=row['project_id'],session_id=None,run_id=None,
                channel=route[0] if route else 'internal',chat_id=route[1] if route else 'pending-project-pm',user_id=route[2] if route else None,
                message_type='reply',reply_to_message_id=None,idempotency_key=f"schedule-invalid:{row['project_id']}:{row['schedule_id']}:{row['control_revision']}",
                content={'interaction_kind':'schedule_configuration_failure','pending_pm_route':not bool(route),
                    'text':f"定时任务 {row['schedule_id']} 已暂停：固定定义不可用（{type(error).__name__}），请检查版本及来源。"})

    async def recover(self,c,cutoff):
        """Move future cursor first, preserving bounded historical backfill work."""
        rows=await (await c.execute("SELECT * FROM assistant.project_schedules WHERE state='enabled' AND next_fire_at<%s ORDER BY project_id,schedule_id",(cutoff,))).fetchall()
        await c.commit()
        for observed in rows:
            try:
                definition,_=self.schedules.definition(observed['project_id'],observed['definition_ref'])
                upcoming=next_time(definition['rule'],definition['timezone'],cutoff)
            except (ValueError,PermissionError,FileNotFoundError,KeyError) as exc:
                await self.quarantine(c,observed,exc);continue
            async with c.transaction():
                await RuntimeAdmission.lock(c)
                row=await self.schedules.locked(c,observed['project_id'],observed['schedule_id'])
                if row['control_revision']!=observed['control_revision'] or row['state']!='enabled' or row['next_fire_at']!=observed['next_fire_at']:continue
                state=deepcopy(row['control_json']);state.setdefault('missed_windows',[]).append({
                    'cursor':row['next_fire_at'].isoformat(),'until':cutoff.isoformat(),'definition_ref':row['definition_ref'],'control_revision':row['control_revision']})
                await c.execute('UPDATE assistant.project_schedules SET next_fire_at=%s,state=%s,control_json=%s,updated_at=now() WHERE project_id=%s AND schedule_id=%s',
                    (upcoming,'enabled' if upcoming else 'exhausted',Jsonb(state),row['project_id'],row['schedule_id']))

    async def scan(self,c,*,now=None,configuration=None):
        configuration=configuration or (await self.configuration())[0]
        now=instant(now) if now else (await (await c.execute('SELECT clock_timestamp() AS value')).fetchone())['value']
        await c.commit()
        await self.settle(c,configuration['batch_size'])
        if configuration['enabled']:
            rows=await (await c.execute("SELECT * FROM assistant.project_schedules WHERE state='enabled' AND next_fire_at<=%s ORDER BY next_fire_at,project_id,schedule_id LIMIT %s",(now,configuration['batch_size']))).fetchall()
            await c.commit()
            for observed in rows:
                project=observed['project_id'];due=observed['next_fire_at'];reason=None;prepared=None
                try:definition,unit=self.schedules.definition(project,observed['definition_ref'])
                except (ValueError,PermissionError,FileNotFoundError,KeyError) as exc:
                    await self.quarantine(c,observed,exc);continue
                if (now-due).total_seconds()>configuration['late_grace_seconds']:reason='missed_time'
                if reason is None:
                    try:
                        await V12Evidence(self.settings,self.repository).verify(project,observed['authorization_refs']+[unit['approved_execution_scope_ref']])
                        if definition['action']=='run':prepared=await self.schedules.prepare_execution(project,observed,definition,unit,due.isoformat())
                    except (ValueError,PermissionError,FileNotFoundError) as exc:reason='admission_rejected:'+type(exc).__name__
                async with c.transaction():
                    await RuntimeAdmission.lock(c)
                    target=await (await c.execute('SELECT * FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(project,observed['unit_id']))).fetchone()
                    row=await self.schedules.locked(c,project,observed['schedule_id'])
                    if row['state']!='enabled' or row['control_revision']!=observed['control_revision'] or row['next_fire_at']!=due:continue
                    if prepared and not prepared.get('duplicate') and prepared['parent']['control_revision']!=target['control_revision']:reason=reason or 'target_control_changed'
                    await self.schedules.dispatch_locked(c,row,definition,unit,due,prepared=prepared,skip_reason=reason)
                    upcoming=next_time(definition['rule'],definition['timezone'],due)
                    state=deepcopy(row['control_json'])
                    # A delayed scan never replays a backlog within the grace window.
                    if upcoming and upcoming<=now:
                        state.setdefault('missed_windows',[]).append({'cursor':upcoming.isoformat(),'until':(now+timedelta(microseconds=1)).isoformat(),
                            'definition_ref':row['definition_ref'],'control_revision':row['control_revision']})
                        upcoming=next_time(definition['rule'],definition['timezone'],now)
                    paused=bool(reason and reason.startswith('admission_rejected'))
                    if paused:state['pause_reason']=reason;upcoming=None
                    await c.execute('UPDATE assistant.project_schedules SET next_fire_at=%s,state=%s,control_json=%s,updated_at=now() WHERE project_id=%s AND schedule_id=%s',
                        (upcoming,'paused' if paused else 'enabled' if upcoming else 'exhausted',Jsonb(state),project,row['schedule_id']))
            await self.backfill(c,configuration['batch_size'])
        return {'last_scan_at':now.isoformat(),'enabled':configuration['enabled']}

    async def backfill(self,c,limit):
        rows=await (await c.execute("SELECT * FROM assistant.project_schedules WHERE jsonb_array_length(COALESCE(control_json->'missed_windows','[]'::jsonb))>0 ORDER BY updated_at,project_id,schedule_id LIMIT %s",(limit,))).fetchall()
        await c.commit();remaining=limit
        for observed in rows:
            if remaining<=0:break
            # Resolve fixed documents and compute the bounded historical batch before the public lock.
            prepared=[];windows=deepcopy(observed['control_json'].get('missed_windows',[]))
            while windows and len(prepared)<remaining:
                window=windows[0];due=instant(window['cursor']);until=instant(window['until'])
                definition,unit=self.schedules.definition(observed['project_id'],window['definition_ref'])
                prepared.append((deepcopy(window),due,definition,unit))
                following=next_time(definition['rule'],definition['timezone'],due)
                if following is None or following>=until:windows.pop(0)
                else:window['cursor']=following.isoformat()
            async with c.transaction():
                await RuntimeAdmission.lock(c)
                await c.execute('SELECT unit_id FROM assistant.project_runtime_units WHERE project_id=%s AND unit_id=%s FOR UPDATE',(observed['project_id'],observed['unit_id']))
                row=await self.schedules.locked(c,observed['project_id'],observed['schedule_id'])
                if row['control_json'].get('missed_windows')!=observed['control_json'].get('missed_windows'):continue
                state=deepcopy(row['control_json']);state['missed_windows']=windows
                for window,due,definition,unit in prepared:
                    historical={**row,'definition_ref':window['definition_ref'],'control_revision':window['control_revision']}
                    await self.schedules.dispatch_locked(c,historical,definition,unit,due,skip_reason='missed_time')
                    remaining-=1
                await c.execute('UPDATE assistant.project_schedules SET control_json=%s,updated_at=now() WHERE project_id=%s AND schedule_id=%s',(Jsonb(state),row['project_id'],row['schedule_id']))

    async def settle(self,c,limit):
        rows=await (await c.execute("SELECT * FROM assistant.schedule_ticks WHERE settlement_status='pending' AND (%s::uuid IS NULL OR tick_id>%s::uuid) ORDER BY tick_id LIMIT %s",
            (self.settlement_cursor,self.settlement_cursor,limit))).fetchall()
        if not rows and self.settlement_cursor is not None:
            self.settlement_cursor=None
            rows=await (await c.execute("SELECT * FROM assistant.schedule_ticks WHERE settlement_status='pending' ORDER BY tick_id LIMIT %s",(limit,))).fetchall()
        if rows:self.settlement_cursor=str(rows[-1]['tick_id'])
        await c.commit()
        for observed in rows:
            status=None;result_ref=None;reason=observed['reason']
            if observed['run_id']:
                run=await self.repository.get_run(str(observed['run_id']))
                if run['status'] not in TERMINAL:continue
                attempt=(run['executor_json'] or {}).get('python_attempt') or {}
                if attempt.get('process_identity') and (attempt.get('cleanup') or {}).get('status')!='verified':continue
                if attempt.get('effect_status')=='unknown':continue
                result_ref=attempt.get('settlement_ref')
                if result_ref:
                    settlement=self.schedules.documents.read(observed['project_id'],result_ref)['payload']
                    if settlement.get('effect_status')=='unknown':continue
                status='failed' if run['status']=='dead_letter' else run['status']
                result_ref=result_ref or resource_ref(kind='receipt',identity=str(run['run_id']),uri='db://assistant/runs/'+str(run['run_id']),purpose='actual scheduled Run outcome')
            elif observed['control_request']:
                target=await (await c.execute('''SELECT u.* FROM assistant.project_runtime_units u JOIN assistant.project_schedules s
                    ON s.project_id=u.project_id AND s.unit_id=u.unit_id WHERE s.project_id=%s AND s.schedule_id=%s''',
                    (observed['project_id'],observed['schedule_id']))).fetchone()
                await c.commit()
                command=target['control_json'].get('commands',{}).get(observed['control_request']['idempotency_key'])
                if not command:continue
                result_ref=command['receipt_ref'];receipt=self.schedules.documents.read(observed['project_id'],result_ref)['payload']
                if receipt['status']=='accepted':continue
                if receipt['status'] not in {'settled','failed'}:continue
                if receipt['status']=='failed':
                    instance_ref=receipt.get('instance_ref')
                    if instance_ref:
                        snapshot=self.schedules.documents.read(observed['project_id'],instance_ref)['payload']
                        actual=await (await c.execute('SELECT cleanup_status FROM assistant.service_instances WHERE project_id=%s AND instance_id=%s',
                            (observed['project_id'],snapshot['instance_id']))).fetchone()
                        await c.commit()
                        if not actual or actual['cleanup_status']!='verified':continue
                    status='failed'
                elif command.get('schedule_outcome'):status=command['schedule_outcome']
                elif reason in {'cancel_requested','superseded_by_manual_control'}:status='cancelled'
                else:status='succeeded'
            if status:
                async with c.transaction():
                    await RuntimeAdmission.lock(c)
                    tick=await (await c.execute("UPDATE assistant.schedule_ticks SET settlement_status=%s,result_ref=%s,settled_at=now() WHERE tick_id=%s AND settlement_status='pending' RETURNING *",(status,Jsonb(result_ref),observed['tick_id']))).fetchone()
                    if tick:await self.schedules.notify_locked(c,tick)
