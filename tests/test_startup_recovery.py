"""Focused startup, cancellation and real isolated-database reconnect checks."""
import asyncio
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import psycopg
import pytest

from assistant_kernel.services import config_consumers as consumers
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.config import KernelSettings
from assistant_kernel.services.runtime_admission import RuntimeCapacityUnavailable
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.worker import main as worker
from test_core4 import core_settings, prepare_agent


def closed_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def test_startup_retries_availability_and_restores_signal_handlers(monkeypatch):
    attempts=[];delays=[];before=signal.getsignal(signal.SIGTERM)
    def connect(settings):
        attempts.append(1)
        if len(attempts)<=10:raise psycopg.OperationalError('connection refused')
        return None,None
    monkeypatch.setattr(consumers,'accepted_configuration',connect)
    monkeypatch.setattr(consumers.time,'sleep',delays.append)
    settings=SimpleNamespace()
    assert consumers.startup_settings(settings,'test') is settings
    assert delays==[1,2,4,8,10,10,10,10,10,10]
    assert signal.getsignal(signal.SIGTERM)==before


@pytest.mark.parametrize('error',[
    psycopg.OperationalError('password authentication failed'),
    psycopg.OperationalError('database "invalid" does not exist'),
    ValueError('invalid accepted configuration'),
])
def test_permanent_startup_error_is_not_retried(monkeypatch,error):
    def fail(settings):raise error
    monkeypatch.setattr(consumers,'accepted_configuration',fail)
    monkeypatch.setattr(consumers.time,'sleep',lambda _:pytest.fail('must not retry'))
    with pytest.raises(type(error)):consumers.startup_settings(SimpleNamespace(),'test')


def test_startup_wait_can_be_stopped_without_waiting_for_backoff(tmp_path):
    marker=tmp_path/'waiting'
    code='''import sys
from pathlib import Path
import psycopg
from assistant_kernel.services import config_consumers as c
def unavailable(settings):
    Path(sys.argv[1]).write_text('waiting')
    raise psycopg.OperationalError('connection refused')
c.accepted_configuration=unavailable
c.startup_settings(None,'signal-test')
'''
    child=subprocess.Popen([sys.executable,'-c',code,str(marker)],stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.05)
        assert marker.exists()
        child.terminate()
        assert child.wait(timeout=3)==0
    finally:
        if child.poll() is None:child.kill();child.wait()


@pytest.mark.parametrize('failure',[asyncio.CancelledError,RuntimeCapacityUnavailable])
async def test_database_loss_during_settlement_does_not_kill_worker_or_replay(tmp_path,monkeypatch,failure):
    stop=asyncio.Event();calls=[]
    class Repository:
        async def ping(self):return True
        async def claim_run(self,*args,**kwargs):
            calls.append('claim')
            if calls.count('claim')==1:
                return {'run_id':'isolated','claim_token':'claim','run_type':'chat','request_json':{}}
            stop.set()
            return None
        async def finish_cancelled_run(self,*args):
            calls.append('settle');raise psycopg.OperationalError('connection refused')
        async def defer_model_capacity(self,*args):
            calls.append('settle');raise psycopg.OperationalError('connection refused')
    repo=Repository();repo.database=repo
    async def process(*args):
        calls.append('execute');raise failure()
    async def configure(*args):pass
    monkeypatch.setattr(worker,'CoreRepository',lambda _:repo)
    monkeypatch.setattr(worker,'RunProcessor',lambda *args:SimpleNamespace(process=process))
    monkeypatch.setattr(worker,'SupervisorProcessor',lambda *args:SimpleNamespace(process=process))
    monkeypatch.setattr(consumers,'ConfigConsumers',lambda _:SimpleNamespace(apply_dynamic=configure))
    settings=KernelSettings.from_environment().model_copy(update={'data_root':tmp_path,'worker_poll_seconds':.01})
    await asyncio.wait_for(worker.run_worker_loop(settings,stop),3)
    assert calls==['claim','execute','settle','claim']


async def test_delayed_database_then_worker_disconnect_recovers_once(core_settings,tmp_path,monkeypatch):
    """Real PostgreSQL in an isolated test DB; a closed port injects unavailability."""
    prepare_agent(core_settings)
    repo=CoreRepository(core_settings)
    async with repo.database.connection() as c:
        for version in (8,9,10,11):
            await c.execute((Path(__file__).parents[1]/'migrations'/f'upgrade_core5_v{version}.sql').read_text())
        await c.commit()
    # Exercise the actual startup reader from a fresh interpreter. Production DB
    # identity is replaced before the first connection; no real outage is caused.
    marker=tmp_path/'database-ready';port=closed_port()
    code='''import sys
from pathlib import Path
from assistant_kernel.config import KernelSettings
from assistant_kernel.services import config_consumers as c
s=KernelSettings.from_environment().model_copy(update={'postgres_database':sys.argv[1],'data_root':Path(sys.argv[2])})
real_host,real_port=s.postgres_host,s.postgres_port
original=c.accepted_configuration
def connect(settings):
    ready=Path(sys.argv[2],'database-ready').exists()
    settings.postgres_host=real_host if ready else '127.0.0.1'
    settings.postgres_port=real_port if ready else int(sys.argv[3])
    return original(settings)
c.accepted_configuration=connect
c.startup_settings(s,'delayed-test')
print('READY',flush=True)
'''
    with (tmp_path/'startup.log').open('w+') as log:
        child=subprocess.Popen([sys.executable,'-c',code,core_settings.postgres_database,str(tmp_path),str(port)],stdout=subprocess.PIPE,stderr=log,text=True)
        try:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                log.flush()
                if 'waiting for database' in (tmp_path/'startup.log').read_text():break
                await asyncio.sleep(.05)
            assert child.poll() is None and 'waiting for database' in (tmp_path/'startup.log').read_text()
            marker.write_text('ready')
            stdout,_=await asyncio.to_thread(child.communicate,timeout=15)
            assert child.returncode==0 and stdout.strip()=='READY'
        finally:
            if child.poll() is None:child.kill();child.wait()
    root=ProjectRuntimeManager(core_settings.data_root).ensure('demo').vault_root
    (root/'once.py').write_text("import os,json\nfrom pathlib import Path\np=Path('executions.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nPath(os.environ['ASSISTANT_RESULT_PATH']).write_text(json.dumps({'ok':True}))\n")
    core_settings.worker_poll_seconds=.05
    stop=asyncio.Event();healthy=asyncio.Event();unavailable=asyncio.Event()
    original_ping=repo.database.ping
    async def ping():
        ok=await original_ping()
        (healthy if ok else unavailable).set()
        return ok
    monkeypatch.setattr(repo.database,'ping',ping)
    monkeypatch.setattr(worker,'CoreRepository',lambda _:repo)
    task=asyncio.create_task(worker.run_worker_loop(core_settings,stop,'runtime'))
    host,actual_port=core_settings.postgres_host,core_settings.postgres_port
    try:
        await asyncio.wait_for(healthy.wait(),10)
        core_settings.postgres_host='127.0.0.1';core_settings.postgres_port=port
        await asyncio.wait_for(unavailable.wait(),10)
        assert not task.done()
        core_settings.postgres_host=host;core_settings.postgres_port=actual_port
        run=await repo.create_run(project_id='demo',agent_key='project:demo:python',run_type='python',request={'source_uri':'vault://demo/once.py'})
        for _ in range(150):
            current=await repo.get_run(str(run['run_id']))
            if current['status']=='succeeded':break
            if task.done():await task
            await asyncio.sleep(.1)
        assert current['status']=='succeeded' and current['attempt']==1
        assert (root/'executions.txt').read_text()=='x'
    finally:
        core_settings.postgres_host=host;core_settings.postgres_port=actual_port
        stop.set()
        await asyncio.wait_for(task,10)
