"""Small synchronous client for fixed Project programs. No admin credentials."""
import json,os
from pathlib import Path
from contextlib import contextmanager


class RuntimeClient:
    def __init__(self,path=None):
        self.private=json.loads(Path(path or os.environ['ASSISTANT_RUNTIME_CLIENT_PATH']).read_text())
        self.context=self.private['context'];self.logical_execution_id=self.context['logical_execution_id']

    def call(self,operation,payload):
        import httpx
        with httpx.Client(timeout=5 if operation in {'ready','heartbeat'} else None) as client:
            service=self.private.get('service_instance_id')
            if service:
                payload={**payload,'logical_execution_id':self.logical_execution_id}
                if operation in {'reserve-cpu','release-cpu'}:payload['expected_instance_generation']=self.private['generation']
            response=client.post(self.private['gateway_url']+('/v1/service-runtime/' if service else '/v1/runtime/')+self.private['run_id']+'/'+operation,
                headers={'Authorization':'Bearer '+self.private['runtime_token']},json=payload)
            response.raise_for_status();return response.json()

    def model(self,agent_id,value,*,key):
        return self.call('model',{'agent_id':agent_id,'input':value,'idempotency_key':key})

    def tool(self,agent_id,server_name,tool,arguments,*,key):
        return self.call('tool',{'agent_id':agent_id,'server_name':server_name,'tool':tool,'arguments':arguments,'idempotency_key':key})

    @contextmanager
    def cpu(self,key,slots=1):
        request={'owner_ref':self.context['execution_ref'],'logical_execution_id':self.logical_execution_id,
            'resource_kind':'cpu_work_item','cpu_slots':slots,'model_invocations':0,'expected_instance_generation':None,'idempotency_key':key}
        import time
        receipt=self.call('reserve-cpu',request)
        while receipt['status']=='queued':
            time.sleep(.05);receipt=self.call('reserve-cpu',request)
        if receipt['status']!='reserved':raise RuntimeError('CPU step is not newly admitted: '+receipt['status'])
        try:yield receipt
        finally:self.call('release-cpu',request)

    def effect(self,key,description,callback):
        prior=self.call('effect-begin',{'key':key,'description':description})
        if prior['duplicate']:return prior['result']
        try:
            result=callback()
        except BaseException:
            self.call('effect-finish',{'key':key,'status':'unknown','result':None});raise
        self.call('effect-finish',{'key':key,'status':'succeeded','result':result})
        return result


    def python_skill(self,agent_id,skill_id,value):
        """Execute the pinned bundle's declared Python Skill, rather than inventing one."""
        import importlib.util
        entries=self.private['python_skills'];entry=entries.get(agent_id+':'+skill_id)
        if not entry:raise PermissionError('Python Skill is not in this fixed Runtime assembly')
        spec=importlib.util.spec_from_file_location('runtime_skill_'+skill_id.replace('-','_'),entry)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.run(value,self)

    def run_graph(self,build,input_value):
        """Use the actual SQLite checkpointer; business nodes own business state."""
        import hashlib,sqlite3,uuid
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.types import Command
        workflow=self.private['workflow']
        if workflow['framework']!='langgraph':raise PermissionError('This fixed Unit has no persistent business graph')
        root=Path(self.private['business_state_root']);root.mkdir(parents=True,exist_ok=True)
        backend=root/'checkpoints.sqlite'
        if self.private.get('resume_checkpoint_path'):
            source=Path(self.private['resume_checkpoint_path'])
            if hashlib.sha256(source.read_bytes()).hexdigest()!=self.private['resume_checkpoint_sha256']:raise ValueError('Resume checkpoint changed')
            with sqlite3.connect(str(source)) as previous,sqlite3.connect(str(backend)) as current:previous.backup(current)
        with SqliteSaver.from_conn_string(str(backend)) as saver:
            config={'configurable':{'thread_id':self.logical_execution_id},'max_concurrency':workflow['max_parallel_nodes']}
            graph=build(saver)
            expected=set(self.private['graph_definition']['nodes'])
            actual=set(graph.get_graph().nodes)-{'__start__','__end__'}
            if actual!=expected:raise ValueError('Actual graph nodes differ from the fixed graph definition')
            answers=self.private.get('resume_answers')
            if answers is None and saver.get_tuple(config):raise PermissionError('Existing logical checkpoint cannot be replayed from the first node')
            value=Command(resume={x:answers for x in self.private['interrupt_ids']}) if answers is not None else input_value
            result=graph.invoke(value,config)
            interruptions=result.get('__interrupt__',())
            if not interruptions:return result
            questions=[]
            for item in interruptions:questions.extend(item.value['questions'])
            snapshots=root/'snapshots';snapshots.mkdir(exist_ok=True)
            path=snapshots/(self.private['run_id']+'-'+uuid.uuid4().hex+'.sqlite')
            with sqlite3.connect(str(path)) as snapshot:saver.conn.backup(snapshot)
        project=self.context['project_id'];vault=Path(self.private['project_vault'])
        ref={'schema_version':'resource-ref.v1','kind':'source','id':path.stem,'document_schema':None,'revision_id':None,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'uri':'vault://'+project+'/'+path.relative_to(vault).as_posix(),
            'purpose':'actual interrupted business graph SQLite checkpoint'}
        pending=self.call('needs-input',{'business_thread_id':self.logical_execution_id,'checkpoint_ref':ref,
            'questions':questions,'interrupt_ids':[x.id for x in interruptions]})
        result={'status':'needs_input','question_group_id':pending['group_id'],'checkpoint_ref':ref}
        Path(os.environ['ASSISTANT_RESULT_PATH']).write_text(json.dumps(result))
        return result


class ServiceClient(RuntimeClient):
    """Service lifetime stays independent of finite Workers; every work item is registered."""
    def ready(self):
        result=self.call('ready',{})
        import time
        deadline=time.monotonic()+self.private['work_limits']['startup_timeout_seconds']
        while not result['ready']:
            if time.monotonic()>=deadline:raise TimeoutError('Kernel has not verified this service readiness')
            time.sleep(.1);result=self.call('ready',{})
        if not getattr(self,'_heartbeat_started',False):
            import threading
            self._heartbeat_started=True;self._heartbeat_stop=threading.Event()
            def pulse():
                while not self._heartbeat_stop.wait(self.private['service_health']['heartbeat_interval_seconds']):
                    try:self.heartbeat()
                    except Exception:pass # Kernel probes the actual process; never pretend the heartbeat succeeded.
            self._heartbeat_thread=threading.Thread(target=pulse,daemon=True);self._heartbeat_thread.start()
        return result
    def heartbeat(self):return self.call('heartbeat',{})

    def close(self):
        if getattr(self,'_heartbeat_started',False):self._heartbeat_stop.set();self._heartbeat_thread.join(timeout=6)

    def run_work(self,key,value,callback):
        from copy import deepcopy
        import hashlib
        work=ServiceClient.__new__(ServiceClient);work.private=deepcopy(self.private);work.context=work.private['context']
        work.logical_execution_id=key;work.context['logical_execution_id']=key
        admission=work.call('work-begin',{'input':value})
        if admission['duplicate']:return admission['result']
        work.logical_execution_id=admission['logical_execution_id'];work.context['logical_execution_id']=work.logical_execution_id
        root=Path(self.private['context']['output_root_uri'])/'work'/hashlib.sha256(work.logical_execution_id.encode()).hexdigest()
        root.mkdir(parents=True,exist_ok=True);work.output_root=root
        result=callback(value,work);work.call('work-end',{'result':result});return result

    def ask(self,questions,checkpoint_ref=None):
        self.call('work-wait',{'questions':questions,'checkpoint_ref':checkpoint_ref})
        import time
        while True:
            answer=self.call('work-answers',{})
            if answer['status']=='answered':return answer['answers']
            self.heartbeat();time.sleep(1)

    def run_graph(self,build,input_value):
        import hashlib,sqlite3,uuid
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.types import Command
        if self.private['workflow']['framework']!='langgraph':raise PermissionError('No fixed business graph')
        root=self.output_root/'graph';root.mkdir(exist_ok=True)
        with SqliteSaver.from_conn_string(str(root/'checkpoints.sqlite')) as saver:
            config={'configurable':{'thread_id':self.logical_execution_id},'max_concurrency':self.private['workflow']['max_parallel_nodes'],
                'recursion_limit':self.private['work_limits']['max_steps_per_work_item']}
            graph=build(saver)
            if set(graph.get_graph().nodes)-{'__start__','__end__'}!=set(self.private['graph_definition']['nodes']):raise ValueError('Graph differs from its fixed definition')
            if saver.get_tuple(config):raise PermissionError('Existing graph requires explicit recovery, not replay')
            value=input_value
            while True:
                result=graph.invoke(value,config);interrupts=result.get('__interrupt__',())
                if not interrupts:return result
                path=root/(uuid.uuid4().hex+'.sqlite')
                with sqlite3.connect(str(path)) as copy:saver.conn.backup(copy)
                ref={'schema_version':'resource-ref.v1','kind':'source','id':path.stem,'document_schema':None,'revision_id':None,
                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'uri':'vault://'+self.context['project_id']+'/'+path.relative_to(Path(self.private['project_vault'])).as_posix(),
                    'purpose':'actual interrupted service business graph'}
                answers=self.ask([q for item in interrupts for q in item.value['questions']],ref)
                value=Command(resume={item.id:answers for item in interrupts})
