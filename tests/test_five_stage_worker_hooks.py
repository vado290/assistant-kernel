from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.worker import main as worker_main
from assistant_kernel.services import operations, project_changes, workspace_scope


async def test_managed_python_validates_before_source_and_process(tmp_path, monkeypatch):
    settings = KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})
    validate = AsyncMock(side_effect=PermissionError("operation authorization changed"))
    monkeypatch.setattr(operations, "ProjectOperations", lambda *args: SimpleNamespace(validate_dispatch=validate))
    process = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", process)
    run = {"project_id": "demo", "run_id": "run-1", "claim_token": "lease",
           "request_json": {"operation_action": {"action_id": "fixed-script"}, "source_uri": "vault://demo/missing.py"}}
    with pytest.raises(PermissionError, match="authorization"):
        await worker_main.execute_python(run, settings, SimpleNamespace(), "worker", asyncio.Event())
    validate.assert_awaited_once_with(run)
    process.assert_not_awaited()
    run["request_json"].pop("operation_action")
    with pytest.raises(ValueError, match="existing"):
        await worker_main.execute_python(run, settings, SimpleNamespace(), "worker", asyncio.Event())
    assert validate.await_count == 1


async def test_map_failure_after_publication_finishes_without_replaying_publisher(tmp_path, monkeypatch):
    settings = KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})
    shutdown = asyncio.Event()
    recorded = {}
    run = {"project_id": "demo", "run_id": "publish-1", "run_type": "publish", "claim_token": "lease",
           "parent_run_id": "task-1", "request_json": {}}

    async def finish(*args, **kwargs):
        recorded.update(kwargs)
        shutdown.set()

    repository = SimpleNamespace(database=SimpleNamespace(ping=AsyncMock(return_value=True)),
        claim_run=AsyncMock(return_value=run), record_process_identity=AsyncMock(return_value=True),
        run_claim_is_active=AsyncMock(return_value=True), clear_publisher_process=AsyncMock(),
        get_run=AsyncMock(return_value={"request_json": {"accepted_documents": {"solution_design": {"kind": "document", "uri": "vault://demo/design.json"}}}}),
        finish_publish_run=finish, retry_run=AsyncMock(), finish_cancelled_run=AsyncMock())
    monkeypatch.setattr(worker_main, "CoreRepository", lambda *args: repository)
    monkeypatch.setattr(worker_main, "RunProcessor", lambda *args: SimpleNamespace())
    monkeypatch.setattr(worker_main, "SupervisorProcessor", lambda *args: SimpleNamespace())
    monkeypatch.setattr(worker_main, "CodexCliHarness", lambda *args: SimpleNamespace())
    monkeypatch.setattr(worker_main, "WorkerHealthReporter", lambda *args: SimpleNamespace(update=lambda **kwargs: None))

    class Publisher:
        calls = 0

        def publish(self, **kwargs):
            self.calls += 1
            return {"outcome": "published", "map_change_batch": {"batch_id": "publisher:publish-1",
                "task_id": "task-1", "changes": []}}, []

    publisher = Publisher()
    monkeypatch.setattr(worker_main, "CapabilityBundleService", lambda *args: publisher)
    capture = AsyncMock(side_effect=OSError("Map storage temporarily unavailable"))
    stale = []
    monkeypatch.setattr(project_changes, "ProjectChangeService", lambda *args: SimpleNamespace(
        capture_batch=capture, maps=SimpleNamespace(mark_stale=lambda *args, **kwargs: stale.append(args))))
    monkeypatch.setattr(workspace_scope, "WorkspaceScopeService", lambda *args: SimpleNamespace(resolve=lambda *args, **kwargs: []))
    await worker_main.run_worker_loop(settings, shutdown)
    assert publisher.calls == 1
    repository.retry_run.assert_not_awaited()
    assert recorded["result"]["outcome"] == "published"
    assert recorded["result"]["map_refresh_pending"]["error_type"] == "OSError"
    assert recorded["result"]["map_change_batch"]["batch_id"] == "publisher:publish-1"
    assert capture.await_args.kwargs["reason_source_refs"][0]["uri"] == "vault://demo/design.json"
    assert stale


async def test_operation_receipt_handoff_failure_never_replays_successful_action():
    callback = AsyncMock(side_effect=OSError("Parent handoff unavailable"))
    repository = SimpleNamespace(resume_completed_operation=callback)
    managed = {"run_id": "settled-operation", "request_json": {"operation_action": {"action_id": "fixed"}}}
    await worker_main._resume_operation_parent(managed, repository)
    callback.assert_awaited_once_with("settled-operation")
    await worker_main._resume_operation_parent({"run_id": "ordinary", "request_json": {}}, repository)
    assert callback.await_count == 1


async def test_first_modern_pm_failure_settles_for_supervisor_instead_of_replaying(tmp_path, monkeypatch):
    settings = KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})
    shutdown = asyncio.Event()
    run = {"project_id": "demo", "run_id": "pm-1", "agent_key": "project:demo:manager",
           "run_type": "chat", "claim_token": "lease", "request_json": {}}
    settlements = []

    async def settle(**kwargs):
        settlements.append(kwargs)
        shutdown.set()

    repository = SimpleNamespace(database=SimpleNamespace(ping=AsyncMock(return_value=True)),
        claim_run=AsyncMock(return_value=run), retry_run=AsyncMock(return_value="queued"),
        get_run=AsyncMock(return_value=run), _five_stage_task=lambda row: True,
        settle_failed_pm_turn=settle)
    processor = SimpleNamespace(process=AsyncMock(side_effect=ValueError("invalid final JSON")))
    monkeypatch.setattr(worker_main, "CoreRepository", lambda *args: repository)
    monkeypatch.setattr(worker_main, "RunProcessor", lambda *args: processor)
    monkeypatch.setattr(worker_main, "SupervisorProcessor", lambda *args: SimpleNamespace())
    monkeypatch.setattr(worker_main, "CodexCliHarness", lambda *args: SimpleNamespace())
    monkeypatch.setattr(worker_main, "WorkerHealthReporter", lambda *args: SimpleNamespace(update=lambda **kwargs: None))
    await worker_main.run_worker_loop(settings, shutdown)
    assert processor.process.await_count == 1 and repository.claim_run.await_count == 1
    assert settlements[0]["task_id"] == "pm-1"
