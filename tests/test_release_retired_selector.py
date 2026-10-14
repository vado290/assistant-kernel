"""An old intake snapshot stays readable but must not enter the new package."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
from types import SimpleNamespace

from scripts import adopt_mcp_release as release


def test_adoption_omits_retired_intake_without_mutating_old_package(tmp_path,monkeypatch):
    root=tmp_path/'runtime/template-bundles/old';root.mkdir(parents=True)
    (root/'manifest.json').write_text(json.dumps({'files':{'profiles.json':'old-profile'}}))
    @dataclass
    class Config:
        agent_key:str
        phase_mode:str='requirements'
        allowed_tools:tuple=('read','legacy_extra')
        config_sha256:str='old'
        source_files:tuple=(str(root/'role.md'),)
    configs={key:Config('project:kernel:'+actor) for key,actor in (
        ('project:kernel:intake/default','intake'),
        ('project:kernel:manager/requirements','manager'),
        ('project:kernel:supervisor/default','supervisor'))}
    package={'template_ref':{'sha256':'old-profile'},
             'configurations':{k:{'uri':k,'sha256':'snapshot'} for k in configs}}
    before=deepcopy(package)
    class Service:
        loader=SimpleNamespace(load_snapshot=lambda project,uri,digest:configs[uri])
        @contextmanager
        def connection(self):
            yield SimpleNamespace(execute=lambda _:SimpleNamespace(fetchall=lambda:[
                {'key':'project-runtime-package:kernel','value':{'package_ref':{'id':'old'}}}]))
        def read_package(self,*args):return package
    bundle=SimpleNamespace(ref=lambda _: {'sha256':'new-profile'},
        profiles={'mcp_tool_policy':{'phases':{'requirements':['read']},'actors':{'supervisor':['read']}}})
    monkeypatch.setattr(release,'PublishedAgentService',lambda _:Service())
    monkeypatch.setattr(release,'TemplateBundle',lambda *args:bundle)
    monkeypatch.setattr(release,'refresh_config',lambda config,*args:config)
    result=release.adopt(SimpleNamespace(data_root=tmp_path),tmp_path/'unused-decision',apply=False)[0]
    assert result['retired_selectors_omitted']==['project:kernel:intake/default']
    assert result['tool_counts']=={'project:kernel:manager/requirements':1,'project:kernel:supervisor/default':1}
    assert result['old_tasks_rebound'] is False and result['applied'] is False
    assert package==before and configs['project:kernel:intake/default'].config_sha256=='old'
