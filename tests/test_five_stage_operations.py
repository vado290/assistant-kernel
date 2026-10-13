from __future__ import annotations

import copy
import hashlib
import uuid
from contextlib import asynccontextmanager

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.operations import ProjectOperations, validate_action_inputs
from assistant_kernel.storage import ProjectRuntimeManager


class Cursor:
    def __init__(self, value): self.value = value
    async def fetchone(self): return self.value


class Database:
    def __init__(self): self.baseline = None
    @asynccontextmanager
    async def connection(self): yield self
    async def execute(self, query, values):
        return Cursor({"value": {"operational_baseline_ref": self.baseline}})


class Repository:
    def __init__(self): self.database = Database(); self.runs = {}; self.messages = {}; self.dispatched = {}
    async def get_run(self, run_id): return self.runs.get(run_id)
    async def get_message(self, message_id): return self.messages.get(message_id)
    async def create_run(self, **kwargs):
        key = kwargs["idempotency_key"]
        if key not in self.dispatched:
            result = {"run_id": str(uuid.uuid4()), "project_id": kwargs["project_id"],
                      "parent_run_id": kwargs.get("parent_run_id"), "request_json": copy.deepcopy(kwargs["request"]),
                      "status": "queued", "run_type": "python", "idempotency_key": key}
            self.dispatched[key] = result
        return self.dispatched[key]


class Queue:
    def __init__(self): self.items = {}
    async def queue(self, project_id, goal, source_message_id, **kwargs):
        key = kwargs["idempotency_key"]
        self.items.setdefault(key, {"work_id": str(uuid.uuid4()), "project_id": project_id,
                                    "source_message_id": source_message_id, **kwargs})
        return self.items[key]


def fixture(tmp_path, risk="low"):
    settings = KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})
    repository = Repository(); queue = Queue()
    service = ProjectOperations(settings, repository, queue)
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    script = vault / "scripts/collect.py"; script.parent.mkdir(); script.write_text("print('data')\n")
    script_ref = {"uri": "vault://demo/scripts/collect.py"}
    action = {"action_id": "collect", "script_ref": script_ref, "risk": risk,
              "input_schema": {"type": "object", "properties": {
                  "days": {"type": "integer", "minimum": 1, "maximum": 30}},
                  "required": ["days"], "additionalProperties": False},
              "fixed_args": ["--approved-mode"], "shared_write": False, "timeout_seconds": 30}
    documents = TaskDocumentService(settings)
    task_id, publication_id = str(uuid.uuid4()), str(uuid.uuid4())
    contract = documents.write_revision(project_id="demo", task_id=task_id, document_kind="task-contract",
        document_schema="task-contract.v1", payload={"goal": "collect data"}, status="approved",
        actor_type="user", actor_id="user", run_id=task_id, reason="approved requirements")
    design = documents.write_revision(project_id="demo", task_id=task_id, document_kind="solution-design",
        document_schema="solution-design.v1", payload={"task_contract_sha256": contract["sha256"], "runbook_actions": [action]},
        status="approved", actor_type="user", actor_id="user", run_id=task_id, reason="approved design")
    req_message, design_message = str(uuid.uuid4()), str(uuid.uuid4())
    for message_id in (req_message, design_message):
        repository.messages[message_id] = {"project_id": "demo", "direction": "inbound", "content_json": {"text": "approve"}}
    requirements_approval = {"message_id": req_message, "approved_document_sha256": contract["sha256"]}
    design_approval = {"message_id": design_message, "approved_document_sha256": design["sha256"]}
    repository.runs[task_id] = {"run_id": task_id, "project_id": "demo", "agent_key": "project:demo:manager",
        "run_type": "chat", "status": "running", "request_json": {"work_mode": "operate", "current_stage": "operate",
            "accepted_documents": {"task_contract": contract, "solution_design": design},
            "requirements_approval": requirements_approval, "design_approval": design_approval}}
    repository.runs[publication_id] = {"run_id": publication_id, "project_id": "demo", "run_type": "publish",
                                     "status": "succeeded", "parent_run_id": task_id}
    hydrated = copy.deepcopy(action); hydrated["script_ref"]["sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    baseline = {"schema_version": "operational-baseline.v1", "task_id": task_id,
                "publication_run_id": publication_id, "publication_receipt": {"outcome": "published"},
                "task_contract_ref": contract, "solution_design_ref": design, "design_approval": design_approval,
                "runbook_actions": [hydrated], "created_at": "2026-10-04T00:00:00+00:00"}
    repository.database.baseline = baseline
    return service, repository, queue, task_id, script, baseline


@pytest.mark.asyncio
async def test_action_requires_real_design_and_published_baseline(tmp_path):
    service, repository, _, task_id, _, baseline = fixture(tmp_path)
    repository.database.baseline = None
    with pytest.raises(PermissionError, match="no published"):
        await service.run_action("demo", task_id, "collect", {"days": 7}, "once")
    repository.database.baseline = baseline
    repository.messages.clear()
    with pytest.raises(PermissionError, match="actual user approval"):
        await service.run_action("demo", task_id, "collect", {"days": 7}, "once")
    assert repository.dispatched == {}


@pytest.mark.asyncio
async def test_fixed_action_sha_scope_inputs_and_phase_are_enforced(tmp_path, monkeypatch):
    service, repository, _, task_id, script, _ = fixture(tmp_path)
    with pytest.raises(ValueError):
        await service.run_action("demo", task_id, "collect", {"days": 31}, "range")
    with pytest.raises(ValueError):
        await service.run_action("demo", task_id, "collect", {"days": 7, "command": "bad"}, "fields")
    with pytest.raises(PermissionError):
        await service.run_action("other", task_id, "collect", {"days": 7}, "scope")
    monkeypatch.setattr(service.scopes, "resolve", lambda *args, **kwargs: [])
    with pytest.raises(PermissionError, match="authorized scope"):
        await service.run_action("demo", task_id, "collect", {"days": 7}, "revoked-scope")
    monkeypatch.undo()
    repository.runs[task_id]["request_json"]["work_mode"] = "build"
    with pytest.raises(PermissionError, match="Operation"):
        await service.run_action("demo", task_id, "collect", {"days": 7}, "phase")
    repository.runs[task_id]["request_json"]["work_mode"] = "operate"
    script.write_text("print('changed')\n")
    with pytest.raises(PermissionError, match="SHA"):
        await service.run_action("demo", task_id, "collect", {"days": 7}, "sha")
    assert repository.dispatched == {}


@pytest.mark.asyncio
async def test_low_action_idempotency_and_execution_revalidation(tmp_path):
    service, repository, _, task_id, script, _ = fixture(tmp_path)
    first = await service.run_action("demo", task_id, "collect", {"days": 7}, "window-1")
    second = await service.run_action("demo", task_id, "collect", {"days": 7}, "window-1")
    recovered = await service.run_action("demo", task_id, "collect", {"days": 7}, "new-model-token-after-supervisor")
    assert first["run_id"] == second["run_id"] and len(repository.dispatched) == 1
    assert recovered["run_id"] == first["run_id"]
    assert first["request_json"]["args"] == ["--approved-mode"]
    assert first["request_json"]["input"] == {"days": 7}
    await service.validate_dispatch(first)
    script.write_text("print('changed after enqueue')\n")
    with pytest.raises(PermissionError, match="SHA"):
        await service.validate_dispatch(first)


@pytest.mark.asyncio
async def test_high_action_requires_exact_real_approval_and_cannot_repeat_with_new_token(tmp_path):
    service, repository, _, task_id, _, _ = fixture(tmp_path, risk="high")
    prepared = await service.prepare_approval("demo", task_id, "collect", {"days": 7})
    assert "document_markdown" in prepared and repository.dispatched == {}
    with pytest.raises(PermissionError, match="high-risk"):
        await service.run_action("demo", task_id, "collect", {"days": 7}, "missing")
    approval_id, question_id = str(uuid.uuid4()), str(uuid.uuid4())
    binding = {key: prepared[key] for key in ("action_definition_sha256", "baseline_sha256", "input_sha256")}
    repository.runs[task_id]["request_json"]["approved_actions"] = {"collect": {
        "approval_message_id": approval_id, **binding}}
    repository.messages[approval_id] = {"project_id": "demo", "run_id": task_id, "direction": "inbound",
        "reply_to_message_id": question_id, "content_json": {"text": "approve"}}
    repository.messages[question_id] = {"project_id": "demo", "run_id": task_id, "direction": "outbound", "message_type": "approval",
        "content_json": {"operation_approval_request": prepared}}
    one = await service.run_action("demo", task_id, "collect", {"days": 7}, "one")
    two = await service.run_action("demo", task_id, "collect", {"days": 7}, "different-model-token")
    assert one["run_id"] == two["run_id"] and len(repository.dispatched) == 1
    await service.validate_dispatch(one)
    forged = {**one, "idempotency_key": "different-run-key"}
    with pytest.raises(PermissionError, match="execution identity"):
        await service.validate_dispatch(forged)
    with pytest.raises(PermissionError, match="high-risk"):
        await service.run_action("demo", task_id, "collect", {"days": 8}, "changed-input")
    repository.messages[approval_id]["content_json"] = {"text": "reject"}
    with pytest.raises(PermissionError, match="actual user approval"):
        await service.validate_dispatch(one)


@pytest.mark.asyncio
async def test_report_window_uses_existing_queue_and_deduplicates(tmp_path):
    service, _, queue, _, _, _ = fixture(tmp_path)
    window = {"start": "2026-10-01T00:00:00+08:00", "end": "2026-10-02T00:00:00+08:00"}
    source_id = str(uuid.uuid4())
    first = await service.request_report("demo", window, source_id)
    second = await service.request_report("demo", window, source_id)
    assert first == second and len(queue.items) == 1
    assert first["task_kind"] == "operation"
    assert first["report_window"]["timezone"] == "Asia/Shanghai"
    equivalent = {"start": "2026-09-30T16:00:00+00:00", "end": "2026-10-01T16:00:00+00:00"}
    assert await service.request_report("demo", equivalent, source_id) == first
    with pytest.raises(ValueError):
        await service.request_report("demo", {"start": "2026-10-01", "end": "2026-10-02"}, source_id)


def test_action_schema_rejects_bool_as_integer_and_unknown_nested_fields():
    with pytest.raises(ValueError): validate_action_inputs(True, {"type": "integer"})
    with pytest.raises(ValueError): validate_action_inputs({"command": "bad"}, {"type": "object", "properties": {}})
    with pytest.raises(ValueError): validate_action_inputs(100, {"type": "number", "exclusiveMaximum": 10})
