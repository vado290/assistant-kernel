from copy import deepcopy
import json

import pytest
from assistant_kernel.services.public_background import PublicBackgroundService
from test_core4 import core_settings
from test_v12_foundations import source


def payload():
    ref=source()
    def fact(name,value):return {'field_id':name,'category':'user_background','value':value,
        'basis':'user_confirmed','confirmed':True,'confirmed_at':'2026-10-07T01:00:00Z',
        'status':'active','source_refs':[ref],'recorded_at':'2026-10-07T01:00:00Z',
        'verified_at':None,'revoked_at':None,'purpose':'shared_project_context'}
    return {'schema_version':'public-background.v1','owner_project_id':'kernel',
        'updated_at':'2026-10-07T01:00:00Z','facts':[fact('user.profession','engineer'),fact('user.language','zh-CN')],
        'verified_runtime_refs':[],'legacy_source_refs':[]}


async def test_background_is_db_accepted_and_unselected_change_does_not_rebuild(core_settings):
    service=PublicBackgroundService(core_settings.data_root,core_settings)
    with pytest.raises(PermissionError,match="retired"):
        await service.accept(payload(),expected_ref=None,approval_refs=[source()],reason="old caller")
    assert service.read_project_context("demo")["documents"]==[]


async def test_selected_fact_revision_changes_subset_but_empty_selection_is_stable(core_settings):
    service=PublicBackgroundService(core_settings.data_root,core_settings)
    with pytest.raises(PermissionError,match="retired"):
        await service.accept(payload(),expected_ref=None,approval_refs=[source()],reason="old caller")
    assert service.read_project_context("demo")["documents"]==[]
