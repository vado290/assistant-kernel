"""Resolve explicit per-Actor bindings once at a legal invocation boundary."""
from dataclasses import replace
from datetime import datetime,timezone
import hashlib
import os

from assistant_kernel.config.system_config import resolve_config_bindings
from assistant_kernel.services.contracts import canonical_json,contracts
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.system_config import SystemConfigService


class InvocationConfiguration:
    def __init__(self,settings):
        self.settings=settings;self.service=SystemConfigService(settings)

    async def resolve(self,config,*,project_id,run_id,invocation_id,pinned_ref=None):
        if pinned_ref is not None:
            snapshot=TaskDocumentService(self.settings).read(project_id,pinned_ref)['payload']
            if snapshot['owner_ref']['id']!=run_id:raise ValueError('Pinned configuration belongs to another Run')
            values={path.rsplit('/',1)[1]:value for path,value in snapshot['selected_values'].items()}
            resolved=replace(config,provider=values['provider'],model=values['model'],
                reasoning_effort=values['reasoning_effort'],timeout_seconds=values['timeout_seconds'],
                context_policy=values['context_policy'],model_context_window_tokens=values['custom_context_window_tokens'],
                model_auto_compact_token_limit=values['native_auto_compact_token_limit'])
            resolved=replace(resolved,config_sha256=hashlib.sha256(canonical_json({
                'published_config_sha256':config.config_sha256,'selected_values_sha256':snapshot['subset_sha256']})).hexdigest())
            return resolved,values,pinned_ref
        state=await self.service.state()
        if not state:raise ValueError('Published invocation requires the imported system configuration')
        expected={'/agent_defaults/'+name for name in contracts().library['$defs']['SystemConfig']['properties']['agent_defaults']['properties']}
        if {b['path'] for b in config.config_bindings}!=expected:
            raise ValueError('Invocation must declare every inherited or overridden Agent default')
        owner=resource_ref(kind='receipt',identity=run_id,uri='db://assistant/runs/'+run_id,
            purpose='actual Run whose invocation '+invocation_id+' resolves these defaults')
        snapshot=resolve_config_bindings(self.service._config(state['desired_ref']),state['desired_ref'],list(config.config_bindings),
            owner_ref=owner,captured_at=datetime.now(timezone.utc).isoformat(),effective_group_refs=[])
        ref=TaskDocumentService(self.settings).write_scoped_revision(project_id=project_id,scope='run',object_id='agent-defaults',
            run_id=run_id,document_kind='resolved-config-snapshot',document_schema='resolved-system-config.v1',
            payload=snapshot,status='submitted',actor_type='kernel',actor_id='invocation-config',
            reason='Fixed values selected for invocation '+invocation_id,source_refs=[state['desired_ref']])
        values={path.rsplit('/',1)[1]:value for path,value in snapshot['selected_values'].items()}
        resolved=replace(config,provider=values['provider'],model=values['model'],
            reasoning_effort=values['reasoning_effort'],timeout_seconds=values['timeout_seconds'],
            context_policy=values['context_policy'],model_context_window_tokens=values['custom_context_window_tokens'],
            model_auto_compact_token_limit=values['native_auto_compact_token_limit'])
        resolved=replace(resolved,config_sha256=hashlib.sha256(canonical_json({
            'published_config_sha256':config.config_sha256,'selected_values_sha256':snapshot['subset_sha256']})).hexdigest())
        return resolved,values,ref

    def check_role_budget(self,config,values):
        import tiktoken
        os.environ.setdefault('TIKTOKEN_CACHE_DIR',str(self.settings.data_root/'caches/tiktoken'))
        # Text-only BPE is a preflight estimate, not provider-billed usage. No
        # local language model, extra summary call or automatic truncation.
        encoding=tiktoken.get_encoding('o200k_base')
        text=config.role_text+'\n\n'+'\n\n'.join(config.skill_texts)
        tokens=len(encoding.encode(text,disallowed_special=()))
        if tokens>values['role_hard_limit_tokens']:
            raise ValueError('Fixed Role/Skill text exceeds the declared role token budget; revise the accepted selection, do not truncate')
        return {'estimated_role_tokens':tokens,'tokenizer':'tiktoken/o200k_base',
            'measurement_quality':'estimate','soft_limit_exceeded':tokens>values['role_soft_limit_tokens'],
            'hard_limit_tokens':values['role_hard_limit_tokens'],'truncated':False}

    async def record_use(self,*,snapshot_ref,receipt_payload):
        snapshot=TaskDocumentService(self.settings).read(
            snapshot_ref['uri'].removeprefix('vault://').split('/',1)[0],snapshot_ref)['payload']
        state=await self.service.state()
        if not state or state['desired_ref']!=snapshot['desired_config_ref']:return None
        payload={'schema_version':'config-consumer-receipt.v1','consumer':'agent_invocation',
            'source_ref':state['desired_ref'],'resolved_snapshot_ref':snapshot_ref,**receipt_payload}
        encoded=canonical_json(payload);digest=hashlib.sha256(encoded).hexdigest()
        path=self.settings.data_root/'runtime/config-consumers/agent-invocation'/(digest+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.read_bytes()!=encoded:raise ValueError('Consumer receipt changed')
        path.write_bytes(encoded)
        ref=resource_ref(kind='receipt',identity='invocation-config-'+digest,uri=str(path),sha256=digest,
                         purpose='actual invocation parameters; explicit overrides remain separate')
        if any(b['mode']=='override' for b in snapshot['bindings']):
            return ref  # Do not claim an overridden global default was consumed.
        group='agent_defaults:next_invocation'
        current=next(g for g in state['groups'] if g['group']==group)
        if current['status']!='applied' or current['effective_ref']!=state['desired_ref']:
            await self.service.settle_group(expected_revision=state['revision'],group=group,evidence_ref=ref,
                applied=True,reason='Invocation resolver consumed all Agent default bindings; this does not hot-replace existing packages')
        return ref
