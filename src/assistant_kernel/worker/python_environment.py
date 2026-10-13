"""Business subprocess parameters; this is not an OS isolation boundary."""
from pathlib import Path
import os

SAFE_SYSTEM_ENV=('PATH','LANG','LC_ALL','LC_CTYPE','TZ','SYSTEMROOT','WINDIR','PATHEXT')
NUMERIC_THREADS=('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS',
    'VECLIB_MAXIMUM_THREADS','BLIS_NUM_THREADS')


def python_environment(run:dict,output_root:Path)->dict[str,str]:
    environment={key:os.environ[key] for key in SAFE_SYSTEM_ENV if key in os.environ}
    defaults=((run.get('request_json') or {}).get('capacity_reservation') or {}).get('python_defaults') or {}
    resources=(run.get('request_json') or {}).get('runtime_resources') or {}
    threads=int(resources.get('numeric_threads',defaults.get('numeric_threads',1)))
    if threads<1:raise ValueError('Numeric library threads must be positive')
    environment.update({key:str(threads) for key in NUMERIC_THREADS})
    environment.update(HOME=str(output_root),TMPDIR=str(output_root),PYTHONUNBUFFERED='1')
    return environment


def business_credentials(data_root,project,refs):
    """Resolve only this manifest's Project credentials; never copy Kernel secrets."""
    from assistant_kernel.services.project_credentials import ProjectCredentialsStore
    store=ProjectCredentialsStore(data_root);environment={};bindings=[]
    prefix='secret://projects/'+project+'/'
    for ref in refs:
        uri=ref['uri']
        if not uri.startswith(prefix):raise PermissionError('Business credentials must belong to the same Project secret store')
        key=uri.removeprefix(prefix);record=store.get(project,key)
        if record['status']!='active':raise PermissionError('Business credential is inactive')
        revision=ref.get('revision_id')
        if revision is not None and revision!=str(record['revision']):raise PermissionError('Pinned business credential revision changed')
        fields=record['fields']
        if not fields:raise ValueError('Business credential has no declared environment bindings')
        for name,value in fields.items():
            if name in {*SAFE_SYSTEM_ENV,*NUMERIC_THREADS,'HOME','TMPDIR'} or name.startswith(('ASSISTANT_','KERNEL_','PYTHON','LD_')):
                raise PermissionError('Business credential cannot override execution control environment')
            if name in environment and environment[name]!=value:raise ValueError('Selected business credentials have conflicting environment fields')
            environment[name]=value
        bindings.append({'ref':ref,'actual_revision':record['revision'],'environment_names':sorted(fields)})
    return environment,bindings
