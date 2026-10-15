"""Bounded reconciliation of this invocation's tagged MCP processes on Linux."""
import asyncio, os, signal
from pathlib import Path


def _stat(pid):
    # comm can contain spaces and parentheses; fields after its final ')' are stable.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "state": fields[0], "start_ticks": int(fields[19])}


def members(invocation):
    expected=('KERNEL_MCP_INVOCATION_ID='+invocation).encode();found=[]
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal() or int(path.name)==os.getpid():continue
        try:
            if expected not in (path/'environ').read_bytes().split(b'\0'):continue
            item=_stat(int(path.name))
            if item['state'] not in {'Z','X'}:found.append(item)
        except (FileNotFoundError,ProcessLookupError,PermissionError):continue
    return found


async def cleanup_mcp_processes(invocation):
    """Never kill by name/command or touch a PID whose start ticks changed."""
    terminated=[]
    for sig in (signal.SIGTERM,signal.SIGKILL):
        remaining=members(invocation)
        if not remaining:break
        for item in remaining:
            try:
                if _stat(item['pid'])['start_ticks']!=item['start_ticks']:continue
                if not any(x['pid']==item['pid'] and x['start_ticks']==item['start_ticks'] for x in members(invocation)):continue
                os.kill(item['pid'],sig);terminated.append(item['pid'])
            except (FileNotFoundError,ProcessLookupError):pass
        for _ in range(20):
            if not members(invocation):break
            await asyncio.sleep(.1)
    remaining=members(invocation)
    return {'status':'verified' if not remaining else 'unknown','terminated_pids':sorted(set(terminated)),
        'remaining_pids':[x['pid'] for x in remaining],'scope':'invocation-tagged MCP processes; not OS containment'}
