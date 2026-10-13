"""Render only existing managed consumers. Never restart, recreate or move data implicitly."""
import argparse,asyncio,configparser,hashlib,json,re,shlex
from pathlib import Path
from copy import deepcopy
import yaml
from assistant_kernel.config import KernelSettings
from assistant_kernel.services.system_config import SystemConfigService

def render_supervisor(text,config,data_root,*,external=False,logging_config=False):
    parser=configparser.RawConfigParser();parser.read_string(text)
    original={name:dict(parser[name]) for name in parser.sections()}
    for queue,section in [('interactive','program:kernel-run-worker'),('runtime','program:kernel-runtime-worker')]:
        if parser.has_section(section):parser.set(section,'numprocs',str(config['kernel']['workers'][queue]))
    for section in parser.sections():
        if section.startswith('program:kernel-') and logging_config:
            for stream in ('stdout','stderr'):
                parser.set(section,stream+'_logfile_maxbytes',str(config['logging']['max_file_bytes']))
                parser.set(section,stream+'_logfile_backups',str(config['logging']['backup_files']))
    if external and parser.has_section('program:docling-serve'):
        command=shlex.split(parser.get('program:docling-serve','command'));keep=[];skip=False
        for arg in command:
            if skip:skip=False;continue
            if arg in {'--port','--workers'}:skip=True;continue
            if arg.startswith(('--port=','--workers=')):continue
            keep.append(arg)
        keep+=['--port',str(config['document_service']['listen_port']),'--workers',str(config['document_service']['workers'])]
        parser.set('program:docling-serve','command',' '.join(shlex.quote(x) for x in keep))
    if not parser.has_section('include'):parser.add_section('include')
    paths=parser.get('include','files',fallback='').split();managed=str(data_root/'runtime/kernel-services/programs/*.conf')
    if managed not in paths:paths.append(managed)
    parser.set('include','files',' '.join(paths))
    changed=[name for name in parser.sections() if dict(parser[name])!=original.get(name)]
    for name,values in original.items():
        if not name.startswith('program:kernel-') and name!='include' and not (external and name=='program:docling-serve'):
            assert dict(parser[name])==values,'An unrelated service setting changed'
    from io import StringIO
    out=StringIO();parser.write(out);return out.getvalue(),changed

def render_compose(value,config,*,allow_storage=False):
    result=deepcopy(value);changes=[];pending=[];services=result.get('services',{})
    # Existing service/container identities only; a sample Redis definition never creates a new platform.
    for key,service in services.items():
        name=service.get('container_name',key);wanted=config['infrastructure']['services'].get(name)
        if not wanted:continue
        for field,val in [('image',wanted['image']),('cpus',wanted['cpu_limit']),('mem_limit',str(wanted['memory_limit_mb'])+'m' if wanted['memory_limit_mb'] else None)]:
            if val is None:service.pop(field,None)
            else:service[field]=val
        labels={'AI':[('kernel_api',config['kernel']['api']['port']),('kernel_mcp',config['kernel']['mcp']['port']),('document_service',config['document_service']['listen_port'])],
            'rag-postgres':[('postgres',config['postgres']['port'])],'rag-reranker':[('reranker',config['reranker']['listen_port'])],
            'redis':[('redis',config['redis']['port'])]}.get(name,[])
        known_targets={target for _,target in labels}
        ports=[]
        for old in service.get('ports',[]):
            target=old.get('target') if isinstance(old,dict) else str(old).split(':')[-1].split('/')[0]
            if str(target).isdigit() and int(target) in known_targets:continue
            ports.append(old)
        ports += [{'target':target,'published':str(config['infrastructure']['host_ports'][label]),'host_ip':config['infrastructure']['host_bind_address'],'protocol':'tcp'} for label,target in labels]
        if labels:service['ports']=ports
        for storage in config['infrastructure']['storage'].values():
            for index,mount in enumerate(service.get('volumes',[])):
                if isinstance(mount,dict):target=mount.get('target')
                else:
                    parts=str(mount).split(':');target=parts[-2] if parts[-1] in {'ro','rw'} else parts[-1]
                if target!=storage['target']:continue
                next_mount={'type':storage['type'],'source':storage['source'],'target':storage['target']}
                if mount!=next_mount:
                    if allow_storage:service['volumes'][index]=next_mount
                    else:pending.append({'service':name,'target':target,'reason':'storage changes require separate explicit migration'})
        if service!=value['services'][key]:changes.append(name)
    return result,changes,pending

async def main():
    p=argparse.ArgumentParser();p.add_argument('--supervisor-input',type=Path);p.add_argument('--compose-input',type=Path)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--expected-revision',required=True)
    p.add_argument('--external',action='store_true');p.add_argument('--logging',action='store_true');p.add_argument('--allow-storage-migration',action='store_true');a=p.parse_args()
    settings=KernelSettings.from_environment();service=SystemConfigService(settings);state=await service.state()
    if not state or state['revision']!=a.expected_revision:raise ValueError('Configuration revision changed before rendering')
    config=service._config(state['desired_ref']);pending=[]
    if a.supervisor_input:
        raw,changed=render_supervisor(a.supervisor_input.read_text(),config,settings.data_root,external=a.external,logging_config=a.logging)
        pending=['document_service.request_timeout_seconds is a caller timeout, not the Docling keep-alive option'] if a.external else []
    elif a.compose_input:
        data,changed,pending=render_compose(yaml.safe_load(a.compose_input.read_text()),config,allow_storage=a.allow_storage_migration)
        raw=yaml.safe_dump(data,sort_keys=False,allow_unicode=True)
    else:raise ValueError('An actual current consumer configuration is required')
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(raw)
    receipt={'source_ref':state['desired_ref'],'rendered_path':str(a.output),'sha256':hashlib.sha256(a.output.read_bytes()).hexdigest(),
        'changed_consumers':changed,'pending':pending,'applied':False,'requires_operator_application':True}
    a.output.with_suffix(a.output.suffix+'.receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':asyncio.run(main())
