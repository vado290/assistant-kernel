"""Apply approved C26 config projection; preserve all prior config documents/Refs."""
import argparse,asyncio,hashlib,json
from pathlib import Path
from assistant_kernel.config import KernelSettings
from assistant_kernel.services.system_config import SystemConfigService
from assistant_kernel.services.document_runtime import resource_ref
from assistant_kernel.services.scheduling_scope import active_configuration,RETIRED_CONFIG_PATHS


async def retire(settings,*,apply=False,decision_file=None):
    service=SystemConfigService(settings);state=await service.state()
    if not state:raise ValueError('Current accepted configuration is required')
    original=service.documents.read('kernel',state['desired_ref'])['payload'];candidate=active_configuration(original)
    result={'decision':'C26','previous_ref':state['desired_ref'],'removed_config_paths':list(RETIRED_CONFIG_PATHS),
        'ordinary_values_preserved':active_configuration(original)==candidate,'applied':False,'database_schema_changed':False}
    if apply:
        if not decision_file or not decision_file.is_file():raise ValueError('Concrete approved decision record is required')
        target=service.documents.runtime.ensure('kernel').vault_root/'operations/upgrade-decisions/C26.json'
        raw=decision_file.read_bytes()
        if target.exists() and target.read_bytes()!=raw:raise ValueError('C26 source record changed')
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():target.write_bytes(raw)
        ref=resource_ref(kind='source',identity='C26',uri='vault://kernel/operations/upgrade-decisions/C26.json',
            sha256=hashlib.sha256(raw).hexdigest(),purpose='Actual user instruction in the upgrade chat on 2026-10-11')
        receipt=await service.accept(candidate=candidate,expected_revision=state['revision'],reason='C26: retire scheduled execution config only',
            source_kind='external_file',source_refs=[ref],write_input=True)
        result.update(applied=True,desired_ref=receipt['desired_ref'])
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--decision-file',type=Path)
    parser.add_argument('--receipt',type=Path,required=True);args=parser.parse_args()
    result=await retire(KernelSettings.from_environment(),apply=args.apply,decision_file=args.decision_file)
    args.receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'applied':result['applied'],'ordinary_values_preserved':result['ordinary_values_preserved']}))
if __name__=='__main__':asyncio.run(main())
