"""Exercise the real PM processor up to model launch, independently of P3 output."""
from dataclasses import replace
import json
import uuid

import pytest
from psycopg.types.json import Jsonb
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.run_processor import RunProcessor
from test_core4 import core_settings
from test_v12_foundations import seed
from test_v12_packages import candidate


async def test_pm_launch_has_current_scope_and_actual_mcp_discovery(core_settings):
    await seed(core_settings)
    _, config, _ = candidate(core_settings)
    config = replace(config, allowed_tools=('get_authorized_workspace',))
    loader = AgentFileLoader(core_settings)
    uri, sha = loader.snapshot('demo', config)
    repo = CoreRepository(core_settings)
    incoming = await repo.ingest_message(project_id='demo', agent_key=config.agent_key,
        channel='test', chat_id='pm-assembly', user_id='owner', external_event_id='pm-assembly',
        external_message_id=None, content={'text': 'Inspect the explicitly assembled capabilities'})
    async with repo.database.connection() as c:
        await c.execute('UPDATE assistant.runs SET executor_json=%s WHERE run_id=%s',
            (Jsonb({'config_snapshot_uri': uri, 'config_bundle_hash': sha}), incoming['run']['run_id']))
        await c.commit()
    worker = str(uuid.uuid4()); run = await repo.claim_run('pm-assembly', worker, 120)
    requests = []
    class ModelBoundaryReached(RuntimeError):
        pass
    class Harness:
        name = 'isolated-model-boundary'
        async def run_turn(self, request):
            requests.append(request)
            raise ModelBoundaryReached('No model or business output is simulated by this assembly test')
    with pytest.raises(ModelBoundaryReached):
        await RunProcessor(core_settings, repo, Harness()).process(run, worker)
    request = requests[0]
    assert request.sandbox == 'danger-full-access' and request.permission_profile is None
    root = core_settings.data_root / 'vaults/demo'
    receipt = json.loads((root / 'runtime/invocation-assemblies' / (request.invocation_id + '.json')).read_text())
    assert receipt['mcp_discovery'][0]['requested_tools'] == ['get_authorized_workspace']
    assert receipt['mcp_discovery'][0]['status'] == 'declared'
    assert receipt['mcp_discovery'][0]['loaded_tools'] == []
    assert receipt['workspace_scope_ref']['uri'] == request.scope_context['KERNEL_SCOPE_SNAPSHOT_URI']
    assert 'invocation-plan-' in receipt['plan_ref']['uri']
    actual = await repo.get_run(str(run['run_id']))
    from assistant_kernel.services.document_runtime import TaskDocumentService
    context = TaskDocumentService(core_settings).read('demo', actual['executor_json']['context_manifest_ref'])['payload']
    assert context['resolved_config_ref'] == receipt['resolved_config_snapshot_ref']
    assert context['file_authority_ref']['uri'] == request.scope_context['KERNEL_SCOPE_SNAPSHOT_URI']
