"""Deterministic additive P4 writer contract; only edits this candidate checkout."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]/'src/assistant_kernel';contract=root/'contracts';templates=root/'prompts/v12'
def read(p):return json.loads(p.read_text('utf-8'))
def write(p,value):p.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8')
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
library=read(contract/'schema-library-v12.json');registry=read(contract/'registry-v12.json')
library['$defs']['Args_write_runtime_document']={'type':'object','additionalProperties':False,
    'properties':{'document_kind':{'enum':['python-script-manifest','runtime-unit-definition','activation-report']},'payload':{'type':'object'}},
    'required':['document_kind','payload'],'oneOf':[{'properties':{'document_kind':{'const':kind},'payload':{'$ref':'#/$defs/'+definition}}}
        for kind,definition in [('python-script-manifest','V12_PythonManifest'),('runtime-unit-definition','V12_RuntimeProgramDefinition'),('activation-report','Activation')]]}
registry['public_interfaces']['write_runtime_document']={'request':'Args_write_runtime_document','response':'DocRef'}
registry['implementation_revision']=7;library['x-contract-policy']['implementation_revision']=7
library['x-contract-policy']['active_roots']=sorted(set(library['x-contract-policy']['active_roots'])|{'Args_write_runtime_document'})
write(contract/'schema-library-v12.json',library);write(contract/'registry-v12.json',registry)
manifest=read(contract/'manifest.json');manifest['implementation_revision']=7
manifest['files']={p.name:digest(p) for p in contract.glob('*.json') if p.name!='manifest.json'};write(contract/'manifest.json',manifest)
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
manifest['bundle_sha256']=hashlib.sha256(json.dumps(manifest['files'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
write(templates/'manifest.json',manifest)
print(json.dumps({'contract_revision':7,'contract_sha256':digest(contract/'schema-library-v12.json'),'template_sha256':manifest['bundle_sha256']}))
