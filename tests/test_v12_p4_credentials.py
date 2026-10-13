from copy import deepcopy
from datetime import datetime,timezone
import pytest
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.worker.python_environment import business_credentials


def setup(tmp_path):
    store=ProjectCredentialsStore(tmp_path);doc=store._empty('demo');now=datetime.now(timezone.utc).isoformat()
    doc['credentials']={'selected':{'credential_key':'selected','purpose':'synthetic business fixture','revision':2,
        'status':'active','created_at':now,'updated_at':now,'values':{'token':{'value':'synthetic-selected','purpose':'fixture token','env':'BUSINESS_TOKEN'}}},
        'unselected':{'credential_key':'unselected','purpose':'unselected fixture','revision':1,'status':'active',
        'created_at':now,'updated_at':now,'values':{'token':{'value':'synthetic-unselected','purpose':'fixture token','env':'UNSELECTED_TOKEN'}}}}
    store._atomic_json(store.path('demo'),doc)
    ref=resource_ref(kind='source',identity='selected',uri='secret://projects/demo/selected',revision_id='2',purpose='explicit selected Project credential')
    return store,doc,ref


def test_only_explicit_project_credentials_are_resolved_and_receipts_have_no_values(tmp_path):
    _,_,ref=setup(tmp_path)
    environment,receipt=business_credentials(tmp_path,'demo',[ref])
    assert environment=={'BUSINESS_TOKEN':'synthetic-selected'}
    assert receipt==[{'ref':ref,'actual_revision':2,'environment_names':['BUSINESS_TOKEN']}]
    with pytest.raises(PermissionError,match='same Project'):business_credentials(tmp_path,'other',[ref])
    with pytest.raises(PermissionError,match='revision changed'):business_credentials(tmp_path,'demo',[{**ref,'revision_id':'1'}])


def test_project_credential_cannot_override_python_or_kernel_control(tmp_path):
    store,doc,ref=setup(tmp_path)
    doc['credentials']['selected']['values']['token']['env']='PYTHONPATH'
    store._atomic_json(store.path('demo'),doc)
    with pytest.raises(PermissionError,match='control environment'):business_credentials(tmp_path,'demo',[ref])
