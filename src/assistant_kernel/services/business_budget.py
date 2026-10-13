"""A fixed receipt of the existing ledger, never a second budget counter."""
from datetime import datetime,timezone
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref


async def snapshot(settings,repository,run):
    async with repository.database.connection() as c:
        rows=await (await c.execute("SELECT * FROM assistant.model_invocations WHERE project_id=%s AND actor_role='business' AND metadata_json->>'logical_execution_id'=%s ORDER BY started_at",
            (run['project_id'],run['request_json']['logical_execution_id']))).fetchall()
    payload={'project_id':run['project_id'],'logical_execution_id':run['request_json']['logical_execution_id'],
        'runtime_unit_ref':run['request_json']['runtime_unit_ref'],'resource_policy_ref':run['request_json'].get('runtime_resource_policy_ref'),
        'model_calls':len(rows),'elapsed_seconds':sum(float(x['metadata_json'].get('elapsed_seconds',x['metadata_json']['reserved_seconds'])) for x in rows),
        'known_input_tokens':sum(x['input_tokens'] for x in rows if x['metadata_json'].get('usage_status')=='actual'),
        'known_output_tokens':sum(x['output_tokens'] for x in rows if x['metadata_json'].get('usage_status')=='actual'),
        'unknown_usage_calls':sum(x['metadata_json'].get('usage_status')!='actual' for x in rows),
        'pending_calls':sum(x['status']=='running' or x['metadata_json'].get('outcome_status')=='unknown' for x in rows),
        'invocation_refs':[resource_ref(kind='receipt',identity=x['invocation_id'],uri='db://assistant/model_invocations/'+x['invocation_id'],purpose='actual cumulative business invocation') for x in rows],
        'captured_at':datetime.now(timezone.utc).isoformat()}
    return TaskDocumentService(settings).write_scoped_revision(project_id=run['project_id'],scope='project',
        object_id='budget-'+str(run['run_id'])+'-a'+str(run['attempt']),document_kind='runtime-budget-snapshot',document_schema='runtime-budget-snapshot.v1',
        payload=payload,status='settled',actor_type='kernel',actor_id='business-ledger',reason='Known usage sums plus explicit unknown usage; resume retains original ledger',source_refs=payload['invocation_refs'])
