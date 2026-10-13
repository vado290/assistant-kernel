"""Supervisor wrapper for one fixed service generation; child receives a clean environment."""
import asyncio,json,signal,sys
from pathlib import Path
from datetime import datetime,timezone
from assistant_kernel.worker.python_process import python_process_identity,cleanup_python_group

def save(path,value):
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,default=str));temporary.replace(path)

async def main(path):
    launch=json.loads(path.read_text());root=path.parent;stopped=asyncio.Event();loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stopped.set)
    identity=None;process=None;controlled=False;code=None;error=None
    try:
        process=await asyncio.create_subprocess_exec(*launch['command'],cwd=launch['cwd'],env=launch['environment'],
            stdin=asyncio.subprocess.DEVNULL,start_new_session=True)
        identity=python_process_identity(process.pid,launch['instance_id'],launch['generation'])
        save(root/'process.json',identity)
        exited=asyncio.create_task(process.wait());cancel=asyncio.create_task(stopped.wait())
        done,_=await asyncio.wait({exited,cancel},return_when=asyncio.FIRST_COMPLETED)
        controlled=cancel in done
        cleanup=await cleanup_python_group(identity,launch['stop_grace_seconds'])
        if not exited.done():
            try:await asyncio.wait_for(exited,2)
            except asyncio.TimeoutError:pass
        cancel.cancel();await asyncio.gather(cancel,return_exceptions=True);code=process.returncode
    except Exception as exc:
        error=type(exc).__name__;cleanup=await cleanup_python_group(identity,launch['stop_grace_seconds']) if identity else {'status':'verified','reason':'child not launched'}
    save(root/'exit.json',{'exit_code':code,'controlled_stop':controlled,'error_type':error,'cleanup':cleanup,
        'ended_at':datetime.now(timezone.utc).isoformat()})
    return 0 if controlled else code if code is not None else 1

if __name__=='__main__':raise SystemExit(asyncio.run(main(Path(sys.argv[1]))))
