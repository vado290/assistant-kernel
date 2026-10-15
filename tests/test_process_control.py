import sys
import asyncio
import pytest

from assistant_kernel.harness.process_control import run_managed_process


async def test_managed_process_receives_stdin_eof(tmp_path) -> None:
    code, stdout, stderr, _ = await run_managed_process(
        sys.executable,
        "-c",
        "import sys; data=sys.stdin.read(); print(len(data))",
        cwd=str(tmp_path),
        timeout_seconds=2,
    )
    assert code == 0
    assert stdout.strip() == b"0"
    assert stderr == b""


async def test_prompt_pipe_closes_and_timeout_keeps_partial_trace(tmp_path):
    code,out,err,_=await run_managed_process(sys.executable,'-c','import sys; print(len(sys.stdin.buffer.read()))',
        cwd=str(tmp_path),timeout_seconds=3,input_data=b'x'*200000,capture_directory=tmp_path/'complete')
    assert code==0 and out.strip()==b'200000'
    with pytest.raises(asyncio.TimeoutError):
        await run_managed_process(sys.executable,'-c','import time; print("partial",flush=True); time.sleep(10)',
            cwd=str(tmp_path),timeout_seconds=0.3,capture_directory=tmp_path/'timeout')
    assert b'partial' in (tmp_path/'timeout/stdout.jsonl').read_bytes()


async def test_managed_process_observes_cancel_event(tmp_path):
    cancel = asyncio.Event()
    task = asyncio.create_task(run_managed_process(
        sys.executable, '-c', 'import time; time.sleep(10)',
        cwd=str(tmp_path), timeout_seconds=20, cancel_event=cancel,
    ))
    await asyncio.sleep(0.1)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task
