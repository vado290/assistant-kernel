import uuid
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.services.invocation_config import InvocationConfiguration
from assistant_kernel.services.invocation_context import write_invocation_context
from assistant_kernel.services.public_background import PublicBackgroundService
from test_core4 import core_settings
from test_v12_foundations import seed, source
from test_v12_background import payload
from test_v12_packages import candidate


async def test_context_manifest_records_only_selected_background_and_actual_missing_map(core_settings):
    await seed(core_settings)
    _, config, _ = candidate(core_settings)
    run_id = str(uuid.uuid4())
    _, _, resolved = await InvocationConfiguration(core_settings).resolve(config,
        project_id='demo', run_id=run_id, invocation_id='context-proof')
    background = PublicBackgroundService(core_settings.data_root, core_settings)
    selected = background.read_project_context('demo', fields=['user.language'])
    pinned = resource_ref(kind='source', identity='fixture-fixed-package',
        uri='vault://demo/runtime/config.json', sha256='a'*64, purpose='isolated package fixture')
    ref = write_invocation_context(core_settings, project_id='demo', run_id=run_id,
        invocation_id='context-proof', config_ref=pinned, resolved_config_ref=resolved, scope_ref=pinned,
        source_message_ids=[], accepted_documents={}, skill_refs=[], public_context=selected, semantic_context=None)
    actual = TaskDocumentService(core_settings).read('demo', ref)['payload']
    assert actual['coverage'] == 'partial' and len(actual['missing_refs']) == 1
    assert actual['background_selections']==[]
    assert actual['resolved_config_ref'] == resolved
    assert actual['prior_stage_chat_loaded'] is False
