"""Adopt approved scheduler templates/configuration; preserve every existing Task snapshot."""
import argparse,asyncio,hashlib,json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from assistant_kernel.config import KernelSettings
from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.published_agents import PublishedAgentService
from assistant_kernel.services.template_bundle import TemplateBundle
from assistant_kernel.services.system_config import SystemConfigService
from assistant_kernel.services.schedules import DEFAULTS
from scripts.adopt_c26_templates import refresh_config
from assistant_kernel import __version__

READ_TOOLS=('preview_schedule','list_schedules','schedule_status','list_schedule_ticks')
WRITE_TOOLS=('register_schedule','control_schedule')


def refresh(config,old,new):
    updated=refresh_config(config,old,new)
    tools=updated.allowed_tools
    if updated.kind in {'pm','supervisor'} or updated.agent_key.endswith((':manager',':supervisor')):
        tools=tuple(dict.fromkeys((*tools,*READ_TOOLS)))
        if updated.phase_mode=='operate':tools=tuple(dict.fromkeys((*tools,*WRITE_TOOLS)))
    available=list(updated.available_skills)
    if updated.agent_key.endswith((':manager',':supervisor')):
        available=[x for x in available if x.get('name')!='assistant-schedule-management']+[{'name':'assistant-schedule-management','source':'kernel'}]
    updated=replace(updated,allowed_tools=tools,available_skills=tuple(available))
    return replace(updated,config_sha256=hashlib.sha256(canonical_json({**updated.__dict__,'config_sha256':None,'decision':'scheduler-0.31'})).hexdigest())


def provenance(settings,project,decision):
    raw=decision.read_bytes();parsed=json.loads(raw)
    if parsed.get('authority')!='actual_user_request' or parsed.get('release')!=__version__:raise PermissionError('Actual release implementation instruction is required')
    root=PublishedAgentService(settings).loader.runtime.describe(project).vault_root
    name='scheduler-'+__version__+'.json'
    path=root/'operations/upgrade-decisions'/name;path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.read_bytes()!=raw:raise ValueError('Immutable upgrade authority differs')
    if not path.exists():path.write_bytes(raw)
    return resource_ref(kind='source',identity='scheduler-'+__version__,uri='vault://'+project+'/operations/upgrade-decisions/'+name,
        sha256=hashlib.sha256(raw).hexdigest(),purpose='Actual user instruction to implement, verify and deploy Kernel scheduling; not business task approval')


def adopt_packages(settings,decision,apply=False):
    service=PublishedAgentService(settings);new=TemplateBundle();results=[]
    with service.connection() as c:rows=c.execute("SELECT key,value FROM assistant.kernel_meta WHERE key LIKE 'project-runtime-package:%%' ORDER BY key").fetchall()
    for row in rows:
        project=row['key'].split(':',1)[1];old_ref=row['value']['package_ref'];package=service.read_package(project,old_ref)
        loaded={selector:service.loader.load_snapshot(project,ref['uri'],ref['sha256']) for selector,ref in package['configurations'].items()}
        candidates=[]
        for path in (settings.data_root/'runtime/template-bundles').glob('*/manifest.json'):
            manifest=json.loads(path.read_text())
            if manifest['files'].get('profiles.json')==package['template_ref']['sha256'] and any(Path(name).is_relative_to(path.parent) for config in loaded.values() for name in config.source_files):candidates.append(path.parent)
        if len(candidates)!=1:raise ValueError('Existing public template source is ambiguous')
        old=TemplateBundle(candidates[0])
        if old.sha256==new.sha256:
            results.append({'project_id':project,'already_current':True});continue
        target=new.preserve(settings.data_root) if apply else new
        configs={key:refresh(config,old,target) for key,config in loaded.items()}
        result={'project_id':project,'previous_ref':old_ref,'selectors':sorted(configs),'old_tasks_rebound':False,'applied':False}
        if apply:
            source=provenance(settings,project,decision)
            requested=json.loads(decision.read_text()).get('existing_task_ids',[])
            with service.connection() as c:
                included=[str(x['run_id']) for x in c.execute('SELECT run_id FROM assistant.runs WHERE project_id=%s AND run_id::text=ANY(%s)',(project,requested)).fetchall()]
            ref=service.prepare(project,configurations=configs,template_ref=target.ref('profiles.json'),
                adoption_scope={'project_ids':[project],'existing_task_ids':included,'new_tasks':True,'next_turn_binding_confirmation':True},
                source_refs=[source],supervisor_binding_specs=deepcopy(package.get('supervisor_config_bindings')),project_policy=deepcopy(package['project_policy']))
            service.adopt(project,package_ref=ref,expected_previous_ref=old_ref,approval_refs=[source]);result.update(applied=True,package_ref=ref)
        results.append(result)
    return results


async def adopt(settings,decision,*,apply=False,enable=False,config_only=False):
    service=SystemConfigService(settings);prior=await service.state()
    if not prior:raise PermissionError('Scheduler upgrade needs the existing accepted Kernel configuration')
    candidate=service._config(prior['desired_ref']);candidate['kernel']['scheduler']={**DEFAULTS,**candidate['kernel'].get('scheduler',{}),'enabled':enable}
    result={'previous_config_state':prior,'enabled':enable,'applied':apply}
    if apply:
        source=provenance(settings,'kernel',decision)
        result['config_receipt']=await service.accept(candidate=candidate,expected_revision=prior['revision'],
            reason='Approved 0.31 scheduler rollout; business schedules remain independently authorized',source_kind='external_file',source_refs=[source],write_input=True)
    if not config_only:result['packages']=adopt_packages(settings,decision,apply)
    return result


async def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--enable',action='store_true');p.add_argument('--config-only',action='store_true')
    p.add_argument('--decision',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True);a=p.parse_args()
    result=await adopt(KernelSettings.from_environment(),a.decision,apply=a.apply,enable=a.enable,config_only=a.config_only)
    a.receipt.parent.mkdir(parents=True,exist_ok=True);a.receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
    print(json.dumps({'applied':a.apply,'scheduler_enabled':a.enable,'packages':len(result.get('packages',[]))}))

if __name__=='__main__':asyncio.run(main())
