"""Database-only scheduler hooks for existing Run/Unit transactions."""
from psycopg.types.json import Jsonb


async def installed(c):
    return bool((await (await c.execute("SELECT to_regclass('assistant.project_schedules') AS name")).fetchone())['name'])


async def cancel_unstarted(c,project,schedule_id):
    await c.execute("""UPDATE assistant.runs SET status='cancelled',cancel_requested_at=now(),ended_at=now(),updated_at=now()
        WHERE project_id=%s AND request_json#>>'{schedule_binding,schedule_id}'=%s AND status='queued'
        AND NOT request_json ? 'schedule_dispatch_permit'""",(project,schedule_id))


async def pause_unit_schedules(c,project,unit_id,reason):
    if not await installed(c):return
    rows=await (await c.execute("""UPDATE assistant.project_schedules SET state='paused',control_revision=control_revision+1,
        next_fire_at=NULL,control_json=control_json || %s,updated_at=now()
        WHERE project_id=%s AND unit_id=%s AND state IN ('enabled','exhausted') RETURNING schedule_id""",
        (Jsonb({'pause_reason':reason}),project,unit_id))).fetchall()
    for row in rows:await cancel_unstarted(c,project,row['schedule_id'])


async def authorize_claim(c,run):
    binding=(run['request_json'] or {}).get('schedule_binding')
    if not binding:return True
    tick=await (await c.execute('SELECT * FROM assistant.schedule_ticks WHERE tick_id=%s',(binding['tick_id'],))).fetchone()
    if not tick or tick['project_id']!=run['project_id'] or str(tick['run_id'])!=str(run['run_id']):
        raise PermissionError('Scheduled Run has no matching Tick')
    permit=run['request_json'].get('schedule_dispatch_permit')
    if permit:
        if permit['tick_id']!=binding['tick_id']:raise PermissionError('Scheduled dispatch permit changed')
        return True
    schedule=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',
        (run['project_id'],binding['schedule_id']))).fetchone()
    states={'enabled','exhausted'} if tick['origin']=='scheduled' else {'disabled','enabled','paused','exhausted'}
    if (not schedule or schedule['state'] not in states or schedule['control_revision']!=binding['control_revision']
            or tick['settlement_status']!='pending'):
        await c.execute("UPDATE assistant.runs SET status='cancelled',cancel_requested_at=now(),ended_at=now(),updated_at=now() WHERE run_id=%s AND status='queued'",(run['run_id'],))
        return False
    run['request_json']['schedule_dispatch_permit']={'tick_id':binding['tick_id'],'control_revision':binding['control_revision']}
    return True
