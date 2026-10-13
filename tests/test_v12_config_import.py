import json
from pathlib import Path

from assistant_kernel.config.system_config import validate_system_config
from scripts.build_system_config_candidate import build
from test_core4 import core_settings
from test_v12_foundations import example_config


async def test_provider_import_preserves_model_catalog_and_explicit_empty(core_settings, tmp_path):
    settings=core_settings.model_copy(update={'workspace_root':tmp_path.parent})
    settings.provider_secrets_path.write_text(json.dumps({'providers':{'fixture':{
        'enabled':True,'api_key':'private-fixture-value','native_base_url':'',
        'models':{'embedding':'','reranker':'','multimodal_embedding':'',
                  'multimodal_reranker':'','vision':'','document_ocr':''},
        'chat_models':{'my-model':{'display_name':'Fixture model','model_id':'actual-model',
            'endpoint_id':'','base_url':'','api_style':'openai','api_key_env':'FIXTURE_API_KEY',
            'input_price_per_million_tokens_usd':0,'capabilities':{'function_calling':True}}}}}}))
    config=example_config()
    containers={name:{'name':name,'image':spec['image'],'nano_cpus':0,'memory_bytes':0,
        'ports':{},'environment':{},'compose_project':'ai'}
        for name,spec in config['infrastructure']['services'].items()}
    for name,internal,external in [('AI',8001,18001),('AI',8002,18002),('AI',5001,15001),
        ('rag-postgres',5432,15432),('redis',6379,16379),('rag-reranker',8080,18088)]:
        containers[name]['ports'][str(internal)+'/tcp']=[{'HostPort':str(external),'HostIp':'127.0.0.1'}]
    supervisor=tmp_path/'supervisor.conf'
    supervisor.write_text('[program:kernel-run-worker]\nnumprocs=2\n[program:kernel-runtime-worker]\nnumprocs=4\n')
    candidate,receipt=await build(list(containers.values()),supervisor,settings)
    assert receipt['import_ready']
    provider=candidate['providers']['fixture']
    assert all(v is None for v in provider['model_names'].values())
    assert provider['native_base_url'] is None
    model=provider['chat_models']['my-model']
    assert model['model_id']=='actual-model' and model['endpoint_id'] is None
    assert model['base_url'] is None and model['input_price_per_million_tokens_usd']==0
    assert model['pricing_verification']=={'status':'unverified','source_refs':[],'verified_at':None}
    assert len(receipt['explicit_empty_conversions'])>=9
    assert 'private-fixture-value' not in json.dumps([candidate,receipt])
    assert 'private-fixture-value' in settings.provider_secrets_path.read_text()
    model['api_key']='never-allowed'
    assert not validate_system_config(candidate)['valid']
