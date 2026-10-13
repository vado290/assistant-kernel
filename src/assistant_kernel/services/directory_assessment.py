"""Bounded metadata measurement for external directory proposals.

This never deletes files, reduces directory authority, or grants business access.
Skipped/inaccessible entries make coverage a lower bound rather than an exact sum.
"""
from datetime import datetime,timezone
import os
from pathlib import Path
import time
import uuid

from assistant_kernel.services.contracts import contracts


def default_parameters():
    return {'scan_limits':{'max_entries':10000,'max_elapsed_ms':5000,'follow_symlinks':False},
            'thresholds':{'file_count':10000,'total_bytes':1073741824,'elapsed_ms':3000,
                          'configured':False,'source_ref':None}}


async def configured_parameters(settings):
    from assistant_kernel.services.system_config import SystemConfigService
    service=SystemConfigService(settings);state=await service.state()
    if not state:return default_parameters()
    values=service._config(state['desired_ref'])['map']
    return {'scan_limits':{'max_entries':values['scan']['max_files'],
                'max_elapsed_ms':values['scan']['max_duration_ms'],'follow_symlinks':False},
            'thresholds':{**values['external_directory_warning'],'configured':True,
                          'source_ref':state['desired_ref']}}


async def record_configuration_use(settings,assessments):
    if not assessments:return None
    from assistant_kernel.services.system_config import SystemConfigService
    from assistant_kernel.services.document_runtime import resource_ref
    from assistant_kernel.services.contracts import canonical_json
    import hashlib
    source=assessments[0]['thresholds'].get('source_ref')
    if source is None:return None
    if any(a['thresholds'].get('source_ref')!=source for a in assessments):
        raise ValueError('One directory operation has inconsistent configuration sources')
    service=SystemConfigService(settings);state=await service.state()
    if not state or state['desired_ref']!=source:return None
    receipt={'schema_version':'config-consumer-receipt.v1','consumer':'directory_assessor',
        'source_ref':source,'assessment_ids':[a['assessment_id'] for a in assessments],
        'scan_limits':assessments[0]['scan_limits'],'thresholds':assessments[0]['thresholds'],
        'observed_at':assessments[0]['observed_at']}
    encoded=canonical_json(receipt);digest=hashlib.sha256(encoded).hexdigest()
    path=settings.data_root/'runtime/config-consumers/directory-assessor'/(digest+'.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.read_bytes()!=encoded:raise ValueError('Immutable consumer receipt differs')
    path.write_bytes(encoded)
    ref=resource_ref(kind='receipt',identity='directory-config-'+digest,uri=str(path),sha256=digest,
                     purpose='actual bounded scan and warning parameters used')
    for group in ['map/scan:dynamic_admission','map/external_directory_warning:dynamic_admission']:
        old=next((g for g in state['groups'] if g['group']==group),None)
        if old is None:raise ValueError('Configuration consumer groups need the approved upgrade migration')
        if old['status']=='applied' and old['effective_ref']==source:continue
        await service.settle_group(expected_revision=state['revision'],group=group,evidence_ref=ref,
            applied=True,reason='Directory metadata assessment actually used this parameter group')
    return ref


def assess_bindings(project_id,bindings,*,context,source_refs,parameters=None):
    parameters=parameters or default_parameters()
    result=[]
    for binding in bindings:
        item=assess_directory(project_id=project_id,root_key=binding['root_key'],
            path=binding['resolved_path'],context=context,source_refs=source_refs,**parameters)
        item['path']=binding.get('display_path') or binding['resolved_path']
        result.append(item)
    return result


def assess_directory(*,project_id,root_key,path,context,scan_limits,thresholds,source_refs):
    registry=contracts()
    registry.validate('DirectoryScanLimits',scan_limits)
    registry.validate('DirectoryThresholds',thresholds)
    target=Path(path).resolve()
    if not target.is_dir():raise FileNotFoundError('Proposed external directory is unavailable')
    started=time.monotonic();deadline=started+scan_limits['max_elapsed_ms']/1000
    files=0;total=0;entries=0;complete=True;stack=[target];reasons=[]
    while stack:
        if time.monotonic()>=deadline or entries>=scan_limits['max_entries']:
            complete=False;reasons.append('scan_budget_reached');break
        current=stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    if time.monotonic()>=deadline or entries>=scan_limits['max_entries']:
                        complete=False;reasons.append('scan_budget_reached');stack.clear();break
                    entries+=1
                    try:
                        if entry.is_symlink():
                            complete=False;continue
                        if entry.is_dir(follow_symlinks=False):stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files+=1;total+=entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        complete=False;reasons.append('some_metadata_unavailable')
        except OSError:
            complete=False;reasons.append('some_metadata_unavailable')
    elapsed=max(0,int((time.monotonic()-started)*1000))
    if not complete:reasons.append('coverage_incomplete')
    for key,actual in [('file_count',files),('total_bytes',total),('elapsed_ms',elapsed)]:
        threshold=thresholds.get(key)
        if threshold is not None and actual>=threshold:reasons.append(key+'_threshold_reached')
    reasons=list(dict.fromkeys(reasons))
    result={'assessment_id':'directory-'+uuid.uuid4().hex,'project_id':project_id,'root_key':root_key,
        'path':str(path),'resolved_path':str(target),'context':context,
        'observed_at':datetime.now(timezone.utc).isoformat(),'observed_file_count':files,
        'estimated_total_bytes':total,'elapsed_ms':elapsed,'scan_complete':complete,
        'scan_limits':scan_limits,'thresholds':thresholds,'warning_reasons':reasons,'source_refs':source_refs,
        'measurement_quality':{'file_count':'exact' if complete else 'lower_bound',
                               'total_bytes':'exact' if complete else 'lower_bound'},
        'disposition':'pending' if reasons else 'not_required','disposition_source_ref':None}
    registry.validate('ExternalDirectoryAssessment',result)
    return result


def directory_disclosure(assessments):
    lines=['目录及全部子目录长期共享给本 Project 各角色，可读、写、新增、删除和执行；业务批准仍分别核验。']
    if not assessments:lines.append('本提案不添加外部目录。')
    for item in assessments:
        qualifier='精确计数' if item['scan_complete'] else '扫描未完成，以下仅为已观察下界'
        lines.append(f"{item['path']}：{item['observed_file_count']} 个文件，{item['estimated_total_bytes']} 字节，{item['elapsed_ms']} ms；{qualifier}。")
        if item['warning_reasons']:
            lines.append('目录体量或扫描覆盖需要留意。可以接受现状，或提出清理/调整范围；系统不会自动删除文件或缩小授权。')
    return '\n'.join(lines)
