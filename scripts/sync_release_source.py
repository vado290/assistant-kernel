"""Apply one tested commit delta to the original source without resetting its Git state."""
from __future__ import annotations
import argparse,hashlib,json,subprocess,zipfile
from pathlib import Path

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path,required=True);p.add_argument('--original',type=Path,required=True);p.add_argument('--base',required=True);p.add_argument('--expected-source-manifest',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True);p.add_argument('--backup',type=Path,required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    original=a.original.resolve();candidate=a.candidate.resolve()
    expected=json.loads(a.expected_source_manifest.read_text('utf-8-sig'))['files']
    expected={x['path']:x['sha256'] for x in expected}
    for rel,sha in expected.items():
        path=original/rel
        if not path.is_file() or digest(path)!=sha:raise RuntimeError('original source changed since the accepted baseline: '+rel)
    current=subprocess.check_output(['git','-C',str(candidate),'rev-parse','HEAD'],text=True).strip()
    if subprocess.check_output(['git','-C',str(candidate),'status','--porcelain'],text=True).strip():raise RuntimeError('candidate is not committed')
    changed=subprocess.check_output(['git','-C',str(candidate),'diff','--name-only','-z',a.base,current]).decode().split('\0')
    entries=[]
    for rel in filter(None,changed):
        source=candidate/rel;target=original/rel
        if original not in target.resolve().parents:raise RuntimeError('source delta escaped the original repository')
        if rel not in expected and target.exists():raise RuntimeError('new release file collides with unrelated local content: '+rel)
        entries.append({'path':rel,'before_sha256':digest(target) if target.exists() else None,
                        'after_sha256':digest(source) if source.exists() else None})
    receipt={'source_commit':current,'base_commit':a.base,'original':str(original),'files':entries,'applied':False}
    if a.apply:
        if a.backup.exists():raise RuntimeError('source delta backup already exists')
        with zipfile.ZipFile(a.backup,'w',zipfile.ZIP_DEFLATED) as z:
            for item in entries:
                target=original/item['path']
                if target.is_file():z.write(target,item['path'])
            z.writestr('delta-manifest.json',json.dumps(receipt))
        written=[]
        try:
            for item in entries:
                source=candidate/item['path'];target=original/item['path']
                if (digest(target) if target.is_file() else None)!=item['before_sha256']:
                    raise RuntimeError('source changed during release synchronization: '+item['path'])
                if source.exists():
                    target.parent.mkdir(parents=True,exist_ok=True)
                    temporary=target.with_suffix(target.suffix+'.release.tmp')
                    if temporary.exists():raise RuntimeError('unexpected source staging file')
                    try:
                        temporary.write_bytes(source.read_bytes());temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                else:target.unlink()
                written.append(item)
        except Exception:
            conflicts=[]
            with zipfile.ZipFile(a.backup) as z:
                for item in reversed(written):
                    target=original/item['path']
                    if (digest(target) if target.is_file() else None)!=item['after_sha256']:
                        conflicts.append(item['path']);continue
                    if item['before_sha256'] is None:target.unlink(missing_ok=True)
                    else:target.write_bytes(z.read(item['path']))
            receipt['rollback_conflicts']=conflicts
            a.receipt.parent.mkdir(parents=True,exist_ok=True)
            a.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
            raise
        receipt['applied']=True
        receipt['files']=[{'path':rel,'sha256':digest(original/rel)} for rel in sorted(set(expected)|set(filter(None,changed))) if (original/rel).is_file()]
    a.receipt.parent.mkdir(parents=True,exist_ok=True);a.receipt.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps({'changed_paths':len(entries),'source_commit':current,'applied':a.apply,'original_git_not_reset':True}))
if __name__=='__main__':main()
