"""Host-side committed source export; no container Git or working-tree reset required."""
import argparse,hashlib,json,subprocess
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1]);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    git=['git','-C',str(a.source_root)]
    if subprocess.check_output(git+['status','--porcelain'],text=True).strip():raise RuntimeError('Source has uncommitted changes')
    commit=subprocess.check_output(git+['rev-parse','HEAD'],text=True).strip()
    paths=subprocess.check_output(git+['ls-files','-z']).decode().split('\0')
    paths=[rel for rel in paths if rel and (a.source_root/rel).is_file()]
    if any('\n' in rel or '\r' in rel for rel in paths):raise RuntimeError('Unsupported newline in release path')
    # Git may report a CRLF working file clean while its committed bytes are LF.
    # A release SHA must describe the recoverable commit, including JSON hashes.
    encoded=subprocess.check_output(git+['cat-file','--batch'],input=''.join(commit+':'+rel+'\n' for rel in paths).encode())
    cursor=0;files=[]
    for rel in paths:
        end=encoded.index(b'\n',cursor);header=encoded[cursor:end].split()
        if len(header)!=3 or header[1]!=b'blob':raise RuntimeError('Release contains a non-blob path')
        size=int(header[2]);start=end+1;blob=encoded[start:start+size];cursor=start+size+1
        if (a.source_root/rel).read_bytes()!=blob:
            raise RuntimeError('Working bytes differ from the recoverable commit; normalize before release: '+rel)
        files.append({'path':rel,'sha256':hashlib.sha256(blob).hexdigest()})
    result={'commit':commit,'files':files}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'commit':commit,'files':len(result['files'])}))
if __name__=='__main__':main()
