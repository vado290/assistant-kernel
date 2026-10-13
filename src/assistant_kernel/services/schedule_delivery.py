"""Connect fixed stage obligations to actual schedule registration and execution."""
from assistant_kernel.services.schedules import Schedules
from assistant_kernel.services.document_runtime import same_resource_ref
from assistant_kernel.services.v12_evidence import references


def validate_handoffs(settings,repository,project,design,implementation):
    plans=design.get('schedule_plans') or []
    if not plans:return []
    service=Schedules(settings,repository);handoffs=implementation.get('schedule_handoffs') or []
    found={}
    for ref in handoffs:
        definition,unit=service.definition(project,ref)
        if definition['schedule_id'] in found:raise ValueError('Duplicate Schedule handoff')
        found[definition['schedule_id']]=(ref,definition,unit)
    if set(found)!={p['schedule_id'] for p in plans}:raise ValueError('Implementation Schedule handoffs differ from accepted design')
    for plan in plans:
        ref,definition,unit=found[plan['schedule_id']]
        if unit['runtime_unit_id']!=plan['runtime_unit_id'] or any(definition[k]!=plan[k] for k in ('rule','timezone','action','inputs')):
            raise PermissionError('Schedule handoff changed the approved rule, target or inputs')
    return [(plan,*found[plan['schedule_id']]) for plan in plans]


async def verify_activation_locked(c,settings,repository,project,design,implementation,activation):
    selected=validate_handoffs(settings,repository,project,design,implementation)
    evidence=list(references(activation));armed=[]
    now=(await (await c.execute('SELECT clock_timestamp() AS value')).fetchone())['value']
    for plan,ref,definition,unit in selected:
        row=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',(project,plan['schedule_id']))).fetchone()
        if not row or row['state'] not in {'enabled','exhausted'} or not same_resource_ref(row['definition_ref'],ref):
            raise PermissionError('Schedule obligation has no actual enabled registration of the reviewed definition')
        controls=[]
        service=Schedules(settings,repository)
        for command in row['control_json'].get('commands',{}).values():
            proof=command['receipt_ref'];body=service.documents.read(project,proof)['payload']
            if body['state']=='enabled' and body['control_revision']==row['control_revision'] and same_resource_ref(body['definition_ref'],ref):controls.append(proof)
        selected_controls=[x for x in controls if any(same_resource_ref(x,y) for y in evidence)]
        if not selected_controls:
            raise PermissionError('Activation omitted actual Schedule control evidence')
        if plan['requires_first_run']:
            ticks=await (await c.execute("SELECT tick_id,definition_ref FROM assistant.schedule_ticks WHERE project_id=%s AND schedule_id=%s AND origin='scheduled' AND settlement_status='succeeded'",(project,plan['schedule_id']))).fetchall()
            if not any(same_resource_ref(t['definition_ref'],ref) and any(x['uri']=='db://assistant/schedule_ticks/'+str(t['tick_id']) for x in evidence) for t in ticks):
                raise PermissionError('Schedule acceptance requires an actual successful timed occurrence, not a manual substitute')
        elif row['state']=='enabled' and row['next_fire_at'] and row['next_fire_at']>now:
            armed.append({'runtime_unit_id':unit['runtime_unit_id'],'criterion_ids':plan['acceptance_ids'],'receipt_refs':selected_controls})
        else:
            ticks=await (await c.execute("SELECT 1 FROM assistant.schedule_ticks WHERE project_id=%s AND schedule_id=%s AND definition_ref=%s AND settlement_status='succeeded' LIMIT 1",
                (project,plan['schedule_id'],__import__('psycopg.types.json',fromlist=['Jsonb']).Jsonb(ref)))).fetchone()
            if not ticks:raise PermissionError('The configured schedule is no longer armed and has no successful occurrence')
    return armed
