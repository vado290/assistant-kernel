"""Refresh a reviewed local template candidate; never publish/adopt it."""
from pathlib import Path
import hashlib,json


def refresh(root):
    path=root/'profiles.json';profiles=json.loads(path.read_text('utf-8'))
    def update(value):
        if isinstance(value,dict):
            uri=value.get('uri','')
            if uri.startswith('kernel://template-assets/'):
                relative=uri.removeprefix('kernel://template-assets/').split('/',1)[1]
                digest=hashlib.sha256((root/relative).read_bytes()).hexdigest()
                value.update(uri='kernel://template-assets/'+digest+'/'+relative,sha256=digest)
            for child in value.values():update(child)
        elif isinstance(value,list):
            for child in value:update(child)
    update(profiles)
    path.write_text(json.dumps(profiles,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8',newline='\n')
    path=root/'manifest.json';manifest=json.loads(path.read_text('utf-8'))
    manifest['files']={relative:hashlib.sha256((root/relative).read_bytes()).hexdigest() for relative in manifest['files']}
    for source in manifest['design_sources']:source['sha256']=manifest['files'][source['path']]
    manifest['bundle_sha256']=hashlib.sha256(json.dumps(manifest['files'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    path.write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps({'bundle_sha256':manifest['bundle_sha256'],'adopted_projects':[]}))

if __name__=='__main__':refresh(Path(__file__).parents[1]/'src/assistant_kernel/prompts/v12')
