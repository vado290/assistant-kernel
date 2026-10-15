"""Adopt explicit MCP phase selections for new work; preserve existing Task refs."""
import hashlib,json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.published_agents import PublishedAgentService
from assistant_kernel.services.template_bundle import TemplateBundle
from assistant_kernel.services.kernel_management import retired_identity
from scripts.adopt_c26_templates import refresh_config
from assistant_kernel import __version__


def select_existing_tools(config,policy,actor):
    selected=policy['phases'][config.phase_mode] if actor=='pm' else policy['actors'][actor]
    return tuple(name for name in selected if name in config.allowed_tools)


def adopt(settings,decision,*,apply=False):
    service=PublishedAgentService(settings);new=TemplateBundle();results=[]
    with service.connection() as c:
        rows=c.execute("SELECT key,value FROM assistant.kernel_meta WHERE key LIKE 'project-runtime-package:%%' ORDER BY key").fetchall()
    for row in rows:
        project=row['key'].split(':',1)[1];old_ref=row['value']['package_ref'];package=service.read_package(project,old_ref)
        if package['template_ref']==new.ref('profiles.json'):
            results.append({'project_id':project,'already_current':True});continue
        loaded={k:service.loader.load_snapshot(project,r['uri'],r['sha256']) for k,r in package['configurations'].items()}
        roots=[]
        for path in (settings.data_root/'runtime/template-bundles').glob('*/manifest.json'):
            manifest=json.loads(path.read_text())
            if manifest['files'].get('profiles.json')==package['template_ref']['sha256'] and any(
                Path(name).is_relative_to(path.parent) for config in loaded.values() for name in config.source_files):roots.append(path.parent)
        if len(roots)!=1:raise ValueError('Published template provenance is ambiguous')
        old=TemplateBundle(roots[0]);target=new.preserve(settings.data_root) if apply else new;configs={};retired=[]
        for selector,config in loaded.items():
            if retired_identity(project,config.agent_key):
                retired.append(selector)
                continue
            updated=refresh_config(config,old,target)
            actor='pm' if config.agent_key.endswith(':manager') else config.agent_key.rsplit(':',1)[-1]
            policy=target.profiles['mcp_tool_policy']
            tools=select_existing_tools(config,policy,actor)
            updated=replace(updated,allowed_tools=tuple(tools))
            configs[selector]=replace(updated,config_sha256=hashlib.sha256(canonical_json({**updated.__dict__,'config_sha256':None})).hexdigest())
        result={'project_id':project,'previous_ref':old_ref,'old_tasks_rebound':False,
            'tool_counts':{k:len(v.allowed_tools) for k,v in configs.items()},
            'retired_selectors_omitted':retired,'applied':False}
        if apply:
            raw=Path(decision).read_bytes();root=service.loader.runtime.describe(project).vault_root
            relative='operations/upgrade-decisions/mcp-'+__version__+'.json';path=root/relative;path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and path.read_bytes()!=raw:raise ValueError('MCP approval provenance changed')
            path.write_bytes(raw)
            source=resource_ref(kind='source',identity='mcp-unification-'+__version__,uri='vault://'+project+'/'+relative,
                sha256=hashlib.sha256(raw).hexdigest(),purpose='Actual user approved MCP configuration/discovery/call unification')
            ref=service.prepare(project,configurations=configs,template_ref=target.ref('profiles.json'),
                adoption_scope={'project_ids':[project],'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True},
                source_refs=[source],supervisor_binding_specs=deepcopy(package.get('supervisor_config_bindings')),
                project_policy=deepcopy(package['project_policy']))
            service.adopt(project,package_ref=ref,expected_previous_ref=old_ref,approval_refs=[source]);result.update(applied=True,package_ref=ref)
        results.append(result)
    return results
