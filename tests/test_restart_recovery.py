from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.run_processor import RunProcessor
from assistant_kernel.worker import main as worker_main
from test_core4 import core_settings, prepare_agent


@pytest.mark.parametrize('old_process_alive',[False,True])
async def test_maintenance_keeps_legacy_supervisor_quarantined_for_manual_action(
    core_settings, monkeypatch, old_process_alive,
) -> None:
    import os
    import uuid
    from pathlib import Path
    from psycopg.types.json import Jsonb
    from assistant_kernel.repositories import CoreRepository
    from assistant_kernel.worker.process_control import process_start_ticks
    from assistant_kernel.services.reconciliation import ReconciliationService

    prepare_agent(core_settings)
    repo=CoreRepository(core_settings)
    async with repo.database.connection() as c:
        for version in (8,9,10,11):
            await c.execute((Path(__file__).parents[1]/'migrations'/f'upgrade_core5_v{version}.sql').read_text())
        await c.commit()
    incoming=await repo.ingest_message(project_id='demo',agent_key='project:demo:supervisor',
        channel='discord',chat_id='manual-recovery',user_id='test-owner',external_event_id=str(uuid.uuid4()),
        external_message_id=None,content={'text':'isolated recovery test'})
    run=await repo.create_run(project_id='demo',agent_key='project:demo:supervisor',run_type='chat',
        trigger_message_id=str(incoming['message']['message_id']),
        request={'interaction_kind':'supervisor_consultation','task_id':str(uuid.uuid4())})
    async with repo.database.connection() as c:
        if incoming.get('run'):
            await c.execute("UPDATE assistant.runs SET status='cancelled' WHERE run_id=%s",(incoming['run']['run_id'],))
        await c.execute("UPDATE assistant.runs SET status='reconciling',attempt=1,process_pid=%s,process_start_ticks=%s,error_json=%s WHERE run_id=%s",
            (os.getpid() if old_process_alive else None,process_start_ticks(os.getpid()) if old_process_alive else None,
             Jsonb({'type':'LeaseExpired','message':'isolated old attempt'}),run['run_id']))
        await c.commit()
    stop=asyncio.Event()
    async def one_iteration(*args):stop.set()
    monkeypatch.setattr(worker_main,'_poll_or_shutdown',one_iteration)
    await asyncio.wait_for(worker_main.run_maintenance_loop(core_settings,stop),15)
    current=await repo.get_run(str(run['run_id']))
    assert current['status']=='reconciling' and current['attempt']==1
    assert await repo.claim_run('isolated',str(uuid.uuid4()),60) is None
    card_id=current['executor_json']['reconciliation']['card_message_id']
    card=await repo.get_message(card_id)
    assert card['message_type']=='reconciliation'
    actions={action['id'] for action in card['content_json']['actions']}
    assert ('wait' in actions) if old_process_alive else ('retry' in actions)
    assert await ReconciliationService(core_settings,repo).notify_due()==[]


@pytest.mark.asyncio
async def test_poll_wait_wakes_immediately_for_shutdown() -> None:
    shutdown = asyncio.Event()
    shutdown.set()
    await asyncio.wait_for(
        worker_main._poll_or_shutdown(shutdown, timeout_seconds=60), timeout=0.5
    )


@pytest.mark.asyncio
async def test_worker_with_preexisting_shutdown_does_not_claim(monkeypatch, tmp_path) -> None:
    claimed = False

    class Repository:
        async def claim_run(self, *args, **kwargs):
            nonlocal claimed
            claimed = True
            return None

    repository = Repository()
    monkeypatch.setattr(worker_main, "CoreRepository", lambda settings: repository)
    monkeypatch.setattr(
        worker_main, "ProjectRuntimeManager", lambda data_root: SimpleNamespace()
    )
    monkeypatch.setattr(worker_main, "CodexCliHarness", lambda runtime: SimpleNamespace())
    monkeypatch.setattr(worker_main, "RunProcessor", lambda *args: SimpleNamespace())
    monkeypatch.setattr(
        worker_main, "ReconciliationService", lambda *args: SimpleNamespace()
    )
    shutdown = asyncio.Event()
    shutdown.set()
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    await worker_main.run_worker_loop(settings, shutdown)
    assert claimed is False


@pytest.mark.asyncio
async def test_maintenance_with_preexisting_shutdown_does_not_touch_database(
    monkeypatch, tmp_path,
) -> None:
    constructed = False

    def repository_factory(settings):
        nonlocal constructed
        constructed = True
        return SimpleNamespace()

    monkeypatch.setattr(worker_main, "CoreRepository", repository_factory)
    shutdown = asyncio.Event()
    shutdown.set()
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    await worker_main.run_maintenance_loop(settings, shutdown)
    assert constructed is True


def test_retry_trace_detects_incomplete_resumed_turn(tmp_path) -> None:
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    processor = RunProcessor(settings, SimpleNamespace(), SimpleNamespace())
    runtime = processor.runtime.ensure("demo")
    invocation = "run-a1-claim"
    trace_root = runtime.vault_root / "runs/agent-traces/invocations" / invocation
    trace_root.mkdir(parents=True)
    (trace_root / "stdout.jsonl").write_text(
        "\n".join((
            json.dumps({"type": "thread.started", "thread_id": "thread-old"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}),
        )) + "\n",
        encoding="utf-8",
    )
    result = processor._prior_attempt_trace_state(
        {"attempt": 2, "project_id": "demo"},
        {"trace_uri": f"vault://demo/runs/agent-traces/invocations/{invocation}/"},
    )
    assert result == {
        "state": "incomplete",
        "thread_id": "thread-old",
        "trace_uri": f"vault://demo/runs/agent-traces/invocations/{invocation}/",
        "turn_started": True,
        "turn_completed": False,
        "turn_failed": False,
    }


def test_retry_trace_distinguishes_completed_and_first_attempt(tmp_path) -> None:
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    processor = RunProcessor(settings, SimpleNamespace(), SimpleNamespace())
    runtime = processor.runtime.ensure("demo")
    trace_root = runtime.vault_root / "runs/agent-traces/invocations/completed"
    trace_root.mkdir(parents=True)
    (trace_root / "stdout.jsonl").write_text(
        "\n".join((
            json.dumps({"type": "thread.started", "thread_id": "thread-complete"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        )) + "\n",
        encoding="utf-8",
    )
    executor = {"trace_uri": "vault://demo/runs/agent-traces/invocations/completed/"}
    completed = processor._prior_attempt_trace_state(
        {"attempt": 2, "project_id": "demo"}, executor
    )
    assert completed and completed["state"] == "completed"
    assert processor._prior_attempt_trace_state(
        {"attempt": 1, "project_id": "demo"}, executor
    ) is None
