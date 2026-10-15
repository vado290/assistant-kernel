"""Build the reviewed 0.31 scheduler contract/template candidate; no deployment."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/'src/assistant_kernel'

def read(p):return json.loads(p.read_text(encoding='utf-8'))
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8',newline='\n')
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def ref(name):return {'$ref':'#/$defs/'+name}
def obj(properties,required=None):return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties) if required is None else required}
def nullable(value):return {'anyOf':[value,{'type':'null'}]}
TEXT={'type':'string','minLength':1};TIME={'type':'string','format':'date-time'}

def main():
    folder=ROOT/'contracts';library=read(folder/'schema-library-v12.json');registry=read(folder/'registry-v12.json');defs=library['$defs']
    refs={'type':'array','items':ref('Ref')};auth={**refs,'minItems':1}
    rule={'oneOf':[obj({'kind':{'const':'once'},'at':TIME}),obj({'kind':{'const':'interval'},'anchor':TIME,'seconds':{'type':'integer','minimum':1}}),obj({'kind':{'const':'cron'},'expression':TEXT})]}
    defs['KernelScheduleDefinition']=obj({'project_id':TEXT,'schedule_id':{'type':'string','pattern':'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$'},
        'name':TEXT,'runtime_unit_ref':ref('DocRef'),'action':{'enum':['run','start','stop','restart']},'inputs':{'type':'object'},
        'rule':rule,'timezone':TEXT,'misfire_policy':{'const':'skip'},'overlap_policy':{'const':'skip'},
        'notification_policy':{'const':'every_occurrence'},'source_refs':auth})
    defs['ScheduleRegistration']=obj({'project_id':TEXT,'schedule_id':TEXT,'definition_ref':ref('DocRef'),'unit_id':TEXT,
        'state':{'enum':['disabled','enabled','paused','exhausted','retired']},'control_revision':{'type':'integer','minimum':0},
        'next_fire_at':nullable(TIME),'authorization_refs':refs,'last_tick_id':nullable(TEXT),'last_control_receipt_ref':nullable(ref('DocRef'))})
    defs['ScheduleControlRequest']=obj({'schedule_id':TEXT,'action':{'enum':['update','enable','pause','resume','retire','run_now','cancel_current']},
        'expected_control_revision':{'type':'integer','minimum':0},'idempotency_key':TEXT,'authorization_source_refs':auth,
        'target_definition_ref':nullable(ref('DocRef'))})
    defs['ScheduleOperationActions']={'type':'array','minItems':1,'items':obj({
        'schedule_id':TEXT,'definition_ref':ref('DocRef'),
        'action':defs['ScheduleControlRequest']['properties']['action'],
        'expected_control_revision':{'type':'integer','minimum':0},
        'changes':obj({'rule':rule,'timezone':TEXT,'inputs':{'type':'object'}},required=[])})}
    defs['PromptDraft']['properties']['schedule_actions']=ref('ScheduleOperationActions')
    defs['ScheduleControlReceipt']=obj({'project_id':TEXT,'schedule_id':TEXT,'action':TEXT,'control_revision':{'type':'integer'},
        'state':TEXT,'definition_ref':ref('DocRef'),'next_fire_at':nullable(TIME),'authorization_source_refs':auth,
        'tick_ref':nullable(ref('Ref')),'recorded_at':TIME})
    defs['ScheduleTick']=obj({'tick_id':TEXT,'project_id':TEXT,'schedule_id':TEXT,'origin':{'enum':['scheduled','manual']},
        'scheduled_for_utc':TIME,'definition_ref':ref('DocRef'),'control_revision':{'type':'integer'},
        'disposition':{'enum':['submitted','skipped']},'reason':nullable(TEXT),'run_ref':nullable(ref('Ref')),
        'control_receipt_ref':nullable(ref('DocRef')),'settlement_status':{'enum':['pending','succeeded','failed','cancelled','skipped']},
        'result_ref':nullable(ref('Ref')),'notification_id':nullable(TEXT),'admitted_at':TIME,'settled_at':nullable(TIME)})
    defs['SchedulePreview']=obj({'timezone':TEXT,'rule':rule,'as_of':TIME,'times':{'type':'array','items':obj({'utc':TIME,'local':TIME})},
        'description':TEXT,'misfire_policy':{'const':'skip'},'overlap_policy':{'const':'skip'}})
    for schema,name in [('kernel-schedule-definition.v1','KernelScheduleDefinition'),('schedule-control-receipt.v1','ScheduleControlReceipt'),('schedule-tick.v1','ScheduleTick')]:
        registry['document_selectors'][schema]=name
        cases=defs['V12_Document']['allOf'][1]['oneOf']
        case={'properties':{'schema_version':{'const':schema},'payload_definition':{'const':name},'payload':ref(name)},
              'required':['schema_version','payload','payload_definition']}
        if case not in cases:cases.append(case)
    plan=obj({'schedule_id':TEXT,'runtime_unit_id':TEXT,'action':{'enum':['run','start','stop','restart']},'inputs':{'type':'object'},
        'rule':rule,'timezone':TEXT,'acceptance_ids':{'type':'array','minItems':1,'items':TEXT},'requires_first_run':{'type':'boolean'}})
    defs['ScheduleRequirement']=plan
    defs['V12_Contract']['properties']['schedule_requirements']={'type':'array','items':ref('ScheduleRequirement')}
    defs['V12_Design']['properties']['schedule_plans']={'type':'array','items':ref('ScheduleRequirement')}
    defs['V12_Implementation']['properties']['schedule_handoffs']={'type':'array','items':ref('DocRef')}
    defs['V12_RuntimeDesign']['properties']['trigger_mode']={'enum':['on_demand','once','recurring']}
    defs['V12_RuntimeDesign']['properties']['execution_mode']={'enum':['on_demand','scheduled','service']}
    defs['V12_RuntimeRequirement']['properties']['execution_mode']={'enum':['on_demand','scheduled','service']}
    defs['V12_Design']['properties']['report_schedule_ref']['description']='Legacy external report scheduler field; use schedule_plans with a registered report RuntimeUnit.'
    defs['V12_PMPayload']['properties']['schedule_handoff']['description']='Retired legacy backend handoff; use Implementation.schedule_handoffs and confirmed Operate schedule tools.'
    defs['Args_preview_schedule']=obj({'rule':rule,'timezone':TEXT})
    defs['Args_register_schedule']=obj({'definition_ref':ref('DocRef'),'expected_control_revision':{'const':0},'idempotency_key':TEXT,'authorization_source_refs':auth})
    paging={'limit':{'type':'integer','minimum':1,'maximum':100},'cursor':nullable(TEXT)}
    defs['Args_list_schedules']=obj(paging)
    defs['Args_schedule_status']=obj({'schedule_id':TEXT})
    defs['Args_control_schedule']=ref('ScheduleControlRequest')
    defs['Args_list_schedule_ticks']=obj({'schedule_id':TEXT,**paging})
    for name,response in [('preview_schedule','SchedulePreview'),('register_schedule','ScheduleRegistration'),('list_schedules',None),('schedule_status',None),('control_schedule','ScheduleControlReceipt'),('list_schedule_ticks',None)]:
        if response is None:
            response='Returns_'+name;defs[response]={'type':'object'}
        registry['public_interfaces'][name]={'request':'Args_'+name,'response':response}
        library['x-contract-policy']['active_roots']=sorted(set(library['x-contract-policy']['active_roots'])|{'Args_'+name,response})
    writer=defs['Args_write_runtime_document'];writer['properties']['document_kind']['enum']=sorted(set(writer['properties']['document_kind']['enum'])|{'kernel-schedule-definition'})
    case={'properties':{'document_kind':{'const':'kernel-schedule-definition'},'payload':ref('KernelScheduleDefinition')}}
    if case not in writer['oneOf']:writer['oneOf'].append(case)
    control=defs['V12_RuntimeControlRequest']['properties']
    control['cause']['enum']=sorted(set(control['cause']['enum'])|{'scheduled'})
    control['tick_ref']=nullable(ref('Ref'))
    scheduler=obj({'enabled':{'type':'boolean','default':False},'poll_seconds':{'type':'number','minimum':0.1,'maximum':60,'default':1},
        'late_grace_seconds':{'type':'number','minimum':0,'maximum':300,'default':5},'batch_size':{'type':'integer','minimum':1,'maximum':1000,'default':100}})
    for key,field in scheduler['properties'].items():field.update({'description':'Kernel内置定时器 '+key,'x-apply-mode':'dynamic_admission','x-inheritable':False})
    kernel=defs['SystemConfig']['properties']['kernel'];kernel['properties']['scheduler']=scheduler
    # Existing immutable configs remain readable; migration writes explicit defaults.
    example=read(folder/'kernel-system.example.json');example['kernel']['scheduler']={k:v['default'] for k,v in scheduler['properties'].items()};write(folder/'kernel-system.example.json',example)
    catalog=read(folder/'config-field-catalog.json')
    for key,field in scheduler['properties'].items():
        path='/kernel/scheduler/'+key
        catalog=[x for x in catalog if x.get('path')!=path]
        catalog.append({'path':path,'type':field['type'],'proposed_value':field['default'],'schema':field,
            'apply_mode':'dynamic_admission','inheritance':'global_only','description':field['description'],'consumer':'kernel-scheduler',
            'failure':'拒绝非法参数，保留上次有效配置；实际消费后写回执','null_meaning':'不允许null','secret_value_allowed':False,
            'source_plan_pointer':path,'unit':'seconds' if key.endswith('seconds') else 'count/value',
            'value_source':'用户批准的 Kernel 0.31 定时实现方案','visibility':'related_operational_read','write_authority':'kernel_management'})
    write(folder/'config-field-catalog.json',catalog)
    names={'KernelScheduleDefinition','ScheduleRegistration','ScheduleTick','ScheduleControlRequest','ScheduleControlReceipt','SchedulePreview','ScheduleRequirement','ScheduleOperationActions'}
    library['x-contract-policy']['active_dependencies']=sorted(set(library['x-contract-policy']['active_dependencies'])|names)
    policy=library['x-contract-policy']
    policy['compatibility_only']=sorted(set(policy['compatibility_only'])-set(policy['active_roots'])-set(policy['active_dependencies']))
    correction='0.31 (2026-10-13): user-approved Kernel scheduling supersedes C26 for adopted tasks; preserve old readers and explicit business authorization.'
    if correction not in policy['approved_corrections']:policy['approved_corrections'].append(correction)
    registry['implementation_revision']=10;library['x-contract-policy']['implementation_revision']=10
    write(folder/'schema-library-v12.json',library);write(folder/'registry-v12.json',registry)
    manifest=read(folder/'manifest.json');manifest['implementation_revision']=10;manifest['files']={p.name:digest(p) for p in folder.glob('*.json') if p.name!='manifest.json'};write(folder/'manifest.json',manifest)
    templates=ROOT/'prompts/v12';(templates/'schemas/schema-library.json').write_bytes((folder/'schema-library-v12.json').read_bytes())
    profiles=read(templates/'profiles.json');relative='skills/assistant-schedule-management/SKILL.md'
    if (templates/relative).exists():
        sha=digest(templates/relative)
        for profile in profiles['profiles'].values():
            profile['conditional_skill_rules']=[x for x in profile['conditional_skill_rules'] if x['condition']!='schedule_work']+[{
                'condition':'schedule_work','skill_name':'assistant-schedule-management','must_record_actual_load':True,
                'published_ref':{'schema_version':'resource-ref.v1','kind':'source','id':'v12-template/'+relative,'document_schema':None,
                    'revision_id':None,'sha256':sha,'uri':'kernel://template-assets/'+sha+'/'+relative,'purpose':'fixed published template asset; not business approval'}}]
    def refresh(value):
        if isinstance(value,dict):
            if str(value.get('id','')).startswith('v12-template/'):
                rel=value['id'][13:];path=templates/rel
                if path.is_file():value.update(sha256=digest(path),uri='kernel://template-assets/'+digest(path)+'/'+rel)
            for x in value.values():refresh(x)
        elif isinstance(value,list):
            for x in value:refresh(x)
    refresh(profiles);write(templates/'profiles.json',profiles)
    manifest=read(templates/'manifest.json');manifest['contract_library_sha256']=digest(folder/'schema-library-v12.json')
    manifest['files']={p.relative_to(templates).as_posix():digest(p) for p in templates.rglob('*') if p.is_file() and p.name!='manifest.json'}
    for source in manifest['design_sources']:source['sha256']=manifest['files'][source['path']]
    manifest['bundle_sha256']=hashlib.sha256(json.dumps(manifest['files'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest();write(templates/'manifest.json',manifest)
    print(json.dumps({'contract_revision':10,'template_sha256':manifest['bundle_sha256']}))

if __name__=='__main__':main()
