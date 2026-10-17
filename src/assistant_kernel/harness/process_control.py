from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from collections.abc import Awaitable, Callable
from pathlib import Path


async def terminate_process_group(process: asyncio.subprocess.Process, grace_seconds: float = 5) -> None:
    """Terminate the complete Linux process group, then force kill it."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()


def process_start_ticks(pid: int) -> int | None:
    """Return Linux /proc start ticks so a recycled PID is never killed by mistake."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return int(fields[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError):
        return None


async def run_managed_process(
    *command: str,
    cwd: str,
    timeout_seconds: float,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    env: Mapping[str, str] | None = None,
    input_data: bytes | None = None,
    capture_directory: Path | None = None,
    cancel_event: asyncio.Event | None = None,
    on_started: Callable[[int, int | None], Awaitable[None]] | None = None,
) -> tuple[int, bytes, bytes, int]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        # Supervisor keeps the worker's stdin open. Codex otherwise waits for
        # "additional input" forever after reading the prompt argument.
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        env=env,
        start_new_session=True,
    )
    start_ticks = process_start_ticks(process.pid)
    if on_started is not None:
        await on_started(process.pid, start_ticks)
    readers = []
    buffers = [bytearray(), bytearray()]
    async def capture(stream, path, buffer):
        with path.open('wb') as target:
            while chunk := await stream.read(65536):
                target.write(chunk); target.flush(); buffer.extend(chunk)
    async def exchange():
        if capture_directory is None:
            return await process.communicate(input=input_data)
        capture_directory.mkdir(parents=True, exist_ok=True)
        readers.extend([asyncio.create_task(capture(process.stdout,capture_directory/'stdout.jsonl',buffers[0])),
                        asyncio.create_task(capture(process.stderr,capture_directory/'stderr.log',buffers[1]))])
        if process.stdin is not None:
            try:
                process.stdin.write(input_data or b'');await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):pass
            finally:process.stdin.close()
        await process.wait()
        await asyncio.gather(*readers)
        return bytes(buffers[0]),bytes(buffers[1])
    exchange_task = asyncio.create_task(exchange())
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    wait_set = {exchange_task}
    if cancel_task is not None:
        wait_set.add(cancel_task)
    try:
        done, _ = await asyncio.wait(
            wait_set, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if exchange_task in done:
            out, err = await exchange_task
        elif cancel_task is not None and cancel_task in done:
            await terminate_process_group(process)
            out, err = await exchange_task
            raise asyncio.CancelledError("managed process cancellation was requested")
        else:
            await terminate_process_group(process)
            await exchange_task
            raise asyncio.TimeoutError
    except asyncio.TimeoutError:
        await terminate_process_group(process)
        if readers:await asyncio.gather(*readers, return_exceptions=True)
        raise
    except asyncio.CancelledError:
        await terminate_process_group(process)
        if readers:await asyncio.gather(*readers, return_exceptions=True)
        raise
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
    return process.returncode or 0, out or b"", err or b"", process.pid
