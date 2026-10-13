"""Record the context actually selected for one model invocation."""
from datetime import datetime, timezone
import hashlib

from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.services.contracts import contracts


def write_invocation_context(settings, *, project_id, run_id, invocation_id, config_ref,
        resolved_config_ref, scope_ref, source_message_ids, accepted_documents,
        skill_refs, public_context, semantic_context):
    now = datetime.now(timezone.utc).isoformat()
    required = []
    for value in accepted_documents.values():
        for ref in value if isinstance(value, list) else [value]:
            if ref is not None:
                TaskDocumentService(settings).read(project_id, ref)
                required.append(ref)
    selections = []
    for document in public_context.get('documents', []):
        # Legacy two-file background stays readable, but cannot masquerade as a
        # DB-accepted v1.2 selection before its explicit migration.
        if document.get('ref') is None:
            raise ValueError('Published context requires an accepted public-background reference')
        selections.append({'document_ref': document['ref'], 'facts': document['facts'],
            'selected_field_ids': public_context['selected_field_ids'],
            'selection_sha256': public_context['selection_sha256'], 'as_of': now})
    missing = []
    if semantic_context and semantic_context.get('map_uri'):
        map_ref = resource_ref(kind='source', identity='project-map:' + project_id,
            uri=semantic_context['map_uri'], sha256=semantic_context['map_sha256'],
            purpose='Map used to select this invocation context')
        if semantic_context.get('map_freshness') != 'current':
            missing.append(map_ref)
    else:
        map_ref = resource_ref(kind='source', identity='project-map:' + project_id,
            uri=f'vault://{project_id}/knowledge/map/latest.json', purpose='Map missing; no index contents claimed')
        missing.append(map_ref)
    payload = {'owner_ref': resource_ref(kind='receipt', identity=run_id,
            uri='db://assistant/runs/' + run_id, purpose='Invocation ' + invocation_id),
        'as_of': now, 'source_message_refs': [resource_ref(kind='message', identity=str(mid),
            uri='db://assistant/messages/' + str(mid), purpose='actual invocation input') for mid in source_message_ids],
        'required_document_refs': required, 'optional_loaded_refs': list(skill_refs),
        'profile_ref': config_ref, 'context_policy_ref': config_ref, 'file_authority_ref': scope_ref,
        'project_map_ref': map_ref, 'coverage': 'partial' if missing else 'complete',
        'missing_refs': missing, 'soft_context_filters': ['Project-selected background fields',
            'Fresh stage starts from accepted artifacts; no previous-stage raw chat'],
        'background_selections': selections, 'resolved_config_ref': resolved_config_ref,
        'prior_stage_chat_loaded': False, 'runtime_unit_refs': []}
    contracts().validate('V12_Active_ContextManifest', payload)
    return TaskDocumentService(settings).write_scoped_revision(project_id=project_id, scope='run',
        run_id=run_id, object_id='ctx-' + hashlib.sha256(invocation_id.encode()).hexdigest(), document_kind='context-manifest',
        document_schema='context-manifest.v1', payload=payload, status='submitted', actor_type='kernel',
        actor_id='invocation-context', reason='Actual selected context; coverage is not business acceptance',
        source_refs=[config_ref, resolved_config_ref, scope_ref])
