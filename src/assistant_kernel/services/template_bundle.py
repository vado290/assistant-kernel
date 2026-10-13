"""Fixed public Role/Skill/profile assets; physical edits never imply adoption."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from assistant_kernel.services.agent_files import FileAgentConfig
from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import resource_ref


class TemplateBundle:
    def __init__(self, root: Path | None = None):
        self.root=root or Path(__file__).parents[1]/'prompts/v12'
        self.manifest=json.loads((self.root/'manifest.json').read_text('utf-8'))
        if self.manifest['schema_version']!='kernel-template-bundle.v1':raise ValueError('Unknown template manifest')
        self.sha256=self.manifest['bundle_sha256']
        if hashlib.sha256(canonical_json(self.manifest['files'])).hexdigest()!=self.sha256:
            raise ValueError('Template manifest digest differs')
        self.verify()
        self.profiles=json.loads((self.root/'profiles.json').read_text('utf-8'))

    def verify(self):
        for relative,digest in self.manifest['files'].items():
            path=(self.root/relative).resolve()
            if self.root.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('Fixed template asset is missing or changed: '+relative)

    def preserve(self,data_root: Path):
        import fcntl
        parent=data_root/'runtime/template-bundles';parent.mkdir(parents=True,exist_ok=True)
        target=parent/self.sha256
        with (parent/'writer.lock').open('a+b') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            if not target.exists():
                staging=parent/('.prepare-'+uuid.uuid4().hex)
                shutil.copytree(self.root,staging)
                TemplateBundle(staging)
                staging.rename(target)
        saved=TemplateBundle(target)
        if saved.sha256!=self.sha256:raise ValueError('Archived template identity differs')
        return saved

    def ref(self,relative):
        digest=self.manifest['files'][relative]
        return resource_ref(kind='source',identity='v12-template/'+relative,
            uri='kernel://template-assets/'+digest+'/'+relative,sha256=digest,
            purpose='fixed published template asset; not business approval')

    def text(self,relative):
        encoded=(self.root/relative).read_bytes()
        if hashlib.sha256(encoded).hexdigest()!=self.manifest['files'][relative]:
            raise ValueError('Template asset changed before loading')
        return encoded.decode('utf-8')

    def assemble(self, base: FileAgentConfig, *, actor: str, phase: str | None,
                 conditions: set[str], condition_source_refs: list[dict],
                 local_role_append: str, local_role_source_refs: list[dict],binding_specs=None):
        """Build a candidate configuration and truthful load record, without adoption.

        Callers explicitly reconcile old local differences. Silently appending the
        entire legacy public Role would reinstate the rules being retired.
        """
        if actor not in {'pm','supervisor','native_subagent'}:raise ValueError('Unknown actor')
        if conditions and not condition_source_refs:raise ValueError('Conditional loading needs fixed source evidence')
        if local_role_append and not local_role_source_refs:raise ValueError('Local Role additions need provenance')
        phase='build' if phase=='repair' else phase
        roles=['roles/common-control.md']
        selected=[];profile=None
        if actor=='pm':
            if phase not in self.profiles['profiles']:raise ValueError('PM phase is not published')
            profile=self.profiles['profiles'][phase]
            roles+=['roles/pm-common.md','roles/pm-'+phase+'.md']
            selected=[x['name'] for x in profile['required_skills']]
            known={x['condition'] for x in profile['conditional_skill_rules']}
            if conditions-known:raise ValueError('Unknown conditional Skill selector')
            selected.extend(x['skill_name'] for x in profile['conditional_skill_rules'] if x['condition'] in conditions)
        elif actor=='supervisor':
            roles+=['roles/supervisor.md']
            selected=['assistant-source-scope','assistant-goal-supervisor','assistant-prompt-assembly','assistant-interaction-followup','assistant-session-boundary']
        else:
            roles+=['roles/native-subagent.md'];selected=['assistant-source-scope','assistant-subagent-orchestration']
        selected=list(dict.fromkeys(selected))
        available=list(base.available_skills)
        if actor in {'pm','supervisor'} and 'skills/assistant-schedule-management/SKILL.md' in self.manifest['files']:
            available=[x for x in available if x.get('name')!='assistant-schedule-management']+[{'name':'assistant-schedule-management','source':'kernel'}]
        base=replace(base,available_skills=tuple(available))
        texts=[];bindings=[];files=[]
        for name in selected:
            relative='skills/'+name+'/SKILL.md';body=self.text(relative);path=self.root/relative
            entries=[{'path':'SKILL.md','sha256':self.manifest['files'][relative],'size':path.stat().st_size}]
            texts.append(body);files.append(str(path))
            bindings.append({'name':name,'source':'kernel','bundle_hash':hashlib.sha256(canonical_json(entries)).hexdigest(),
                'files':entries,'source_root':str(path.parent),'published_ref':self.ref(relative)})
        role='\n\n'.join(self.text(relative).strip() for relative in roles)
        if local_role_append:role+='\n\n# Accepted Project-specific additions\n'+local_role_append
        files.extend(str(self.root/relative) for relative in roles)
        if binding_specs is None:
            # Existing explicit/unknown-origin values remain overrides. Only
            # absent provider/model and newly introduced defaults inherit.
            mapping={'provider':base.provider,'model':base.model,'reasoning_effort':base.reasoning_effort,
                'timeout_seconds':base.timeout_seconds,'context_policy':base.context_policy,
                'custom_context_window_tokens':base.model_context_window_tokens,
                'native_auto_compact_token_limit':base.model_auto_compact_token_limit}
            binding_specs=[]
            for name in contracts().library['$defs']['SystemConfig']['properties']['agent_defaults']['properties']:
                inherit=name not in mapping or name in {'provider','model'} and mapping[name] is None
                binding_specs.append({'path':'/agent_defaults/'+name,'mode':'inherit' if inherit else 'override',
                    'override_value':None if inherit else mapping[name]})
        actual=replace(base,role_text=role,common_role_text=role if actor!='pm' else self.text('roles/common-control.md')+'\n\n'+self.text('roles/pm-common.md'),
            phase_mode=phase,skill_texts=tuple(texts),skill_bindings=tuple(bindings),source_files=tuple(files),
            sandbox='danger-full-access',foundation_revision='1.2',config_bindings=tuple(binding_specs),
            output_schema=self.ref('schemas/agent-result.schema.json')['uri'] if actor in {'pm','supervisor'} else base.output_schema)
        policy=self.profiles.get('mcp_tool_policy')
        if policy is None:raise ValueError('Published template must declare its MCP tool policy')
        selected_tools=policy['phases'][phase] if actor=='pm' else policy['actors'][actor]
        actual=replace(actual,allowed_tools=tuple(dict.fromkeys(selected_tools)))
        material={**actual.__dict__,'config_sha256':None,'template_bundle_sha256':self.sha256,
                  'conditions':sorted(conditions),'condition_source_refs':condition_source_refs,
                  'local_role_source_refs':local_role_source_refs}
        actual=replace(actual,config_sha256=hashlib.sha256(canonical_json(material)).hexdigest())
        receipt={'template_bundle_sha256':self.sha256,'actor':actor,'phase':phase,
            'role_refs':[self.ref(relative) for relative in roles],
            'requested_skills':selected,'loaded_skills':selected,'missing_skills':[],
            'condition_source_refs':condition_source_refs,'local_role_source_refs':local_role_source_refs,
            'tools_requested':list(actual.allowed_tools),'tools_discovered':None,
            'tool_discovery_status':'unverified','file_package':'directory-full.v1',
            'native_isolation':False,'adopted':False}
        return actual,receipt
