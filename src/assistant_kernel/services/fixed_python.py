"""Pinned Python inputs and code. Authorization stays with the caller's control gate."""
from __future__ import annotations
import ast,hashlib,json,sys
from pathlib import Path,PurePosixPath
from copy import deepcopy

from jsonschema import Draft202012Validator,FormatChecker,SchemaError,ValidationError
from assistant_kernel.services.contracts import canonical_json,contracts,strict_json
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager


def data_schema(value,schema):
    """Use the declared JSON Schema without network resolution or legacy depth limits."""
    def local(item):
        if isinstance(item,dict):
            if '$ref' in item and not str(item['$ref']).startswith('#'):raise ValueError('Execution data Schema has an external reference')
            for child in item.values():local(child)
        elif isinstance(item,list):
            for child in item:local(child)
    local(schema)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema,format_checker=FormatChecker()).validate(value)
    except (SchemaError,ValidationError) as exc:
        raise ValueError('Declared execution data Schema failed at /'+'/'.join(map(str,exc.absolute_path))) from exc


def safe_relative(value):
    path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or str(path)=='.':raise ValueError('Bundle/output path must stay below its declared root')
    return path


class FixedPython:
    def __init__(self,settings):
        self.settings=settings;self.runtime=ProjectRuntimeManager(settings.data_root)
        self.documents=TaskDocumentService(settings);self.scopes=WorkspaceScopeService(settings)

    def source_path(self,project,ref,roots):
        contracts().validate('PinnedRef',ref);uri=ref['uri']
        if uri.startswith('vault://'+project+'/'):
            path=self.runtime.resolve_vault_file(project,uri.removeprefix('vault://'+project+'/'))
        elif uri.startswith('workspace://'):
            _,path,_=self.scopes.resolve_uri(roots,uri)
        else:raise PermissionError('Code/input source is outside the registered Project scope')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=ref['sha256']:
            raise ValueError('Pinned execution source bytes changed: '+uri)
        return path

    def read_manifest(self,project,ref):
        document=self.documents.read(project,ref)
        service=document['schema_version']=='python-service-manifest.v1'
        if document['schema_version'] not in {'python-script-manifest.v1','python-service-manifest.v1'}:raise PermissionError('Expected a fixed Python manifest')
        manifest=deepcopy(document['payload']);contracts().validate('V12_ServiceManifest' if service else 'V12_PythonManifest',manifest)
        if not service and manifest['ownership']['project_id']!=project:raise PermissionError('Manifest belongs to another Project')
        if service:
            manifest['resolved_config_ref']=manifest['resources']['resolved_config_ref']
        for ref in (manifest['resolved_config_ref'],manifest['resources']['resolved_config_ref']):
            snapshot=self.documents.read(project,ref)
            if snapshot['schema_version']!='resolved-system-config.v1' or snapshot['payload']['status']!='resolved':
                raise ValueError('Python configuration is not an actual resolved snapshot')
        return manifest

    def resource_keys(self,project,manifest,run_id='00000000-0000-0000-0000-000000000000'):
        """Lock the actual paths, so two aliases of one shared file conflict."""
        roots=self.scopes.resolve(project,run_id,'build')
        keys=[]
        for uri in manifest['io']['write_targets']:
            if uri.startswith('attempt://'):
                safe_relative(uri.removeprefix('attempt://'))
                continue  # Every Run/attempt has its own output directory.
            if uri.startswith('vault://'+project+'/'):
                path=self.runtime.resolve_vault_file(project,uri.removeprefix('vault://'+project+'/'))
            elif uri.startswith('workspace://'):
                _,path,_=self.scopes.resolve_uri(roots,uri,require_write=True)
            else:raise PermissionError('Write target is outside the Project file agreement')
            keys.append('path:'+path.resolve().as_posix())
        return sorted(set(keys))

    def prepare(self,*,run,authorization_verified:bool,attempt_root=None):
        if authorization_verified is not True:raise PermissionError('Execution needs its actual business authorization gate')
        project=run['project_id'];request=run['request_json'];manifest=self.read_manifest(project,request['manifest_ref'])
        inputs=request['input']
        if 'service_id' not in manifest:data_schema(inputs,manifest['io']['input_schema'])
        if len(canonical_json(inputs))>manifest['resources'].get('max_input_bytes',1048576):raise ValueError('Fixed Python input exceeds its declared limit')
        roots=self.scopes.resolve(project,str(run['run_id']),'build')
        self.resource_keys(project,manifest,str(run['run_id']))
        for ref in manifest['io']['read_refs']:
            self.source_path(project,ref,roots)
        code=manifest['code_bundle'];index=strict_json(self.source_path(project,code['bundle_ref'],roots).read_bytes())
        closure=strict_json(self.source_path(project,code['closure_manifest_ref'],roots).read_bytes())
        if index.get('schema_version')!='python-code-bundle.v1' or closure.get('schema_version')!='python-import-closure.v1':
            raise ValueError('Code bundle needs its fixed file index and import closure')
        pinned=[code['entrypoint_ref'],*code['module_refs']];files=index['files']
        if not files or len({x['relative_path'] for x in files})!=len(files):raise ValueError('Bundle file paths are duplicated or empty')
        if {x['ref']['uri'] for x in files}!={x['uri'] for x in pinned}:raise ValueError('Bundle index differs from the manifest entrypoint/modules')
        paths={x['ref']['uri']:safe_relative(x['relative_path']) for x in files}
        local_modules={str(p).replace('/','.').removesuffix('.py') for p in paths.values()}
        local_modules.update(x.removesuffix('.__init__') for x in list(local_modules))
        if not set(closure['stdlib_modules'])<=set(sys.stdlib_module_names):raise ValueError('Closure claims a non-stdlib module as stdlib')
        if manifest['environment']['dependencies_kind']=='stdlib_only' and closure['dependency_modules']:
            raise ValueError('A stdlib-only environment cannot silently load third-party dependencies')
        allowed=set(closure['stdlib_modules'])|set(closure['dependency_modules'])|{x.split('.')[0] for x in local_modules}
        sources=[]
        for item in files:
            approved=next(x for x in pinned if x['uri']==item['ref']['uri'])
            if approved['sha256']!=item['ref']['sha256']:raise ValueError('Bundle file SHA differs from the manifest')
            source=self.source_path(project,approved,roots);raw=source.read_bytes()
            if source.suffix=='.py':
                tree=ast.parse(raw,filename=str(source))
                for node in ast.walk(tree):
                    modules=([x.name for x in node.names] if isinstance(node,ast.Import) else
                        [node.module] if isinstance(node,ast.ImportFrom) and node.level==0 and node.module else [])
                    if any(x.split('.')[0] not in allowed for x in modules):raise ValueError('A Python import is absent from the fixed closure')
            sources.append((paths[approved['uri']],raw))
        env_path=self.source_path(project,manifest['environment']['environment_ref'],roots)
        environment=strict_json(env_path.read_bytes())
        if environment.get('schema_version')!='python-environment-fingerprint.v1':raise ValueError('Expected the actual Python environment fingerprint')
        interpreter=Path(environment['interpreter_path'])
        interpreter_ref=manifest['environment']['interpreter_ref']
        if str(interpreter)!=interpreter_ref['uri'] or not interpreter.is_file() or hashlib.sha256(interpreter.read_bytes()).hexdigest()!=interpreter_ref['sha256']:
            raise ValueError('Fixed Python interpreter changed')
        dependencies=manifest['environment']
        for ref in (dependencies['dependency_lock_ref'],dependencies['dependency_manifest_ref']):
            if ref:self.source_path(project,ref,roots)
        if dependencies['dependencies_kind']=='declared_dependencies' and not (dependencies['dependency_lock_ref'] or dependencies['dependency_manifest_ref']):
            raise ValueError('Dependencies need their actual fixed lock/manifest')
        # Metadata fingerprints are read without executing sitecustomize or dependency code.
        import importlib.metadata
        expected_site=str(interpreter.parent.parent/'lib'/('python'+environment['python_minor'])/'site-packages')
        if environment['site_packages']!=[expected_site]:raise ValueError('Environment package path differs from its fixed interpreter prefix')
        actual={x.metadata['Name'].lower().replace('_','-'):x.version for x in importlib.metadata.distributions(path=environment['site_packages'])}
        if actual!=environment['distribution_versions']:raise ValueError('Fixed Python dependency environment changed')
        attempt=attempt_root or self.runtime.describe(project).vault_root/'runs'/str(run['run_id'])/'attempts'/str(run['attempt'])
        code_root=attempt/'code';code_root.mkdir(parents=True,exist_ok=False)
        for relative,raw in sources:
            target=code_root/str(relative);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        entrypoint=code_root/str(paths[code['entrypoint_ref']['uri']])
        for binding in manifest['io']['input_bindings']:
            source=self.source_path(project,binding['source_ref'],roots)
            destination=attempt/'inputs'/str(safe_relative(binding['destination_relative_path']))
            destination.parent.mkdir(parents=True,exist_ok=True)
            if binding['method']=='copy':destination.write_bytes(source.read_bytes())
            else:destination.symlink_to(source)
        cwd_uri=manifest['io']['cwd_uri']
        if cwd_uri=='attempt://code':cwd=code_root
        elif cwd_uri=='attempt://output':cwd=attempt
        else:_,cwd,_=self.scopes.resolve_uri(roots,cwd_uri,require_write=False)
        if not cwd.is_dir():raise ValueError('Fixed Python working directory is absent')
        output_uri=manifest['io']['output_root_uri']
        if output_uri=='attempt://output':business_output=attempt
        else:
            _,output_base,_=self.scopes.resolve_uri(roots,output_uri,require_write=True)
            business_output=output_base/'runs'/str(run['run_id'])/'attempts'/str(run['attempt'])
            business_output.mkdir(parents=True,exist_ok=False)
        return {'manifest':manifest,'source':entrypoint,'interpreter':interpreter,'cwd':cwd,
            'args':manifest['fixed_args'],'input':inputs,'resources':manifest['resources'],'attempt_root':attempt,
            'business_output_root':business_output,
            'materialized_files':{uri:code_root/str(relative) for uri,relative in paths.items()},
            'code_bundle_ref':code['bundle_ref'],'environment_ref':dependencies['environment_ref']}

    def check_outputs(self,prepared,result,output_root):
        manifest=prepared['manifest'];io=manifest['io'];limits=manifest['resources']
        data_schema(result,io['output_schema'])
        if limits.get('max_output_bytes') is not None and len(canonical_json(result))>limits['max_output_bytes']:raise ValueError('Python output exceeds its declared limit')
        for relative in io['required_outputs']:
            path=output_root/str(safe_relative(relative))
            if output_root.resolve() not in path.resolve().parents or not path.is_file():raise ValueError('Python required output is missing: '+relative)
        checks=[];verified_effects=set();observed_files=[]
        for ref in io['business_check_refs']:
            rule=strict_json(self.source_path(manifest['ownership']['project_id'],ref,
                self.scopes.resolve(manifest['ownership']['project_id'],'00000000-0000-0000-0000-000000000000','build')).read_bytes())
            if rule.get('schema_version')!='python-business-check.v1':raise ValueError('Expected the fixed business assertion')
            value=result
            if rule.get('source_kind')=='file_text':
                uri=rule['target_uri']
                if uri not in io['write_targets'] or rule['json_pointer']:raise PermissionError('File assertion is outside the fixed declared write target')
                project=manifest['ownership']['project_id'];roots=self.scopes.resolve(project,'00000000-0000-0000-0000-000000000000','build')
                if uri.startswith('vault://'+project+'/'):path=self.runtime.resolve_vault_file(project,uri.removeprefix('vault://'+project+'/'))
                else:_,path,_=self.scopes.resolve_uri(roots,uri,require_write=False)
                value=path.read_text(encoding='utf-8') if path.is_file() else None
                if path.is_file():
                    raw=path.read_bytes();target=output_root/'business-evidence'/hashlib.sha256(uri.encode()).hexdigest()
                    target.parent.mkdir(exist_ok=True);target.write_bytes(raw);observed_files.append(target)
            for part in rule['json_pointer'].strip('/').split('/') if rule['json_pointer'] else []:
                part=part.replace('~1','/').replace('~0','~');value=value[int(part)] if isinstance(value,list) else value[part]
            if rule['operator']!='equals':raise ValueError('Business check operator is not implemented')
            checks.append(value==rule['expected'])
            if checks[-1] and rule.get('source_kind')=='file_text':verified_effects.update(rule.get('effect_keys',[]))
        prepared['business_check_evidence']=observed_files
        return {'contract_status':'passed','business_status':'passed' if checks and all(checks) else 'failed' if checks else 'unverified',
            'effect_status':'none' if not manifest['expected_effects'] else 'verified' if set(manifest['expected_effects'])<=verified_effects else 'unknown'}
