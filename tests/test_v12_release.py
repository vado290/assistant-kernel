import hashlib,json,subprocess,sys
import shutil
from pathlib import Path

def git(root,*args):
    if shutil.which('git') is None:
        import pytest
        pytest.skip('Source publication is tested on the Git-equipped deployment host')
    return subprocess.check_output(['git','-c','user.name=Fixture','-c','user.email=fixture@invalid','-C',str(root),*args],stderr=subprocess.DEVNULL,text=True).strip()

def setup(tmp_path):
    original=tmp_path/'original';candidate=tmp_path/'candidate'
    original.mkdir();candidate.mkdir()
    for root in [original,candidate]:
        (root/'a.py').write_text('old\n');(root/'local-notes.md').write_text('keep user changes\n')
    git(candidate,'init');git(candidate,'add','.');git(candidate,'commit','-m','baseline');base=git(candidate,'rev-parse','HEAD')
    expected=tmp_path/'expected.json';expected.write_text(json.dumps({'files':[{'path':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in original.iterdir()]}))
    (candidate/'a.py').write_text('new\n');(candidate/'new.json').write_text('{}')
    git(candidate,'add','.');git(candidate,'commit','-m','candidate')
    command=[sys.executable,str(Path(__file__).parents[1]/'scripts/sync_release_source.py'),
             '--candidate',str(candidate),'--original',str(original),'--base',base,
             '--expected-source-manifest',str(expected),'--receipt',str(tmp_path/'receipt.json'),
             '--backup',str(tmp_path/'backup.zip')]
    return original,candidate,command

def test_source_sync_preview_and_apply_preserve_unrelated_changes(tmp_path):
    original,candidate,command=setup(tmp_path)
    subprocess.run(command,check=True,capture_output=True)
    assert (original/'a.py').read_text()=='old\n'
    subprocess.run([*command,'--apply'],check=True,capture_output=True)
    assert (original/'a.py').read_bytes()==(candidate/'a.py').read_bytes()
    assert (original/'local-notes.md').read_text()=='keep user changes\n'
    assert (tmp_path/'backup.zip').exists()

def test_source_sync_stops_before_any_write_on_baseline_drift(tmp_path):
    original,_,command=setup(tmp_path)
    (original/'local-notes.md').write_text('changed elsewhere')
    result=subprocess.run([*command,'--apply'],capture_output=True)
    assert result.returncode!=0
    assert (original/'a.py').read_text()=='old\n'
    assert not (tmp_path/'backup.zip').exists()

def test_source_sync_rolls_back_its_partial_write_without_touching_collision(tmp_path):
    original,_,command=setup(tmp_path)
    (original/'new.json.release.tmp').write_text('belongs to another pending writer')
    result=subprocess.run([*command,'--apply'],capture_output=True)
    assert result.returncode!=0
    assert (original/'a.py').read_text()=='old\n'
    assert not (original/'new.json').exists()
    assert (original/'new.json.release.tmp').read_text()=='belongs to another pending writer'
    assert json.loads((tmp_path/'receipt.json').read_text())['rollback_conflicts']==[]
