"""A required Skill can change without changing profiles.json."""
import json,shutil
from pathlib import Path
from test_core4 import core_settings
from test_v12_p3_admission import complete_package
from assistant_kernel.services.template_bundle import TemplateBundle
from scripts.refresh_v12_template_manifest import refresh
from scripts.adopt_scheduler_release import adopt_packages
from assistant_kernel import __version__


def test_skill_only_release_is_adopted_and_then_idempotent(core_settings,monkeypatch):
    current=TemplateBundle();old_root=core_settings.data_root/'old-template'
    shutil.copytree(current.root,old_root)
    skill=old_root/'skills/assistant-prompt-assembly/SKILL.md'
    skill.write_text(skill.read_text()+'\nSynthetic previous Skill version.\n');refresh(old_root)
    old=TemplateBundle(old_root)
    assert old.ref('profiles.json')==current.ref('profiles.json') and old.sha256!=current.sha256
    with monkeypatch.context() as patch:
        patch.setattr('test_v12_p3_admission.TemplateBundle',lambda:old)
        service,prior=complete_package(core_settings)
    decision=core_settings.data_root/'synthetic-adoption.json'
    decision.write_text(json.dumps({'authority':'actual_user_request','release':__version__,'fixture':True,
        'user_request':'Synthetic isolated approval fixture, never a production user instruction','existing_task_ids':[]}))
    report=adopt_packages(core_settings,decision,apply=True)
    assert report[0]['applied'] and report[0]['package_ref']!=prior
    assert adopt_packages(core_settings,decision,apply=True)[0]['already_current']
