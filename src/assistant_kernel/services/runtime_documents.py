"""Small producer for the runtime documents already defined by v1.2."""
import hashlib
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.contracts import contracts


async def resolve_runtime_config(settings,task,bindings):
    from datetime import datetime,timezone
    from assistant_kernel.services.system_config import SystemConfigService
    from assistant_kernel.config.system_config import resolve_config_bindings
    if any(x['path'].split('/')[1] not in {'python_defaults','service_defaults','resource_limits'} for x in bindings):raise PermissionError('Runtime bindings are limited to public execution fields')
    service=SystemConfigService(settings);state=await service.state()
    if not state:raise PermissionError('Runtime configuration has not been accepted')
    identity=str(task['run_id']);source=resource_ref(kind='receipt',identity=identity,uri='db://assistant/runs/'+identity,purpose='actual runtime authoring owner')
    resolved=resolve_config_bindings(service._config(state['desired_ref']),state['desired_ref'],bindings,owner_ref=source,
        captured_at=datetime.now(timezone.utc).isoformat(),effective_group_refs=[])
    ref=TaskDocumentService(settings).write_scoped_revision(project_id=task['project_id'],scope='project',object_id='runtime-config-'+identity,
        document_kind='resolved-config-snapshot',document_schema='resolved-system-config.v1',payload=resolved,status='settled',
        actor_type='kernel',actor_id='runtime-config',reason='Actual accepted defaults selected for runtime authoring; not execution approval',source_refs=[state['desired_ref'],source])
    return {'snapshot_ref':ref,'bindings':resolved['bindings'],'selected_values':resolved['selected_values']}


def write_runtime_document(settings,task,kind,payload):
    if (task['request_json'] or {}).get('schedule_actions'):
        from assistant_kernel.services.schedule_operations import action_for,verify_update
        if kind!='kernel-schedule-definition':raise PermissionError('Daily schedule operations cannot author code, permissions or RuntimeUnits')
        action=action_for(task,payload['schedule_id'])
        old=TaskDocumentService(settings).read(task['project_id'],action['definition_ref'])['payload']
        verify_update(task,old,payload)
    names={'python-script-manifest':'script_id','python-service-manifest':'service_id','runtime-unit-definition':'runtime_unit_id','activation-report':None,'kernel-schedule-definition':'schedule_id'}
    if kind not in names:raise PermissionError('Unknown runtime document kind')
    phase=task['request_json'].get('current_stage')
    if phase not in {'build','operate'} or kind=='activation-report' and phase!='operate':raise PermissionError('Runtime document is outside the current phase')
    schema=kind+'.v1';contracts().validate(contracts().document_definition(schema),payload)
    project=task['project_id'];identity=str(task['run_id'])
    if kind in {'runtime-unit-definition','kernel-schedule-definition'} and payload['project_id']!=project:raise PermissionError('Definition belongs to another Project')
    if kind=='kernel-schedule-definition':
        from assistant_kernel.services.schedule_time import preview
        preview(payload['rule'],payload['timezone'])
    if kind=='python-script-manifest' and payload['ownership']['project_id']!=project:raise PermissionError('Manifest belongs to another Project')
    source=resource_ref(kind='receipt',identity=identity,uri='db://assistant/runs/'+identity,purpose='actual scoped document authoring Task')
    shared=kind!='activation-report'
    return TaskDocumentService(settings).write_scoped_revision(project_id=project,scope='project' if shared else 'task',
        object_id='runtime-'+hashlib.sha256(str(payload[names[kind]] if shared else identity).encode()).hexdigest()[:24],
        task_id=None if shared else identity,run_id=None if shared else identity,
        document_kind=kind,document_schema=schema,payload=payload,status='submitted',actor_type='pm',actor_id=task['agent_key'],
        reason='Runtime document candidate; writing does not register, enable or accept it',source_refs=[source])
