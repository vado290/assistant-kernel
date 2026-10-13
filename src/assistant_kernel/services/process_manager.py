"""Small supervisor adapter. Kernel owns retries; the manager never restarts business code."""
import asyncio,hashlib,json,os,shlex,sys
from pathlib import Path


class ProcessManager:
    def __init__(self,settings):
        self.root=settings.data_root/'runtime/kernel-services'
        self.config=Path(os.getenv('KERNEL_SUPERVISOR_CONFIG','/etc/supervisor/conf.d/assistant.conf'))

    async def command(self,*args,timeout=15):
        process=await asyncio.create_subprocess_exec('supervisorctl','-c',str(self.config),*args,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        try:out,_=await asyncio.wait_for(process.communicate(),timeout)
        except BaseException:
            process.kill();await process.wait();raise
        state=out.decode(errors='replace').strip().split()
        known_status=args and args[0]=='status' and len(state)>1 and state[1] in {'STOPPED','RUNNING','STARTING','STOPPING','EXITED','FATAL','BACKOFF','UNKNOWN'}
        if process.returncode and not known_status:raise RuntimeError('Process manager command failed: '+out.decode(errors='replace')[:1000])
        return out.decode()

    @staticmethod
    def key(instance_id):return 'kernel-service-'+hashlib.sha256(str(instance_id).encode()).hexdigest()[:24]

    async def ensure(self,instance_id,launch_path,limits):
        key=self.key(instance_id);programs=self.root/'programs';programs.mkdir(parents=True,exist_ok=True)
        path=programs/(key+'.conf');logs=launch_path.parent
        command=' '.join(shlex.quote(x) for x in (sys.executable,'-m','assistant_kernel.worker.service_main',str(launch_path)))
        if any('\n' in str(x) or '\r' in str(x) for x in (command,logs)):raise ValueError('Invalid managed program path')
        # supervisor uses %(...) interpolation even inside commands.
        command=command.replace('%','%%');log_path=str(logs).replace('%','%%')
        text=f'''[program:{key}]
command={command}
directory={log_path}
autostart=false
autorestart=false
startsecs=0
startretries=0
stopasgroup=true
killasgroup=true
stopwaitsecs={limits['stop_grace_seconds']+15}
stdout_logfile={log_path}/stdout.log
stderr_logfile={log_path}/stderr.log
stdout_logfile_maxbytes={limits['log_limit_bytes']}
stderr_logfile_maxbytes={limits['log_limit_bytes']}
stdout_logfile_backups=1
stderr_logfile_backups=1
'''
        if path.exists() and path.read_text()!=text:raise PermissionError('Immutable service program definition changed')
        if not path.exists():path.write_text(text)
        await self.command('reread');await self.command('update',key)
        return key

    async def start(self,key):
        status=await self.probe(key)
        if status['state'] in {'RUNNING','STARTING'}:return status
        await self.command('start',key);return await self.probe(key)

    async def probe(self,key):
        if not key.startswith('kernel-service-'):raise PermissionError('Not a Kernel business program')
        raw=await self.command('status',key);parts=raw.strip().split()
        return {'program':key,'state':parts[1] if len(parts)>1 else 'UNKNOWN','manager_output':raw.strip()}

    async def stop(self,key):
        status=await self.probe(key)
        if status['state'] in {'RUNNING','STARTING'}:
            import configparser
            config=configparser.RawConfigParser();config.read(self.root/'programs'/(key+'.conf'))
            await self.command('stop',key,timeout=config.getint('program:'+key,'stopwaitsecs',fallback=45)+5)
        return await self.probe(key)

    async def remove(self,key):
        status=await self.probe(key)
        if status['state'] in {'RUNNING','STARTING','STOPPING'}:raise PermissionError('Managed program is still active')
        await self.command('remove',key)
        path=self.root/'programs'/(key+'.conf')
        if path.resolve().parent!=(self.root/'programs').resolve():raise PermissionError('Unexpected managed path')
        path.unlink(missing_ok=True)
