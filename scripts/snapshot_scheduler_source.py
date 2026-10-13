"""Recoverable two-commit release snapshot without staging the user's dirty checkout."""
import argparse,json,shutil,subprocess
from pathlib import Path

EXCLUDED={'.git','__pycache__','.pytest_cache','.venv','venv','build','dist'}

def files(root):
    return {p.relative_to(root).as_posix():p for p in root.rglob('*') if p.is_file()
        and not any(part in EXCLUDED or part.endswith('.egg-info') for part in p.relative_to(root).parts)
        and p.suffix not in {'.pyc','.pyo'}}

def git(root,*args):return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()

def copy(source,target):
    entries=files(source)
    for rel,path in entries.items():
        dest=target/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
    for rel in git(target,'ls-files','-z').split('\0'):
        if rel and rel not in entries:
            path=(target/rel).resolve()
            if not path.is_relative_to(target.resolve()):raise ValueError('Snapshot path escaped destination')
            path.unlink(missing_ok=True)

def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--source',type=Path,required=True)
    p.add_argument('--destination',type=Path,required=True);p.add_argument('--seal',action='store_true')
    p.add_argument('--branch',default='codex/scheduler-0310');p.add_argument('--baseline-version',default='0.30.2');a=p.parse_args()
    target=a.destination.resolve()
    if target==a.source.resolve() or target==a.baseline.resolve():raise ValueError('Snapshot must have its own directory')
    if not (target/'.git').exists():
        if target.exists() and any(target.iterdir()):raise ValueError('Snapshot destination is not empty')
        target.mkdir(parents=True,exist_ok=True);git(target,'init','-b',a.branch)
        git(target,'config','user.name','Kernel release snapshot');git(target,'config','user.email','kernel-release@local')
        git(target,'config','core.autocrlf','false');git(target,'config','core.hooksPath','.disabled-hooks')
        (target/'.git/info/attributes').write_text('* -text\n')
        copy(a.baseline,target);git(target,'add','-A');git(target,'commit','-m','Baseline actual '+a.baseline_version+' source bytes before scheduler changes')
    if git(target,'branch','--show-current')!=a.branch:raise ValueError('Not the scheduler snapshot repository')
    copy(a.source,target)
    if a.seal and git(target,'status','--porcelain'):
        git(target,'add','-A');git(target,'commit','-m','Implement Kernel schedules, transactional dispatch, role integration and controlled rollout')
    print(json.dumps({'snapshot':str(target),'commit':git(target,'rev-parse','HEAD'),'sealed':a.seal,'files':len(files(target))}))

if __name__=='__main__':main()
