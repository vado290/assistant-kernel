"""Explicit Supervisor defaults; legacy values never silently become inherited."""
from copy import deepcopy
from datetime import datetime, timezone

from assistant_kernel.config.system_config import leaf_values, resolve_config_bindings
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.services.system_config import SystemConfigService

TIERS = {'intent': 'intent_panel', 'normal': 'main_evaluation', 'critical': 'critical_evaluation'}


def legacy_bindings(configuration):
    values = {name: configuration['models'][legacy] for name, legacy in TIERS.items()}
    values.update({name: configuration[name] for name in
        ('max_task_turns', 'daily_input_tokens', 'daily_output_tokens')})
    bindings = [{'path': path, 'mode': 'override', 'override_value': value}
        for path, value in leaf_values(values, '/supervisor_defaults').items()]
    bindings.extend({'path': '/supervisor_defaults/' + name, 'mode': 'inherit', 'override_value': None}
        for name in ('retry_delays_seconds', 'format_correction_attempts'))
    return bindings


async def resolve_supervisor_configuration(settings, *, project_id, run_id, base, bindings):
    service = SystemConfigService(settings); state = await service.state()
    if not state:
        raise ValueError('Supervisor inheritance requires an accepted system configuration')
    config = service._config(state['desired_ref'])
    expected = set(leaf_values(config['supervisor_defaults'], '/supervisor_defaults'))
    if {item['path'] for item in bindings} != expected or len(bindings) != len(expected):
        raise ValueError('Supervisor must declare every inherited or overridden default')
    snapshot = resolve_config_bindings(config, state['desired_ref'], bindings,
        owner_ref=resource_ref(kind='receipt', identity=run_id, uri='db://assistant/runs/' + run_id,
            purpose='Supervisor control invocation'), captured_at=datetime.now(timezone.utc).isoformat(),
        effective_group_refs=[])
    result = deepcopy(base)
    for path, value in snapshot['selected_values'].items():
        parts = path.split('/')[2:]
        if parts[0] in TIERS:
            result['models'][TIERS[parts[0]]][parts[1]] = value
        else:
            result[parts[0]] = value
    ref = TaskDocumentService(settings).write_scoped_revision(project_id=project_id, scope='run',
        object_id='supervisor-defaults', run_id=run_id, document_kind='resolved-config-snapshot',
        document_schema='resolved-system-config.v1', payload=snapshot, status='submitted', actor_type='kernel',
        actor_id='supervisor-config', reason='Pin Supervisor tiers and budgets before the first invocation',
        source_refs=[state['desired_ref']])
    result['resolved_supervisor_config_ref'] = ref
    return result
