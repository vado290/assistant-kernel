"""Build the approved implementation contracts from the immutable design package.

This script only writes package contracts in this checkout. It never migrates DBs,
accepts a document, publishes a template, or changes production configuration.
"""
from __future__ import annotations
import argparse,copy,hashlib,json
from pathlib import Path

BASELINE_SHA='f9ae591846cc24c57687dd9bc35c8fa0f940668a83bcf1e66ff1f0414a4a3b29'
def main():
 parser=argparse.ArgumentParser();parser.add_argument('--design-root',type=Path,required=True);a=parser.parse_args()
 report=a.design_root/'Kernel 基础版本统一设计报告候选稿 v1.2.md'
 if hashlib.sha256(report.read_bytes()).hexdigest()!=BASELINE_SHA:raise ValueError('Design baseline differs')
 source=a.design_root/'contracts';target=Path(__file__).resolve().parents[1]/'src/assistant_kernel/contracts';target.mkdir(exist_ok=True)
 library=json.loads((source/'schema-library.json').read_text('utf-8'));d=library['$defs']
 def ref(n):return {'$ref':'#/$defs/'+n}
 def obj(fields):return {'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}
 text={'type':'string','minLength':1};integer={'type':'integer','minimum':0};nullable=lambda x:{'anyOf':[x,{'type':'null'}]}
 # C01: distinct business and maintenance interaction policy.
 q=d['V12_QuestionGroup'];q['properties']['display_policy']['enum'].append('runtime_both')
 q['allOf']=[]
 for scope,policy in [('pm_business','pm_both'),('supervisor_consultation','supervisor_primary'),('runtime_business','runtime_both'),('runtime_maintenance','runtime_pm_primary'),('system_configuration','kernel_management')]:
  props={'display_policy':{'const':policy}}
  if scope.startswith('runtime_'):props['runtime_owner']=ref('V12_RuntimeInteractionOwner')
  q['allOf'].append({'if':{'properties':{'interaction_scope':{'const':scope}},'required':['interaction_scope']},'then':{'properties':props}})
 # C02/C11: rejected untrusted callbacks do not invent a due/tick; controls are not Runs.
 d['TriggerRejection']=obj({'decision':{'const':'rejected'},'request_receipt_ref':ref('Ref'),'project_id':nullable(text),'schedule_id':nullable(text),'reason_code':text,'evidence_refs':{'type':'array','items':ref('Ref')},'received_at':{'type':'string','format':'date-time'}})
 admission=copy.deepcopy(d['TriggerAdmission'])
 admission['properties'].update(action={'enum':['execute_finite','start_service','maintenance_restart']},control_ref=nullable(ref('Ref')))
 admission['required']+=['action','control_ref'];admission['properties']['decision']['enum']=['accepted','duplicate','skip']
 admission['allOf']=[{'if':{'properties':{'action':{'const':'execute_finite'}}},'then':{'properties':{'control_ref':{'type':'null'}}},'else':{'properties':{'run_ref':{'type':'null'}}}}]
 d['V12_TriggerAdmission']=admission;d['ScheduleTriggerResult']={'oneOf':[ref('V12_TriggerAdmission'),ref('TriggerRejection')]}
 trigger=copy.deepcopy(d['Trigger']);trigger['properties'].update(action=admission['properties']['action'],control_ref=nullable(ref('Ref')))
 trigger['required']+=['action','control_ref'];trigger['allOf']=copy.deepcopy(admission['allOf']);d['V12_Trigger']=trigger
 registration=copy.deepcopy(d['ScheduleRegistration'])
 registration['properties']['control_revision']=integer;registration['required'].append('control_revision')
 for key in ['desired_status','observed_status']:
  registration['properties'][key]['enum']=['registered_disabled' if x=='registered' else x for x in registration['properties'][key]['enum']]
 d['V12_ScheduleRegistration']=registration
 # C03: filesystem projection progress is recoverable from the immutable manifest.
 for key,spec in {'source_change_ids':{'type':'array','items':text,'uniqueItems':True},'previous_generation_id':nullable(text)}.items():
  d['MapManifest']['properties'][key]=spec;d['MapManifest']['required'].append(key)
 # C04: all actual CPU pools declare a start method, including mixed I/O jobs.
 cpu_rule={'if':{'properties':{'cpu_pool_size':{'minimum':1}},'required':['cpu_pool_size']},'then':{'properties':{'start_method':{'enum':['spawn','fork','forkserver']}}},'else':{'properties':{'start_method':{'const':'not_applicable'}}}}
 for n in ['V12_PythonResources','V12_ServiceResources']:
  d[n]['properties']['start_method']={'enum':['not_applicable','spawn','fork','forkserver']}
  if 'start_method' not in d[n]['required']:d[n]['required'].append('start_method')
  d[n].setdefault('allOf',[]).append(copy.deepcopy(cpu_rule))
 # C05/C06: minimal instance-owned admission facts, not a second business state machine.
 d['ServiceWorkItem']=obj({'logical_execution_id':text,'definition_ref':ref('DocRef'),'state':{'enum':['active','waiting_input','settling']},'checkpoint_ref':nullable(ref('PinnedRef')),'prior_effect_refs':{'type':'array','items':ref('Ref')},'admitted_at':{'type':'string','format':'date-time'},'reservation_ids':{'type':'array','items':text,'uniqueItems':True}})
 d['ServiceWorkControl']=obj({'gate':{'enum':['open','maintenance','closed']},'gate_revision':integer,'control_ref':nullable(ref('Ref')),'items':{'type':'object','additionalProperties':ref('ServiceWorkItem')}})
 d['V12_ServiceInstance']['properties']['work_control']=ref('ServiceWorkControl');d['V12_ServiceInstance']['required'].append('work_control')
 # C07/C09: server-owned registry selects strict writers; payload_definition is not model choice.
 interfaces=json.loads((source/'interfaces.json').read_text('utf-8'))
 selectors=interfaces['active_write_selectors']
 replacements={'V12_Contract':'V12_Active_Contract','V12_Design':'V12_Active_Design','V12_Implementation':'V12_Active_Implementation','Review':'Active_Review','Operation':'Active_Operation','ActionPlan':'Active_ActionPlan','V12_ContextManifest':'V12_Active_ContextManifest','Trigger':'V12_Trigger','ScheduleRegistration':'V12_ScheduleRegistration'}
 documents={}
 for branch in d['V12_Document']['allOf'][1]['oneOf']:
  prop=branch['properties'];business_schema=prop['schema_version']['const'];old=prop['payload']['$ref'].rsplit('/',1)[1]
  name=replacements.get(old,old);prop['payload']=ref(name);prop['payload_definition']={'const':name}
  branch['required'].append('payload_definition');documents[business_schema]=name
 # C10: spawn failure has no PID; do not fabricate identity.
 started=copy.deepcopy(d['ProcessReceipt']);started['properties']['launch_state']={'const':'started'};started['required'].append('launch_state')
 failed=copy.deepcopy(started);failed['properties']['launch_state']={'const':'launch_failed'}
 for key in ['pid','process_group_id','start_ticks']:failed['properties'][key]={'type':'null'}
 failed['properties']['status']={'const':'failed'}
 unknown=copy.deepcopy(started);unknown['properties']['launch_state']={'const':'identity_unknown'}
 for key in ['pid','process_group_id','start_ticks']:unknown['properties'][key]=nullable(started['properties'][key])
 unknown['properties']['status']={'const':'unknown'}
 d['V12_ProcessReceipt']={'oneOf':[started,failed,unknown]}
 # C12: bounded ports and exact catalog coverage, including identity/provider additions.
 def bounded_ports(node,path=''):
  if not isinstance(node,dict):return
  for key,value in node.get('properties',{}).items():
   here=path+'/'+key
   if key in ['port','listen_port'] or '/host_ports/' in here:value.update(minimum=1,maximum=65535)
   bounded_ports(value,here)
  if isinstance(node.get('additionalProperties'),dict):bounded_ports(node['additionalProperties'],path+'/<provider_id>')
 bounded_ports(d['SystemConfig'])
 # D08/D09: preserve the existing chat catalog and explicit unconfigured values.
 provider=d['SystemConfig']['properties']['providers']['additionalProperties']['properties']
 for key,spec in provider['model_names']['properties'].items():
  provider['model_names']['properties'][key]=nullable(spec)
 chat_properties={key:copy.deepcopy(text) for key in ['display_name','api_style','runtime','provider_npm','verified_at']}
 for key in ['model_id','endpoint_id']:chat_properties[key]=nullable(text)
 chat_properties['base_url']=nullable({'type':'string','format':'uri'})
 chat_properties['api_key_env']={'type':'string','pattern':'^[A-Za-z_][A-Za-z0-9_]*$'}
 for key in ['context_window_tokens','max_output_tokens']:chat_properties[key]={'type':'integer','minimum':1}
 for key in ['input_price_per_million_tokens_usd','output_price_per_million_tokens_usd','cached_input_price_per_million_tokens_usd']:
  chat_properties[key]=nullable({'type':'number','minimum':0})
 for key in ['strengths','weaknesses']:chat_properties[key]={'type':'array','items':{'type':'string'}}
 chat_properties['enabled']={'type':'boolean'}
 chat_properties['capabilities']={'type':'object','additionalProperties':False,'properties':{key:{'type':'boolean'} for key in ['reasoning','function_calling','structured_output','opencode_local_mcp','codex_builtin_shell','codex_custom_mcp']}}
 chat_properties['pricing_verification']=obj({'status':{'enum':['unverified','verified']},'source_refs':{'type':'array','items':ref('Ref')},'verified_at':nullable({'type':'string','format':'date-time'})})
 chat_properties['pricing_verification']['allOf']=[{'if':{'properties':{'status':{'const':'verified'}}},'then':{'properties':{'source_refs':{'minItems':1},'verified_at':{'type':'string','format':'date-time'}}}}]
 provider['chat_models']={'type':'object','additionalProperties':{'type':'object','properties':chat_properties,'additionalProperties':False},'x-apply-mode':'next_invocation','x-inheritable':False}
 # Protocol revision remains 1.2; a fixed library digest distinguishes this approved patch set.
 library['$id']='urn:assistant-kernel:unified-design:1.2:implementation:1'
 registry={'protocol_revision':'1.2','implementation_revision':1,'document_selectors':documents,'write_selectors':selectors,'public_interfaces':interfaces['public_interfaces'],'internal_interfaces':{'accept_schedule_trigger':'ScheduleTriggerResult','reserve_runtime_capacity':'RuntimeCapacityReceipt','settle_runtime_capacity':'RuntimeCapacityReceipt','commit_session_boundary':'SessionBoundaryReceipt','assemble_business_runtime':'V12_RuntimeAssemblyReceipt','apply_runtime_control':'V12_RuntimeControlReceipt','reconcile_service_instance':'V12_ServiceInstance','prepare_pm_prompt':'PromptPreparation','record_prompt_confirmation':'ConfirmationReceipt','dispatch_pm_turn':'TurnAdmission','settle_pm_turn':'TurnSettlement'}}
 roots=set(selectors.values())|set(documents.values())|{'ScheduleTriggerResult','V12_ProcessReceipt','ServiceWorkControl'}
 roots.update(registry['internal_interfaces'].values())
 # Public argument/return DTOs are active dependencies too; they must not be
 # mislabeled compatibility-only just because no document embeds them.
 for interface in interfaces['public_interfaces'].values():roots.update(interface.values())
 pending=list(roots);active=set()
 import re
 while pending:
  name=pending.pop()
  if name in active:continue
  active.add(name);pending.extend(set(re.findall(r'#/\$defs/([^"/]+)',json.dumps(d[name])))-active)
 library['x-contract-policy']={'active_revision':'1.2','implementation_revision':1,'baseline_sha256':BASELINE_SHA,'active_roots':sorted(roots),'active_dependencies':sorted(active-roots),'compatibility_only':sorted(set(d)-active),'approved_corrections':['C01','C02','C03','C04','C05','C06','C07','C08','C09','C10','C11','C12','C13','C14','D06','D08','D09']}
 catalog=json.loads((source/'config-field-catalog.json').read_text('utf-8'));existing={r['path'] for r in catalog}
 def leaf_specs(node,path=''):
  if node.get('properties'):
   for key,spec in node['properties'].items():yield from leaf_specs(spec,path+'/'+key)
  elif isinstance(node.get('additionalProperties'),dict):yield from leaf_specs(node['additionalProperties'],path+('/<model_id>' if path.endswith('/chat_models') else '/<provider_id>'))
  else:yield path,node
 for path,spec in leaf_specs(d['SystemConfig']):
  if path not in existing:
   catalog.append({'path':path,'schema':spec,'proposed_value':None,'value_source':'import actual legacy non-secret identity/provider fields; never infer from missing template values','apply_mode':'next_invocation' if path.startswith('/providers/') else 'pending_restart','consumer':'model_provider' if path.startswith('/providers/') else 'channel','null_meaning':'as declared by Schema; missing is not configured','inheritance':'explicit_import','visibility':'kernel_management','secret_value_allowed':False})
 for name,data in [('schema-library-v12.json',library),('registry-v12.json',registry),('kernel-system.example.json',json.loads((source/'kernel-system.example.json').read_text('utf-8'))),('config-field-catalog.json',catalog)]:
  (target/name).write_text(json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2)+'\n',encoding='utf-8',newline='\n')
 manifest={'design_report_sha256':BASELINE_SHA,'original_library_sha256':hashlib.sha256((source/'schema-library.json').read_bytes()).hexdigest(),'implementation_revision':1,'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in target.glob('*.json') if p.name!='manifest.json'}}
 (target/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8',newline='\n')
 print(json.dumps({'definitions':len(d),'document_selectors':len(documents),'manifest':str(target/'manifest.json')}))
if __name__=='__main__':main()
