"""Update only published public template text, preserving Project additions and old Tasks."""
import argparse,hashlib,json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from assistant_kernel.config import KernelSettings
from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.published_agents import PublishedAgentService
from assistant_kernel.services.template_bundle import TemplateBundle
from assistant_kernel.services.document_runtime import resource_ref


def refresh_config(config,old,new):
    role=config.role_text;common=config.common_role_text;matched=0
    for relative in old.manifest['files']:
        if not relative.startswith('roles/') or relative not in new.manifest['files']:continue
        before=old.text(relative).strip();after=new.text(relative).strip()
        if before in role:role=role.replace(before,after);matched+=1
        if common and before in common:common=common.replace(before,after)
    if not matched:raise ValueError('Published Role has no verified public template source; preserve it for explicit review')
    if len(config.skill_texts)!=len(config.skill_bindings):raise ValueError('Skill load record is incomplete')
    texts=[];bindings=[]
    for text,binding in zip(config.skill_texts,config.skill_bindings):
        name=binding['name']
        if name=='assistant-schedule-design':continue
        relative='skills/'+name+'/SKILL.md'
        public=binding.get('source')=='kernel' and (binding.get('published_ref') or {}).get('id','').startswith('v12-template/')
        if public and relative in new.manifest['files']:
            text=new.text(relative);path=new.root/relative
            files=[{'path':'SKILL.md','sha256':new.manifest['files'][relative],'size':path.stat().st_size}]
            binding={**binding,'files':files,'bundle_hash':hashlib.sha256(canonical_json(files)).hexdigest(),
                'source_root':str(path.parent),'published_ref':new.ref(relative)}
        texts.append(text);bindings.append(binding)
    sources=[]
    for name in config.source_files:
        if 'assistant-schedule-design/' in name:continue
        path=Path(name)
        if path.is_relative_to(old.root):
            relative=path.relative_to(old.root)
            if (new.root/relative).is_file():name=str(new.root/relative)
        sources.append(name)
    updated=replace(config,role_text=role,common_role_text=common,skill_texts=tuple(texts),skill_bindings=tuple(bindings),
        available_skills=tuple(x for x in config.available_skills if x['name']!='assistant-schedule-design'),source_files=tuple(sources))
    return replace(updated,config_sha256=hashlib.sha256(canonical_json({**updated.__dict__,'config_sha256':None,'decision':'C26'})).hexdigest())


def adopt(settings,*,apply=False):
    service=PublishedAgentService(settings);new=TemplateBundle();results=[]
    with service.connection() as c:
        rows=c.execute("SELECT key,value FROM assistant.kernel_meta WHERE key LIKE 'project-runtime-package:%%' ORDER BY key").fetchall()
    for row in rows:
        project=row['key'].split(':',1)[1];old_ref=row['value']['package_ref'];package=service.read_package(project,old_ref)
        if package['template_ref']==new.ref('profiles.json'):
            results.append({'project_id':project,'already_current':True});continue
        matches=[]
        for path in (settings.data_root/'runtime/template-bundles').glob('*/manifest.json'):
            manifest=json.loads(path.read_text())
            if manifest['files'].get('profiles.json')==package['template_ref']['sha256']:matches.append(path.parent)
        loaded={selector:service.loader.load_snapshot(project,ref['uri'],ref['sha256']) for selector,ref in package['configurations'].items()}
        # Several historical bundles can share profiles.json while other assets
        # differ. Resolve the actual archived bundle from the loaded source paths.
        matches=[path for path in matches if any(Path(name).is_relative_to(path)
            for config in loaded.values() for name in config.source_files)]
        if len(matches)!=1:raise ValueError('Original public template source identity is ambiguous or absent')
        old=TemplateBundle(matches[0]);target=new.preserve(settings.data_root) if apply else new
        configs={selector:refresh_config(config,old,target) for selector,config in loaded.items()}
        result={'project_id':project,'previous_ref':old_ref,'selectors':sorted(configs),'old_tasks_rebound':False,
            'policy_preserved':True,'model_tools_budgets_preserved':True,'applied':False}
        if apply:
            decision=service.loader.runtime.describe('kernel').vault_root/'operations/upgrade-decisions/C26.json'
            if not decision.is_file():raise ValueError('Apply the C26 accepted configuration/decision before package adoption')
            # Same actual human instruction, copied as provenance into each Project.
            path=service.loader.runtime.ensure(project).vault_root/'operations/upgrade-decisions/C26.json';raw=decision.read_bytes()
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists() and path.read_bytes()!=raw:raise ValueError('Immutable C26 provenance differs')
            if not path.exists():path.write_bytes(raw)
            source=resource_ref(kind='source',identity='C26',uri='vault://'+project+'/operations/upgrade-decisions/C26.json',
                sha256=hashlib.sha256(raw).hexdigest(),purpose='Actual user approved global scheduling removal')
            ref=service.prepare(project,configurations=configs,template_ref=target.ref('profiles.json'),
                adoption_scope={'project_ids':[project],'existing_task_ids':[],'new_tasks':True,'next_turn_binding_confirmation':True},
                source_refs=[source],supervisor_binding_specs=deepcopy(package.get('supervisor_config_bindings')),
                project_policy=deepcopy(package['project_policy']))
            service.adopt(project,package_ref=ref,expected_previous_ref=old_ref,approval_refs=[source])
            result.update(applied=True,package_ref=ref)
        results.append(result)
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args();result=adopt(KernelSettings.from_environment(),apply=args.apply)
    args.receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'packages':len(result),'applied':args.apply}))
