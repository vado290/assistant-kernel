"""Build a reviewable import candidate; never write the effective input or DB.

Every leaf records whether it is an observed legacy value or a proposed design
default. Unknown legacy fields block import rather than silently disappearing.
Secret payloads are never copied to the candidate or its provenance ledger.
"""
from __future__ import annotations
import argparse
import asyncio
import configparser
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from assistant_kernel.config import KernelSettings
from assistant_kernel.config.system_config import leaf_values, pointer_parts, validate_system_config
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.services.contracts import contracts
from assistant_kernel.services.discord import load_discord_config
from assistant_kernel.services.feishu import load_feishu_config


async def build(platform, supervisor_path, settings=None):
    settings=settings or KernelSettings.from_environment()
    registry=contracts()
    candidate=json.loads((registry.root/'kernel-system.example.json').read_text())
    provenance={p:{'source':'approved v1.2 design default','observed':False,
                   'status':'proposed_default_pending_consumer'} for p in leaf_values(candidate)}
    blockers=[];conversions=[]
    def unconfigured(value,path):
        if isinstance(value,str) and not value.strip():
            conversions.append({'path':path,'old_value':value,'new_value':None,
                                'reason':'D09 approved explicit unconfigured value; no default substitution'})
            return None
        return value
    def put(path,value,source,observed=True):
        parts=pointer_parts(path);node=candidate
        for part in parts[:-1]:node=node.setdefault(part,{})
        node[parts[-1]]=deepcopy(value)
        for leaf in list(provenance):
            if leaf==path or leaf.startswith(path+'/'):del provenance[leaf]
        for leaf in leaf_values(value,path):
            provenance[leaf]={'source':source,'observed':observed,
                              'status':'import_legacy_value' if observed else 'proposed_default_pending_consumer'}
    mapping={
        '/paths/workspace_root':'workspace_root','/paths/python_executable':'python_executable',
        '/kernel/api/listen_host':'api_host','/kernel/api/port':'api_port',
        '/kernel/mcp/listen_host':'mcp_host','/kernel/mcp/port':'mcp_port',
        '/kernel/queue/poll_seconds':'worker_poll_seconds','/kernel/queue/lease_seconds':'worker_lease_seconds',
        '/python_defaults/timeout_seconds':'python_timeout_seconds','/agent_defaults/timeout_seconds':'agent_timeout_seconds',
        '/agent_defaults/provider':'default_codex_provider','/agent_defaults/model':'default_codex_model',
        '/agent_defaults/reasoning_effort':'default_codex_reasoning_effort',
        '/agent_defaults/context_policy':'default_codex_context_policy',
        '/agent_defaults/role_soft_limit_tokens':'role_soft_limit_tokens',
        '/agent_defaults/role_hard_limit_tokens':'role_hard_limit_tokens',
        '/postgres/host':'postgres_host','/postgres/port':'postgres_port',
        '/postgres/database':'postgres_database','/postgres/user':'postgres_user'}
    for path,attribute in mapping.items():
        value=getattr(settings,attribute)
        put(path,str(value) if isinstance(value,Path) else value,'KernelSettings.'+attribute)
    put('/paths/data_directory',settings.data_root.relative_to(settings.workspace_root).as_posix(),'KernelSettings.data_root relative to workspace_root')
    cfg=configparser.ConfigParser(interpolation=None);cfg.read(supervisor_path)
    for queue,section in [('interactive','program:kernel-run-worker'),('runtime','program:kernel-runtime-worker')]:
        put('/kernel/workers/'+queue,cfg.getint(section,'numprocs',fallback=1),'live supervisord '+section)
    properties=registry.library['$defs']['SystemConfig']['properties']
    for channel,actual in [('discord',load_discord_config(settings)),('feishu',load_feishu_config(settings))]:
        published=properties['channels']['properties'][channel]['properties']
        for key in published:
            if key=='account_ref' or key not in actual:continue
            value=actual[key]
            if isinstance(value,set):value=sorted(value)
            put('/channels/'+channel+'/'+key,value,'actual '+channel+' loader; credential fields excluded')
    # Never dump ProviderCredential.model_dump(): extras may contain credentials.
    public_provider=properties['providers']['additionalProperties']['properties']
    source=settings.load_provider_secrets()
    for name,provider in source.providers.items():
        if '/' in name or '~' in name:raise ValueError('provider ID is not supported by the import contract')
        for key in public_provider:
            if key=='api_key_ref':
                value='secret://providers/'+name+'/api-key'
            elif key=='model_names':value={k:unconfigured(v,'/providers/'+name+'/model_names/'+k) for k,v in provider.models.model_dump().items()}
            elif key=='chat_models':
                # Only fields explicitly published by the approved contract can
                # cross this boundary. A future Schema addition is not a secret
                # migration and must remain an explicit contract decision.
                spec=public_provider[key]['additionalProperties']['properties']
                value={}
                for model,record in provider.chat_models.items():
                    extra=set(record)-set(spec)
                    if extra:blockers.append({'kind':'unmapped_model_fields','provider':name,'model':model,'fields':sorted(extra)})
                    value[model]={k:(unconfigured(v,'/providers/'+name+'/chat_models/'+model+'/'+k) if k in {'model_id','endpoint_id','base_url'} else deepcopy(v)) for k,v in record.items() if k in spec}
                    value[model]['pricing_verification']={'status':'unverified','source_refs':[],'verified_at':None}
            elif hasattr(provider,key):value=getattr(provider,key)
            else:continue
            if key in {'native_base_url','openai_compatible_base_url','rerank_base_url'}:
                value=unconfigured(value,'/providers/'+name+'/'+key)
            put('/providers/'+name+'/'+key,value,'provider-secrets public field; secret remains in existing Store')
        if provider.chat_models and 'chat_models' not in public_provider:
            blockers.append({'kind':'unmapped_chat_models','provider':name,'model_count':len(provider.chat_models),
                             'decision':'D08 pending; do not discard existing model configuration'})
    async with AssistantDatabase(settings).connection() as connection:
        await connection.execute('SET TRANSACTION READ ONLY')
        for field in candidate['postgres']['settings']:
            name=field.removesuffix('_mb').removesuffix('_ms')
            row=await (await connection.execute('SELECT setting,unit,source FROM pg_settings WHERE name=%s',(name,))).fetchone()
            if not row:raise ValueError('published PostgreSQL setting is unavailable: '+name)
            value=int(row['setting'])
            if field.endswith('_mb'):
                scale={'8kB':8/1024,'kB':1/1024,'MB':1}.get(row['unit'])
                if scale is None:raise ValueError('unknown PostgreSQL setting unit')
                value=value*scale
                if int(value)!=value:raise ValueError('PostgreSQL value cannot be represented in whole MB')
                value=int(value)
            put('/postgres/settings/'+field,value,'pg_settings '+name+'; source='+row['source'])
    containers={item['name']:item for item in platform}
    for name,item in containers.items():
        if name not in candidate['infrastructure']['services']:continue
        put('/infrastructure/services/'+name,{'image':item['image'],
            'cpu_limit':item['nano_cpus']/1_000_000_000 if item['nano_cpus'] else None,
            'memory_limit_mb':item['memory_bytes']//1048576 if item['memory_bytes'] else None},'docker inspect '+name)
    put('/infrastructure/compose_project',containers['AI']['compose_project'],'Docker Compose label AI')
    if containers['AI']['environment'].get('TZ'):
        put('/system/timezone',containers['AI']['environment']['TZ'],'AI container TZ')
    ports={'kernel_api':('AI','8001/tcp'),'kernel_mcp':('AI','8002/tcp'),
           'document_service':('AI','5001/tcp'),
           'postgres':('rag-postgres','5432/tcp'),'redis':('redis','6379/tcp'),
           'reranker':('rag-reranker','8080/tcp')}
    addresses=set()
    for key,(name,port) in ports.items():
        bound=containers[name]['ports'].get(port) or []
        if len(bound)!=1:raise ValueError('managed host port is ambiguous: '+key)
        put('/infrastructure/host_ports/'+key,int(bound[0]['HostPort']),'docker inspect '+name+' port '+port)
        addresses.add(bound[0]['HostIp'])
    if len(addresses)!=1:raise ValueError('one host_bind_address cannot represent the current managed ports')
    put('/infrastructure/host_bind_address',addresses.pop(),'Docker managed port bindings only; unrelated ports excluded')
    validation=validate_system_config(candidate)
    if not validation['valid']:blockers.append({'kind':'candidate_validation','issues':validation['issues']})
    ledger=[{'path':path,'candidate_value':value,**provenance[path]}
            for path,value in sorted(leaf_values(candidate).items())]
    return candidate,{'schema_version':'config-import-candidate.v1','contract_sha256':registry.sha256,
        'production_written':False,'runtime_effective':False,'import_ready':not blockers,
        'blockers':blockers,'fields':ledger,'explicit_empty_conversions':conversions,
        'limitations':['Proposed defaults do not assert legacy runtime values.',
            'External adapter and new consumers remain pending their verified phase gates.']}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--platform',type=Path,required=True)
    parser.add_argument('--supervisor',type=Path,default=Path('/etc/supervisor/conf.d/assistant.conf'))
    parser.add_argument('--output-directory',type=Path,required=True)
    args=parser.parse_args()
    # Caller selects a review directory; never accept a live input path here.
    live=KernelSettings.from_environment().data_root/'config'
    if args.output_directory.resolve()==live.resolve() or live.resolve() in args.output_directory.resolve().parents:
        raise ValueError('candidate output must be outside the live configuration directory')
    candidate,receipt=asyncio.run(build(json.loads(args.platform.read_text('utf-8-sig')),args.supervisor))
    args.output_directory.mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(candidate,ensure_ascii=False,sort_keys=True,indent=2).encode()
    receipt['candidate_sha256']=hashlib.sha256(encoded).hexdigest()
    (args.output_directory/'kernel-system.candidate.json').write_bytes(encoded)
    (args.output_directory/'import-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'fields':len(receipt['fields']),'import_ready':receipt['import_ready'],'blockers':receipt['blockers']}))
