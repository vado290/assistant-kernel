"""Confirmed, narrowly scoped daily operations on already registered schedules."""
from copy import deepcopy
from psycopg.types.json import Jsonb
from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.document_runtime import TaskDocumentService,same_resource_ref


async def validate_actions(c,project,actions):
    contracts().validate('ScheduleOperationActions',actions)
    if len({a['schedule_id'] for a in actions})!=len(actions):raise ValueError('One action per Schedule in a confirmed operation')
    for action in actions:
        row=await (await c.execute('SELECT * FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',
            (project,action['schedule_id']))).fetchone()
        if not row or row['control_revision']!=action['expected_control_revision'] or not same_resource_ref(row['definition_ref'],action['definition_ref']):
            raise PermissionError('Daily schedule operation needs the actual current registration and revision')
        if action['action']!='update' and action['changes']:raise ValueError('Only update has configuration changes')
    return deepcopy(actions)


def action_for(task,schedule_id):
    actions=(task['request_json'] or {}).get('schedule_actions')
    if not actions:return None
    action=next((a for a in actions if a['schedule_id']==schedule_id),None)
    if action is None:raise PermissionError('Schedule is outside this confirmed daily operation')
    return action


def verify_update(task,old,new):
    action=action_for(task,old['schedule_id'])
    if action is None:return
    if action['action']!='update':raise PermissionError('This daily operation does not authorize a definition update')
    expected={**old,**action['changes']}
    # Provenance may cite the new confirmation; executable identity and all other fields remain fixed.
    if any(new[k]!=v for k,v in expected.items() if k!='source_refs'):
        raise PermissionError('Daily operation changed more than the confirmed schedule configuration')


async def gates(c,settings,task,mode):
    if mode!='operate':raise PermissionError('Daily schedule operations cannot change engineering phase')
    actions=task['request_json']['schedule_actions']
    done=task['request_json'].get('schedule_action_receipts',{})
    if len(done)<len(actions):await validate_actions(c,task['project_id'],[a for a in actions if a['schedule_id'] not in done])
    return {'schedule_actions':actions}


async def complete(c,settings,repository,task,source):
    from assistant_kernel.services.task_queue import ProjectTaskQueue
    request=task['request_json'];documents=TaskDocumentService(settings)
    proofs=request.get('schedule_action_receipts',{});actions=request['schedule_actions']
    if set(proofs)!={a['schedule_id'] for a in actions}:raise PermissionError('Daily schedule operation lacks actual control receipts')
    for action in actions:
        proof=proofs[action['schedule_id']];body=documents.read(task['project_id'],proof)['payload']
        row=await (await c.execute('SELECT control_json FROM assistant.project_schedules WHERE project_id=%s AND schedule_id=%s',
            (task['project_id'],action['schedule_id']))).fetchone()
        if (body['action']!=action['action'] or not row
                or not any(same_resource_ref(x['receipt_ref'],proof) for x in row['control_json'].get('commands',{}).values())):
            raise PermissionError('Daily schedule result is not an actual accepted control')
    queue=ProjectTaskQueue(settings,repository);meta=await queue._meta(c,task['project_id'])
    if meta.get('active_task_id') not in {None,str(task['run_id'])}:raise PermissionError('Another foreground Task owns the Project')
    await queue._patch_meta(c,task['project_id'],{'active_task_id':None})
    await c.execute("UPDATE assistant.runs SET status='succeeded',ended_at=now(),request_json=request_json || %s,result_json=COALESCE(result_json,'{}'::jsonb) || %s WHERE run_id=%s",
        (Jsonb({'task_activation':'closed','task_phase':'completed','pending_task_completion':False,'state_revision':int(request.get('state_revision',0))+1}),
         Jsonb({'schedule_operation_completed':True,'engineering_acceptance':'not_applicable','control_receipt_refs':list(proofs.values())}),task['run_id']))
    return {'task_completed':True,'schedule_operation_completed':True,'control_receipt_refs':list(proofs.values())}
