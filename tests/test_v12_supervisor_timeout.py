from copy import deepcopy
import json,uuid
import pytest
from assistant_kernel.services.contracts import contracts,ContractError
from assistant_kernel.services.supervisor_config import legacy_bindings,resolve_supervisor_configuration
from assistant_kernel.services.goal_supervisor import GoalSupervisorService
from assistant_kernel.repositories import CoreRepository
from test_core4 import core_settings
from test_v12_foundations import seed,example_config


def test_c18_changes_only_configurable_supervisor_ceiling():
    registry=contracts();original=example_config()
    assert [original['supervisor_defaults'][t]['timeout_seconds'] for t in ('intent','normal','critical')]==[90,180,240]
    changed=deepcopy(original)
    for tier in ('intent','normal','critical'):changed['supervisor_defaults'][tier]['timeout_seconds']=900
    registry.validate('SystemConfig',changed)
    changed['supervisor_defaults']['normal']['timeout_seconds']=901
    with pytest.raises(ContractError):registry.validate('SystemConfig',changed)
    rows=json.loads((registry.root/'config-field-catalog.json').read_text())
    assert {row['schema']['maximum'] for row in rows if row['path'] in
        ['/supervisor_defaults/'+t+'/timeout_seconds' for t in ('intent','normal','critical')]}=={900}


async def test_explicit_600_second_control_binding_preserves_global_defaults(core_settings):
    service=await seed(core_settings);state=await service.state()
    before=service._config(state['desired_ref'])
    base=GoalSupervisorService(core_settings,CoreRepository(core_settings)).configuration('demo')
    specs=legacy_bindings(base)
    for item in specs:
        if item['path']=='/supervisor_defaults/normal/timeout_seconds':item['override_value']=600
    actual=await resolve_supervisor_configuration(core_settings,project_id='demo',run_id=str(uuid.uuid4()),base=base,bindings=specs)
    assert actual['models']['main_evaluation']['timeout_seconds']==600
    assert service._config((await service.state())['desired_ref'])==before
