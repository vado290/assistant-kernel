from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.run_processor import RunProcessor
from assistant_kernel.worker import main as worker_main


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
