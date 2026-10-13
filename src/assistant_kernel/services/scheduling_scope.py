"""Current scheduler capability and historical external-backend config projection."""
from copy import deepcopy

SCHEDULING_NOTICE = (
    '# Kernel scheduling capability (0.31)\n'
    'Kernel owns once, interval and calendar schedules. Use fixed Project ScheduleDefinitions and schedule tools. '
    'Build creates and validates candidates; confirmed Operate registers and controls them. '
    'Registration, enablement, dispatch and successful business execution require distinct actual receipts. '
    'Downtime misses and overlapping unfinished work are skipped; notify every outcome. '
    'Default pause/retire cancels unstarted work and drains the current occurrence. '
    'Manual Unit stop/pause and failure protection pause related schedules. '
    'Never create an n8n workflow, shell timer, detached loop or sleeping business Run as a substitute. '
    'Historical C26 deferral is superseded for new adopted tasks, not retroactive business authorization.'
)

RETIRED_CONFIG_PATHS = (
    '/n8n', '/schedule_defaults', '/service_defaults/periodic_restart',
    '/infrastructure/services/n8n', '/infrastructure/host_ports/n8n',
    '/infrastructure/storage/n8n', '/infrastructure/storage/n8n_files',
)


def retired_config_path(path):
    return any(path==prefix or path.startswith(prefix+'/') for prefix in RETIRED_CONFIG_PATHS)


def active_configuration(value):
    """Project an archived config into current fields without editing its bytes/Ref."""
    result=deepcopy(value)
    for path in RETIRED_CONFIG_PATHS:
        parts=path.strip('/').split('/');parent=result
        for part in parts[:-1]:parent=parent.get(part,{})
        parent.pop(parts[-1],None)
    return result
