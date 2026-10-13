from pathlib import Path
import pytest
from test_core4 import core_settings,prepare_bindable_project
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.storage import ProjectRuntimeManager


def test_history_resolution_never_recreates_butler_runtime(core_settings):
    runtime=ProjectRuntimeManager(core_settings.data_root)
    root=runtime.describe('system').vault_root
    assert runtime.resolve_vault_file('system','reports/history.txt')==root/'reports/history.txt'
    assert not root.exists()
    root.mkdir(parents=True);(root/'history.txt').write_text('old evidence')
    assert runtime.resolve_vault_file('system','history.txt').read_text()=='old evidence'
    assert not (root/'runtime').exists()
    for project in ['system','butler-maintenance']:
        with pytest.raises(PermissionError,match='retired'):runtime.ensure(project)


def test_residual_intake_config_does_not_register_an_agent(core_settings):
    prepare_bindable_project(core_settings,'kernel')
    path=core_settings.data_root/'vaults/kernel/agents/intake/config.yaml'
    path.parent.mkdir(parents=True);path.write_text('invalid old intake config')
    agents=AgentFileLoader(core_settings)
    assert [x.agent_key for x in agents.list_agents('kernel')]==['project:kernel:manager']
    assert agents.list_agents('system')==[]
    with pytest.raises(PermissionError,match='retired'):agents.load('kernel','project:kernel:intake')
