"""Consume accepted values and record actual per-field use; never claim an external restart."""
import hashlib,json,logging,os,signal,threading,time
import psycopg
from datetime import datetime,timezone
from pathlib import Path
from assistant_kernel.config.system_config import leaf_values
from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.system_config import SystemConfigService,STATE_KEY

def accepted_configuration(settings):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(host=settings.postgres_host,port=settings.postgres_port,dbname=settings.postgres_database,
        user=settings.postgres_user,password=settings.postgres_password.get_secret_value(),row_factory=dict_row,connect_timeout=10) as c:
        row=c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(STATE_KEY,)).fetchone()
    return (row['value'],SystemConfigService(settings)._config(row['value']['desired_ref'])) if row else (None,None)

def effective_providers(settings):
    """Provider public fields come from accepted configuration; secret values stay in their store."""
    from pydantic import SecretStr
    from assistant_kernel.config.settings import ProviderCredential
    result=settings.load_provider_secrets().model_copy(deep=True);state,config=accepted_configuration(settings)
    if not config or 'providers' not in config:return result
    stored=settings.load_provider_secrets()
    providers={}
    for name,public in config['providers'].items():
        base=stored.providers.get(name) or ProviderCredential()
        values={k:v for k,v in public.items() if k not in {'api_key_ref','model_names','chat_models'}}
        reference=public.get('api_key_ref');key=SecretStr('')
        if reference:
            if not reference.startswith('secret://providers/') or not reference.endswith('/api-key'):raise PermissionError('Unsupported provider secret reference')
            owner=reference.removeprefix('secret://providers/').removesuffix('/api-key')
            if owner in stored.providers:key=stored.providers[owner].api_key
        values.update(api_key=key,models=base.models.model_copy(update=public.get('model_names') or {}),chat_models=public.get('chat_models') or {})
        providers[name]=base.model_copy(update=values)
    result.providers=providers;return result

def _temporary_database_error(error):
    """Retry connection availability, never authentication or invalid configuration."""
    state = error.sqlstate
    if state:
        return state.startswith('08') or state in {'57P01', '57P02', '57P03', '53300'}
    message = str(error).lower()
    if any(text in message for text in (
        'authentication failed', 'no password supplied', 'no pg_hba.conf entry',
        'does not exist', 'invalid connection option', 'invalid sslmode',
    )):
        return False
    return any(text in message for text in (
        'connection refused', 'timeout expired', 'connection timed out',
        'database system is starting up', 'server closed the connection unexpectedly',
        'network is unreachable', 'no route to host', 'temporary failure in name resolution',
        'could not receive data from server', 'connection reset by peer',
    ))


def _startup_configuration(settings, component):
    # Called before the event loop can make progress. Handle stop signals here,
    # then restore each entrypoint's existing handlers before normal operation.
    previous = {}
    def stop(signum, frame):
        raise SystemExit(0 if signum == signal.SIGTERM else 130)
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop)
    delay = 1
    attempts = 0
    try:
        while True:
            try:
                result = accepted_configuration(settings)
                if attempts:
                    logging.info('%s database ready; continuing startup', component)
                return result
            except psycopg.OperationalError as exc:
                if not _temporary_database_error(exc):
                    raise
                attempts += 1
                # Do not log connection strings or credential-bearing exceptions.
                logging.warning('%s waiting for database; attempt=%d retry_in=%ds',
                                component, attempts, delay)
                time.sleep(delay)
                delay = min(10, delay * 2)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def startup_settings(settings,component):
    """Bootstrap DB/path identity stays external until an explicit migration; listeners use accepted values."""
    state,config=_startup_configuration(settings,component)
    if not state:return settings # Original unconfigured reader remains usable for initial setup.
    updates={'api_host':config['kernel']['api']['listen_host'],'api_port':config['kernel']['api']['port'],
        'mcp_host':config['kernel']['mcp']['listen_host'],'mcp_port':config['kernel']['mcp']['port'],
        'worker_poll_seconds':config['kernel']['queue']['poll_seconds'],'worker_lease_seconds':config['kernel']['queue']['lease_seconds'],
        'health_heartbeat_seconds':config['kernel']['health']['heartbeat_seconds'],'health_stale_after_seconds':config['kernel']['health']['stale_after_seconds'],
        'python_timeout_seconds':config['python_defaults']['timeout_seconds']}
    for key,value in updates.items():setattr(settings,key,value)
    logging.getLogger().setLevel(config['logging']['level'])
    used={}
    if component in {'api','mcp'}:used.update(leaf_values(config['kernel'][component],'/kernel/'+component))
    if component in {'worker','maintenance','discord','feishu','scheduler'}:used.update(leaf_values(config['kernel']['health'],'/kernel/health'))
    if component in {'worker','maintenance'}:used.update(leaf_values(config['kernel']['queue'],'/kernel/queue'))
    used['/logging/level']=config['logging']['level']
    from assistant_kernel.worker.process_control import process_start_ticks
    payload={'consumer':component,'pid':os.getpid(),'process_start_ticks':process_start_ticks(os.getpid()),
        'source_ref':state['desired_ref'],'used_values':used,'observed_at':datetime.now(timezone.utc).isoformat()}
    path=settings.data_root/'runtime/config-consumers/startup'/str(os.getpid())/'receipt.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(canonical_json(payload));return settings


class ConfigConsumers:
    def __init__(self,settings):self.settings=settings;self.service=SystemConfigService(settings);self.seen={};self.cached=None

    async def record(self,consumer,source_ref,values):
        """Only mark a group applied when every field has matching actual consumer evidence."""
        from psycopg.types.json import Jsonb
        state=await self.service.state()
        if not state or state['desired_ref']!=source_ref:return None
        signature=hashlib.sha256(canonical_json([source_ref,values])).hexdigest()
        if self.seen.get(consumer)==signature:return None
        config=self.service._config(source_ref);expected=leaf_values(config)
        if any(path not in expected or expected[path]!=value for path,value in values.items()):raise ValueError('Consumer values differ from accepted configuration')
        key='config-consumer-use:'+source_ref['sha256']
        async with self.service.database.connection() as c:
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
            row=await (await c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s',(key,))).fetchone()
            merged=(row['value'].get('values') or {}) if row else {};merged={**merged,**values}
            if row and merged==row['value']['values']:ref=row['value']['receipt_ref']
            else:
                payload={'schema_version':'config-consumer-receipt.v1','consumer':consumer,'source_ref':source_ref,'values':merged,
                    'observed_at':datetime.now(timezone.utc).isoformat()}
                raw=canonical_json(payload);sha=hashlib.sha256(raw).hexdigest();path=self.settings.data_root/'runtime/config-consumers/observed'/(sha+'.json')
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
                ref=resource_ref(kind='receipt',identity='config-use:'+sha,uri=str(path),sha256=sha,purpose='Actual consumed fields, not inferred whole-configuration application')
                await c.execute('INSERT INTO assistant.kernel_meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()',
                    (key,Jsonb({'values':merged,'receipt_ref':ref})))
            await c.commit()
        for group in state['groups']:
            paths=[p for p in expected if p not in {'/$schema','/schema_version'} and self.service._group(p)==group['group']]
            if paths and all(p in merged and merged[p]==expected[p] for p in paths) and (group['status']!='applied' or group['effective_ref']!=source_ref):
                await self.service.settle_group(expected_revision=state['revision'],group=group['group'],evidence_ref=ref,applied=True,
                    reason='All fields in this group have actual matching consumer/readback evidence')
        self.seen[consumer]=signature
        return ref

    async def reconcile(self):
        state=await self.service.state()
        if not state:return
        root=self.settings.data_root/'runtime/config-consumers/startup'
        for path in root.glob('*/receipt.json') if root.exists() else []:
            receipt=json.loads(path.read_bytes())
            from assistant_kernel.worker.process_control import process_start_ticks
            if process_start_ticks(receipt['pid'])!=receipt.get('process_start_ticks') or receipt['source_ref']!=state['desired_ref']:continue
            await self.record(receipt['consumer'],receipt['source_ref'],receipt['used_values'])

    async def apply_dynamic(self,settings,component):
        state=await self.service.state()
        if not state:return settings
        if not self.cached or self.cached[0]!=state['desired_ref']:self.cached=(state['desired_ref'],self.service._config(state['desired_ref']))
        config=self.cached[1];kernel=config['kernel']
        settings.worker_poll_seconds=kernel['queue']['poll_seconds'];settings.worker_lease_seconds=kernel['queue']['lease_seconds']
        settings.health_heartbeat_seconds=kernel['health']['heartbeat_seconds'];settings.health_stale_after_seconds=kernel['health']['stale_after_seconds']
        logging.getLogger().setLevel(config['logging']['level'])
        values={**leaf_values(kernel['queue'],'/kernel/queue'),**leaf_values(kernel['health'],'/kernel/health'),'/logging/level':config['logging']['level']}
        await self.record(component,state['desired_ref'],values);return settings

    async def application_plan(self):
        state=await self.service.state()
        if not state:return {'groups':[],'configured':False}
        return {'configured':True,'desired_ref':state['desired_ref'],'groups':[{'group':g['group'],'status':g['status'],
            'apply_mode':g['apply_mode'],'effective_ref':g['effective_ref'],'required_action':
            'No action; actual values verified' if g['status']=='applied' else
            'Use the approved deployment command to restart only the affected consumers' if g['apply_mode']=='pending_restart' else
            'Render and apply the approved deployment after backup; do not recreate automatically' if g['apply_mode']=='pending_recreate' else
            'Explicit consistent data/path migration required; never connect an empty replacement store' if g['apply_mode']=='requires_migration' else
            'Await this consumer legal boundary and actual use/readback receipt'} for g in state['groups']]}
