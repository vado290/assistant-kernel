import hashlib
from pathlib import Path
import pytest
import assistant_kernel
from scripts.prepare_release import clean_build_source
from scripts.verify_release import verify

def test_build_uses_only_fixed_source_and_excludes_stale_build_cache(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'pyproject.toml').write_text('current')
    (source/'build').mkdir();(source/'build/retired.md').write_text('old schedule skill')
    manifest={'files':[{'path':'pyproject.toml','sha256':hashlib.sha256(b'current').hexdigest()}]}
    with clean_build_source(source,manifest) as clean:
        assert (clean/'pyproject.toml').read_text()=='current' and not (clean/'build').exists()
    assert not clean.exists() and (source/'build/retired.md').exists()

def test_installed_verifier_rejects_deleted_source_left_in_wheel(tmp_path,monkeypatch):
    source=tmp_path/'source';package=source/'src/assistant_kernel';package.mkdir(parents=True)
    installed=tmp_path/'installed';installed.mkdir()
    for root in (package,installed):(root/'__init__.py').write_text('same')
    monkeypatch.setattr(assistant_kernel,'__file__',str(installed/'__init__.py'))
    assert verify(source)['matched_files']==1
    (installed/'retired.md').write_text('old schedule skill')
    with pytest.raises(RuntimeError,match='absent from source'):verify(source)
