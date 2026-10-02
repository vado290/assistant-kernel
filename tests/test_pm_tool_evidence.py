import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.run_processor import (
    PM_TOOL_EVIDENCE_LIMIT,
    RunProcessor,
    summarize_native_delegations,
    summarize_pm_tool_evidence,
)


def _tool_event(event_type: str, call_id: str, *, status: str, error=None):
    return {
        "type": event_type,
        "item": {
            "id": call_id,
            "type": "mcp_tool_call",
            "server": "kernel_scoped",
            "tool": f"tool_{call_id}",
            "status": status,
            "arguments": {"secret": "must-not-leak"},
            "result": {"content": "must-not-leak"},
            "error": error,
        },
    }


def test_pm_tool_evidence_aggregates_calls_without_payloads() -> None:
    events = [
        _tool_event("item.started", "ok", status="in_progress"),
        _tool_event("item.completed", "ok", status="completed"),
        _tool_event(
            "item.completed", "bad", status="failed",
            error={"type": "PermissionError", "message": "secret"},
        ),
        _tool_event("item.started", "lost", status="in_progress"),
        {"type": "item.completed", "item": {"type": "agent_message", "text": "x"}},
    ]

    evidence = summarize_pm_tool_evidence(
        invocation_id="inv-1",
        events=events,
        trace_uri="vault://demo/trace.jsonl",
        trace_sha256="abc",
        usage={
            "input_tokens": 11,
            "cached_input_tokens": 7,
            "output_tokens": 3,
            "reasoning_output_tokens": 2,
            "unrelated": "ignored",
        },
        artifact_refs=[{
            "uri": "vault://demo/report.json", "sha256": "def",
            "kind": "report", "producer": "pm", "payload": "must-not-leak",
        }],
    )

    assert evidence["schema_version"] == "pm-tool-evidence.v1"
    assert evidence["invocation"] == {
        "invocation_id": "inv-1",
        "trace_ref": {"uri": "vault://demo/trace.jsonl", "sha256": "abc"},
        "usage": {
            "input_tokens": 11,
            "cached_input_tokens": 7,
            "output_tokens": 3,
            "reasoning_output_tokens": 2,
        },
    }
    assert evidence["tool_summary"] == {
        "total": 3, "completed": 1, "failed": 1,
        "interrupted": 1, "truncated": False,
    }
    assert [item["status"] for item in evidence["tool_calls"]] == [
        "completed", "failed", "interrupted",
    ]
    assert evidence["tool_calls"][1]["error_type"] == "PermissionError"
    assert evidence["artifact_refs"] == [{
        "uri": "vault://demo/report.json", "sha256": "def",
        "kind": "report", "producer": "pm",
    }]
    encoded = json.dumps(evidence)
    assert "arguments" not in encoded
    assert "result" not in encoded
    assert "must-not-leak" not in encoded
    assert "unrelated" not in encoded


def test_pm_tool_evidence_is_bounded_and_handles_no_tools() -> None:
    events = [
        _tool_event("item.completed", str(index), status="completed")
        for index in range(PM_TOOL_EVIDENCE_LIMIT + 3)
    ]
    artifacts = [
        {"uri": f"vault://demo/{index}.json", "sha256": str(index)}
        for index in range(PM_TOOL_EVIDENCE_LIMIT + 3)
    ]
    evidence = summarize_pm_tool_evidence(
        invocation_id="inv-2", events=events,
        trace_uri=None, trace_sha256=None, usage={}, artifact_refs=artifacts,
    )
    assert evidence["tool_summary"] == {
        "total": PM_TOOL_EVIDENCE_LIMIT + 3,
        "completed": PM_TOOL_EVIDENCE_LIMIT + 3,
        "failed": 0, "interrupted": 0, "truncated": True,
    }
    assert len(evidence["tool_calls"]) == PM_TOOL_EVIDENCE_LIMIT
    assert len(evidence["artifact_refs"]) == PM_TOOL_EVIDENCE_LIMIT

    empty = summarize_pm_tool_evidence(
        invocation_id="inv-empty", events=[], trace_uri=None,
        trace_sha256=None, usage={}, artifact_refs=[],
    )
    assert empty["tool_summary"] == {
        "total": 0, "completed": 0, "failed": 0,
        "interrupted": 0, "truncated": False,
    }
    assert empty["tool_calls"] == []


def test_native_delegation_evidence_is_bounded() -> None:
    evidence = summarize_native_delegations([{
        "type": "item.completed",
        "item": {
            "type": "create_subagent_call", "id": "call-1",
            "subagent_id": "agent-1", "status": "completed",
        },
    }])
    assert evidence["schema_version"] == "native-delegation-evidence.v1"
    assert evidence["events"][0]["agent_id"] == "agent-1"


def test_native_delegation_result_retires_active_request(tmp_path: Path) -> None:
    settings = KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})
    documents = TaskDocumentService(settings)
    request_ref = documents.write_revision(
        project_id="demo", task_id="task-1", document_kind="delegation-native",
        document_schema="delegation-request.v1",
        payload={
            "delegation_id": "native-1", "executor": "native_subagent",
            "goal": "review", "required": True,
        },
        status="requested", actor_type="supervisor", actor_id="supervisor",
        run_id="task-1", reason="test",
    )
    processor = RunProcessor.__new__(RunProcessor)
    processor.task_documents = documents
    run = {
        "run_id": "task-1", "agent_key": "project:demo:manager",
        "request_json": {"accepted_documents": {
            "active_delegations": [request_ref],
        }},
    }
    accepted, result_refs = processor._settle_native_delegations(
        run=run, project_id="demo",
        envelope={"delegation_results": [{
            "delegation_id": "native-1", "status": "completed",
            "summary": "reviewed", "findings": ["ok"],
        }]},
        usage_snapshot={"native_delegations": {
            "schema_version": "native-delegation-evidence.v1",
            "events": [{"status": "completed"}],
        }},
    )
    assert accepted["active_delegations"] == []
    assert accepted["child_results"] == result_refs
    result = documents.read("demo", result_refs[0])
    assert result["payload"]["delegation_id"] == "native-1"
    assert result["payload"]["status"] == "completed"

    with pytest.raises(ValueError, match="required native Delegation Results"):
        processor._settle_native_delegations(
            run=run, project_id="demo", envelope={}, usage_snapshot={},
        )


class _Repository:
    def __init__(self) -> None:
        self.created_request = None

    async def get_run(self, task_id):
        return {
            "run_id": task_id, "status": "waiting_user",
            "request_json": {"task_turn_count": 1, "supervisor_context_epoch": 3},
        }

    async def get_message(self, message_id):
        return {"message_id": message_id, "created_at": "2026-10-01T00:00:00Z"}

    async def create_run(self, **kwargs):
        self.created_request = kwargs["request"]
        return {"run_id": "supervisor-run"}


class _CurrentArtifacts:
    def view(self, project_id, task_id):
        return {}


class _Supervisor:
    def configuration(self, project_id):
        return {"enabled": True}


async def test_turn_handoff_persists_top_level_pm_evidence(tmp_path: Path) -> None:
    processor = RunProcessor.__new__(RunProcessor)
    processor.repository = _Repository()
    processor.goal_supervisor = _Supervisor()
    processor.runtime = SimpleNamespace(
        ensure=lambda project_id: SimpleNamespace(vault_root=tmp_path)
    )
    usage = {
        "pm_evidence": summarize_pm_tool_evidence(
            invocation_id="inv-3",
            events=[_tool_event("item.completed", "one", status="completed")],
            trace_uri="vault://demo/trace.jsonl", trace_sha256="abc", usage={},
        )
    }
    artifacts = [{
        "uri": "vault://demo/result.json", "sha256": "def", "kind": "result",
    }]

    await processor._queue_turn_evaluation(
        project_id="demo", task_id="task-1", turn_number=1,
        trigger={
            "message_id": "source-1", "channel": "discord",
            "chat_id": "chat-1", "user_id": "user-1",
        },
        reply_message_id="reply-1", outcome="completed", summary="done",
        usage=usage, artifacts=artifacts,
    )

    handoff_path = (
        tmp_path / "runs" / "task-1" / "supervisor" / "turn-handoffs"
        / "turn-1.json"
    )
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff["schema_version"] == "supervisor-turn-handoff.v1"
    assert handoff["pm_evidence"]["schema_version"] == "pm-tool-evidence.v1"
    assert handoff["pm_evidence"]["artifact_refs"] == [{
        "uri": "vault://demo/result.json", "sha256": "def",
        "kind": "result", "producer": "pm",
    }]
    assert handoff["usage"]["pm_evidence"] == handoff["pm_evidence"]
    assert processor.repository.created_request["turn_handoff_ref"]["sha256"]
