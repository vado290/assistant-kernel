"""Minimal published interfaces for existing runtime/operation contracts."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]/'src/assistant_kernel';contract=root/'contracts';templates=root/'prompts/v12'
def read(path):return json.loads(path.read_text('utf-8'))
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
library=read(contract/'schema-library-v12.json');registry=read(contract/'registry-v12.json');defs=library['$defs']
assert registry['implementation_revision'] in {7,8}
writer=defs['Args_write_runtime_document'];writer['properties']['document_kind']['enum']=list(dict.fromkeys([*writer['properties']['document_kind']['enum'],'python-service-manifest']))
service_case={'properties':{'document_kind':{'const':'python-service-manifest'},'payload':{'$ref':'#/$defs/V12_ServiceManifest'}}}
if service_case not in writer['oneOf']:writer['oneOf'].append(service_case)
defs['RuntimeConfigBindingInput']={'type':'object','additionalProperties':False,'properties':{'path':{'type':'string','pattern':'^/'},
    'mode':{'enum':['inherit','override']},'override_value':{}},'required':['path','mode','override_value']}
library['x-contract-policy']['active_dependencies']=sorted(set(library['x-contract-policy']['active_dependencies'])|{'RuntimeConfigBindingInput'})
defs['Args_resolve_runtime_config']={'type':'object','additionalProperties':False,'properties':{'bindings':{'type':'array','items':{'$ref':'#/$defs/RuntimeConfigBindingInput'}}},'required':['bindings']}
defs['Returns_resolve_runtime_config']={'type':'object','additionalProperties':False,'properties':{'snapshot_ref':{'$ref':'#/$defs/DocRef'},
    'bindings':{'type':'array','items':{'$ref':'#/$defs/ConfigBinding'}},'selected_values':{'type':'object'}},'required':['snapshot_ref','bindings','selected_values']}
pairs=[('operation-observation','Observation'),('improvement-candidate','Candidate'),('operation-monitor-policy','MonitorPolicy'),
    ('improvement-proposal','Proposal'),('operation-effect-comparison','EffectComparison')]
defs['Args_write_operation_document']={'type':'object','additionalProperties':False,'properties':{'document_kind':{'enum':[x[0] for x in pairs]},'payload':{'type':'object'}},
    'required':['document_kind','payload'],'oneOf':[{'properties':{'document_kind':{'const':kind},'payload':{'$ref':'#/$defs/'+selector}}} for kind,selector in pairs]}
defs['Args_set_operation_monitor_policy']={'type':'object','additionalProperties':False,'properties':{'policy_ref':{'$ref':'#/$defs/DocRef'},
    'authorization_source_refs':{'type':'array','minItems':1,'items':{'$ref':'#/$defs/Ref'}},'active':{'type':'boolean'}},'required':['policy_ref','authorization_source_refs','active']}
defs['Returns_set_operation_monitor_policy']={'type':'object','additionalProperties':False,'properties':{'policy_ref':{'$ref':'#/$defs/DocRef'},
    'active':{'type':'boolean'},'automatic_llm_calls':{'const':False}},'required':['policy_ref','active','automatic_llm_calls']}
for name,response in [('resolve_runtime_config','Returns_resolve_runtime_config'),('write_operation_document','DocRef'),('set_operation_monitor_policy','Returns_set_operation_monitor_policy')]:
    registry['public_interfaces'][name]={'request':'Args_'+name,'response':response}
    library['x-contract-policy']['active_roots']=sorted(set(library['x-contract-policy']['active_roots'])|{'Args_'+name,response})
def descriptions(value):
    if isinstance(value,dict):
        if 'description' in value:value['description']=value['description'].replace('每周维护','手动维护')
        for item in value.values():descriptions(item)
    elif isinstance(value,list):
        for item in value:descriptions(item)
descriptions(defs['SystemConfig']['properties']['service_defaults'])
registry['implementation_revision']=8;library['x-contract-policy']['implementation_revision']=8
write(contract/'schema-library-v12.json',library);write(contract/'registry-v12.json',registry)
manifest=read(contract/'manifest.json');manifest['implementation_revision']=8;manifest['files']={p.name:digest(p) for p in contract.glob('*.json') if p.name!='manifest.json'};write(contract/'manifest.json',manifest)
(templates/'schemas/schema-library.json').write_bytes((contract/'schema-library-v12.json').read_bytes())
profiles=read(templates/'profiles.json')
def refresh(value):
    if isinstance(value,dict):
        if str(value.get('id','')).startswith('v12-template/'):
            relative=value['id'].removeprefix('v12-template/');path=templates/relative
            if path.is_file():value.update(sha256=digest(path),uri='kernel://template-assets/'+digest(path)+'/'+relative)
        for item in value.values():refresh(item)
    elif isinstance(value,list):
        for item in value:refresh(item)
refresh(profiles);write(templates/'profiles.json',profiles)
manifest=read(templates/'manifest.json');manifest['contract_library_sha256']=digest(contract/'schema-library-v12.json')
manifest['files']={p.relative_to(templates).as_posix():digest(p) for p in templates.rglob('*') if p.is_file() and p!=templates/'manifest.json'}
manifest['bundle_sha256']=hashlib.sha256(json.dumps(manifest['files'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest();write(templates/'manifest.json',manifest)
print(json.dumps({'contract_revision':8,'contract_sha256':digest(contract/'schema-library-v12.json'),'template_sha256':manifest['bundle_sha256']}))
