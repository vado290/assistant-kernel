"""Build/check a new immutable environment. Does not switch production or migrate DBs."""
from __future__ import annotations
import argparse,hashlib,json,os,subprocess,shutil,tempfile
from contextlib import contextmanager
from pathlib import Path
import re

def run(args,env,*,log):
    with log.open('ab') as stream:
        result=subprocess.run([str(x) for x in args],env=env,stdout=stream,stderr=subprocess.STDOUT)
    if result.returncode:raise RuntimeError(f'candidate preparation failed; restricted log: {log.name}')

@contextmanager
def clean_build_source(source,manifest):
    """Only committed manifest files enter the build; ignored build caches never do."""
    with tempfile.TemporaryDirectory(prefix='kernel-release-build-') as temporary:
        target=Path(temporary)
        for item in manifest['files']:
            original=source/item['path'];destination=target/item['path']
            if not destination.resolve().is_relative_to(target) or hashlib.sha256(original.read_bytes()).hexdigest()!=item['sha256']:
                raise RuntimeError('Build input differs from the fixed source manifest')
            destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(original,destination)
        yield target

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--data-root',type=Path,default=Path('/home/v/AI/assistant-data'));parser.add_argument('--source-manifest',type=Path,required=True);parser.add_argument('--receipt',type=Path,required=True);a=parser.parse_args()
    source=a.source_root.resolve()
    version=re.search(r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$',(source/'pyproject.toml').read_text(),re.M).group(1)
    target=a.data_root/'runtime/kernel-venvs'/version
    current=(a.data_root/'runtime/kernel-current.txt').read_text().strip()
    if version==current:raise RuntimeError('Never install over the running release')
    if (target/'.complete').exists():raise RuntimeError('Candidate is already sealed; verify it instead of overwriting')
    # Git is a host capability. Windows worktree .git paths are intentionally not
    # interpreted inside the Linux container; verify a host-exported byte manifest.
    source_manifest=json.loads(a.source_manifest.read_text('utf-8-sig'))
    commit=source_manifest['commit']
    for item in source_manifest['files']:
        path=source/item['path']
        if source not in path.resolve().parents or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            raise RuntimeError('Candidate differs from its committed source manifest')
    env=dict(os.environ);env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
    a.receipt.parent.mkdir(parents=True,exist_ok=True);log=a.receipt.with_suffix('.log')
    if not (target/'bin/python').exists():
        run(['/home/v/miniconda3/bin/python','-m','venv',target],env,log=log)
    python=target/'bin/python'
    run([python,'-m','pip','install','--no-compile','--disable-pip-version-check','-r',source/'requirements/kernel.lock'],env,log=log)
    with clean_build_source(source,source_manifest) as build_source:
        run([python,'-m','pip','install','--no-compile','--disable-pip-version-check','--no-deps',build_source],env,log=log)
    run([python,'-m','pip','check'],env,log=log)
    raw=subprocess.check_output([str(python),'-B',str(source/'scripts/verify_release.py'),'--source-root',str(source)],env=env)
    result=json.loads(raw)
    if result['version']!=version:raise RuntimeError('installed version differs')
    probe=subprocess.check_output([str(python),'-B','-c',
        'import assistant_kernel.api.main,assistant_kernel.worker.maintenance_main;from assistant_kernel.services.contracts import contracts;print(contracts().sha256)'],env=env,text=True).strip()
    result.update(source_commit=commit,source_root=str(source),environment=str(target),
                  contract_sha256=probe,source_manifest_sha256=hashlib.sha256(a.source_manifest.read_bytes()).hexdigest(),production_switched=False,previous_version=current)
    a.receipt.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    (target/'.complete').write_text(json.dumps({'commit':commit,'receipt_sha256':hashlib.sha256(a.receipt.read_bytes()).hexdigest()}))
    print(json.dumps({'prepared_version':version,'source_commit':commit,'matched_files':result['matched_files'],'production_switched':False}))
if __name__=='__main__':main()
