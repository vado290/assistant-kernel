import json,hashlib,sys,uuid,importlib.metadata
from pathlib import Path
import pytest
from datetime import datetime,timezone

from assistant_kernel.services.contracts import canonical_json
from assistant_kernel.services.document_runtime import TaskDocumentService,resource_ref
from assistant_kernel.services.fixed_python import FixedPython
from assistant_kernel.storage import ProjectRuntimeManager
from test_core4 import core_settings,prepare_agent
from test_v12_foundations import example_config
from assistant_kernel.config.system_config import resolve_config_bindings


def fixture(settings,*,code='import json\nprint(42)\n',inputs=None):
    prepare_agent(settings);root=ProjectRuntimeManager(settings.data_root).ensure('demo').vault_root
    def source(name,value):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        raw=value if isinstance(value,bytes) else canonical_json(value)
        path.write_bytes(raw)
        return resource_ref(kind='source',identity=name,uri='vault://demo/'+name,sha256=hashlib.sha256(raw).hexdigest(),purpose='isolated fixed input')
    entry=source('program/main.py',code.encode())
    index=source('program/bundle.json',{'schema_version':'python-code-bundle.v1','files':[{'relative_path':'main.py','ref':entry}]})
    closure=source('program/closure.json',{'schema_version':'python-import-closure.v1','stdlib_modules':['json'],'dependency_modules':[]})
    interpreter=Path(sys.executable);site=str(interpreter.parent.parent/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages')
    versions={x.metadata['Name'].lower().replace('_','-'):x.version for x in importlib.metadata.distributions(path=[site])}
    environment=source('program/environment.json',{'schema_version':'python-environment-fingerprint.v1','interpreter_path':str(interpreter),
        'python_minor':f'{sys.version_info.major}.{sys.version_info.minor}','site_packages':[site],'distribution_versions':versions})
    exe=resource_ref(kind='source',identity='fixed-python',uri=str(interpreter),sha256=hashlib.sha256(interpreter.read_bytes()).hexdigest(),purpose='actual interpreter bytes')
    auth=source('program/fixture-authorization.json',{'scope':'isolated materialisation fixture; not human or business authorization evidence'})
    documents=TaskDocumentService(settings)
    global_ref=documents.write_scoped_revision(project_id='kernel',scope='project',object_id='fixture-config',document_kind='kernel-system-config',
        document_schema='kernel-system-config.v1',payload=example_config(),status='submitted',actor_type='kernel',actor_id='fixture',reason='Structural fixture only',source_refs=[])
    snapshot=resolve_config_bindings(example_config(),global_ref,[{'path':'/python_defaults/numeric_threads','mode':'inherit','override_value':None}],
        owner_ref=index,captured_at=datetime.now(timezone.utc).isoformat(),effective_group_refs=[])
    resolved=documents.write_scoped_revision(project_id='demo',scope='project',object_id='fixture-config',document_kind='resolved-config-snapshot',
        document_schema='resolved-system-config.v1',payload=snapshot,status='submitted',actor_type='kernel',actor_id='fixture',reason='Fixed structural configuration fixture',source_refs=[global_ref])
    resources={'mode':'sequential','max_workers':1,'numeric_threads':1,'cpu_budget':1,'expected_peak_memory_mb':None,'resource_policy_ref':None,
        'timeout_seconds':30,'max_input_bytes':10000,'max_output_bytes':10000,'io_limit':1,'cpu_pool_size':0,'start_method':'not_applicable','log_limit_bytes':10000,
        'resolved_config_ref':resolved,'declared_cpu_slots':1}
    manifest={'script_id':'fixture-main','code_bundle':{'bundle_ref':index,'entrypoint_ref':entry,'module_refs':[],'closure_manifest_ref':closure},
        'environment':{'environment_ref':environment,'interpreter_ref':exe,'dependencies_kind':'stdlib_only','dependency_lock_ref':None,'dependency_manifest_ref':None},
        'fixed_args':['fixed'], 'io':{'input_schema':{'type':'object','properties':{'n':{'type':'integer'}},'required':['n'],'additionalProperties':False},
            'output_schema':{'type':'object','properties':{'answer':{'type':'integer'}},'required':['answer'],'additionalProperties':False},
            'cwd_uri':'attempt://code','read_refs':[],'write_targets':[],'required_outputs':['result.json'],'stdout_capture':True,'stderr_capture':True,
            'output_root_uri':'attempt://output','input_bindings':[],'secret_refs':[],'business_check_refs':[]},
        'resources':resources,'purpose':{'kind':'auxiliary','description':'Isolated preflight fixture','authorization_ref':auth,'turn_boundary_ref':auth},
        'expected_effects':[],'shared_write':False,'children':{'allow_detached':False,'wait_for_all':True,'terminate_group_on_timeout':True,
            'terminate_group_on_cancel':True,'terminal_requires_cleanup_receipt':True},
        'retry':{'max_attempts':1,'retry_authorization_ref':None,'same_binding':True,'unknown_effect':'reconcile_then_wait'},
        'ownership':{'project_id':'demo','creating_task_ref':None,'creating_invocation_ref':None,'lifecycle_relation':'independent_operation','initial_authorization_ref':auth},
        'provenance_source_refs':[auth],'idempotence_statement':'No business effects','verification_refs':[],'resolved_config_ref':resolved}
    ref=TaskDocumentService(settings).write_scoped_revision(project_id='demo',scope='project',object_id='fixture-main',document_kind='python-script-manifest',
        document_schema='python-script-manifest.v1',payload=manifest,status='submitted',actor_type='kernel',actor_id='fixture',reason='Isolated structural fixture',source_refs=[auth])
    run={'project_id':'demo','run_id':str(uuid.uuid4()),'attempt':1,'request_json':{'manifest_ref':ref,'input':inputs or {'n':42}}}
    return root,run,manifest,source


def test_immutable_code_materialisation_and_required_output_are_real(core_settings):
    root,run,manifest,source=fixture(core_settings);service=FixedPython(core_settings)
    with pytest.raises(PermissionError,match='authorization gate'):service.prepare(run=run,authorization_verified=False)
    actual=service.prepare(run=run,authorization_verified=True)
    assert actual['source'].read_bytes()==b'import json\nprint(42)\n' and actual['args']==['fixed']
    with pytest.raises(ValueError,match='required output is missing'):service.check_outputs(actual,{'answer':42},actual['attempt_root'])
    (actual['attempt_root']/'result.json').write_text('{"answer":42}')
    result=service.check_outputs(actual,{'answer':42},actual['attempt_root'])
    assert result=={'contract_status':'passed','business_status':'unverified','effect_status':'none'}


def test_changed_source_is_rejected_before_execution(core_settings):
    root,run,_,_=fixture(core_settings);(root/'program/main.py').write_text('print("changed")')
    with pytest.raises(ValueError,match='source bytes changed'):FixedPython(core_settings).prepare(run=run,authorization_verified=True)


def test_unlisted_import_and_wrong_input_are_rejected(core_settings):
    root,run,_,_=fixture(core_settings,code='import os\n')
    with pytest.raises(ValueError,match='absent from the fixed closure'):FixedPython(core_settings).prepare(run=run,authorization_verified=True)
    run['request_json']['input']={'n':'wrong type'}
    with pytest.raises(ValueError,match='Schema failed'):FixedPython(core_settings).prepare(run=run,authorization_verified=True)
