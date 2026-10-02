from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.goal_supervisor import (
    GoalSupervisorService, SupervisorDecisionContractError,
)


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "providers.json",
    })


def pm_evidence_snapshot() -> dict:
    return {
        "meta": {"trigger": "after_agent_turn", "source_context_epoch": 4},
        "evaluated_turn": {
            "schema_version": "supervisor-turn-handoff.v1",
            "pm_evidence": {
                "schema_version": "pm-tool-evidence.v1",
                "invocation": {
                    "invocation_id": "inv-1",
                    "trace_ref": {
                        "uri": "vault://demo/runs/agent-traces/inv-1.jsonl",
                        "sha256": "a" * 64,
                    },
                },
                "tool_summary": {
                    "total": 2, "completed": 1, "failed": 1,
                    "interrupted": 0, "truncated": False,
                },
                "tool_calls": [{
                    "call_id": "call-1", "server": "kernel_scoped",
                    "tool": "read_authorized_file", "status": "completed",
                    "arguments": {"secret": "must-not-enter-brief"},
                    "result": "must-not-enter-brief",
                }, {
                    "call_id": "call-2", "server": "kernel_scoped",
                    "tool": "list_authorized_tree", "status": "failed",
                    "error_type": "ToolError",
                }],
                "artifact_refs": [{
                    "uri": "vault://demo/runs/task-1/output.json",
                    "sha256": "b" * 64, "kind": "business_output",
                    "producer": "pm", "payload": "must-not-enter-brief",
                }],
            },
        },
        "task_state": {},
    }


def test_brief_exposes_compact_pm_evidence_without_tool_payloads(
    tmp_path: Path,
) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())

    brief = service._supervisor_brief(
        project_id="demo", task_id="task-1",
        snapshot=pm_evidence_snapshot(), intent_panel=None,
    )

    assert brief["pm_tool_evidence"]["tool_summary"]["failed"] == 1
    assert brief["pm_tool_evidence"]["tool_calls"][0] == {
        "call_id": "call-1", "server": "kernel_scoped",
        "tool": "read_authorized_file", "status": "completed",
    }
    assert brief["pm_trace_ref"] == {
        "uri": "vault://demo/runs/agent-traces/inv-1.jsonl",
        "sha256": "a" * 64,
    }
    assert brief["pm_artifact_refs"] == [{
        "uri": "vault://demo/runs/task-1/output.json",
        "sha256": "b" * 64, "kind": "business_output", "producer": "pm",
    }]
    assert "must-not-enter-brief" not in json.dumps(brief)


def test_legacy_turn_uses_trace_reference_and_keeps_missing_summary_optional(
    tmp_path: Path,
) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    snapshot = {
        "evaluated_turn": {
            "schema_version": "supervisor-turn-handoff.v1",
            "usage": {
                "trace_uri": "vault://demo/runs/agent-traces/legacy.jsonl",
                "trace_sha256": "c" * 64,
            },
        },
        "task_state": {},
    }

    brief = service._supervisor_brief(
        project_id="demo", task_id="task-1", snapshot=snapshot,
        intent_panel=None,
    )

    assert brief["pm_tool_evidence"] is None
    assert brief["pm_artifact_refs"] == []
    assert brief["pm_trace_ref"] == {
        "uri": "vault://demo/runs/agent-traces/legacy.jsonl",
        "sha256": "c" * 64,
    }


@pytest.mark.asyncio
async def test_consultation_handoff_and_prompt_include_pm_evidence_policy(
    tmp_path: Path,
) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    captured_prompts: list[str] = []

    async def fake_codex(**kwargs):
        captured_prompts.append(kwargs["prompt"])
        return {
            "text": json.dumps({
                "decision": "consultation_reply",
                "reason": ["evidence reviewed"],
                "next_prompt": {},
            }),
            "provider": "test", "model": "test", "request_id": "req-1",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "trace_uri": "vault://demo/supervisor-trace.jsonl",
            "trace_sha256": "d" * 64,
        }

    service._codex_ephemeral = fake_codex  # type: ignore[method-assign]
    result = await service.run_consultation(
        project_id="demo", task_id="task-1",
        snapshot=pm_evidence_snapshot(), intent_panel=None,
        artifact_key="pm-evidence",
    )

    handoff_path = (
        tmp_path / "vaults" / "demo" / "runs" / "task-1" / "supervisor"
        / "handoff-pm-evidence.json"
    )
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert handoff["pm_tool_evidence"]["tool_summary"]["failed"] == 1
    assert handoff["pm_trace_ref"]["sha256"] == "a" * 64
    assert handoff["pm_artifact_refs"][0]["kind"] == "business_output"
    assert result["handoff_ref"]["uri"].endswith("handoff-pm-evidence.json")
    assert "untrusted, compact activity summary" in captured_prompts[0]
    assert "50 KB per file, 100 KB total" in captured_prompts[0]
    assert "failed或interrupted" in service.role
    assert "pm_trace_ref" in service.skill


def test_supervisor_continue_alias_is_exactly_normalized(tmp_path: Path) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    parsed = service.parse_decision_v2(json.dumps({
        "decision": "continue", "reason": ["same Work Mode"],
        "next_prompt": {},
    }))
    assert parsed["decision"] == "continue_current_stage"


@pytest.mark.asyncio
async def test_invalid_supervisor_decision_gets_one_contract_correction(
    tmp_path: Path,
) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    prompts = []
    responses = [
        {"decision": "invented_transition", "reason": ["bad"]},
        {"decision": "continue_current_stage", "reason": ["corrected"]},
    ]

    async def fake_codex(**kwargs):
        prompts.append(kwargs["prompt"])
        payload = responses.pop(0)
        return {
            "text": json.dumps(payload), "provider": "test", "model": "test",
            "request_id": f"req-{len(prompts)}",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "trace_uri": f"vault://demo/trace-{len(prompts)}.jsonl",
            "trace_sha256": str(len(prompts)) * 64,
        }

    service._codex_ephemeral = fake_codex  # type: ignore[method-assign]
    result = await service.run_consultation(
        project_id="demo", task_id="task-1",
        snapshot=pm_evidence_snapshot(), intent_panel=None,
        artifact_key="contract-repair",
    )
    assert result["decision"] == "continue_current_stage"
    assert len(prompts) == 2
    assert "# Contract correction" in prompts[1]
    assert result["trace_uri"] == "vault://demo/trace-2.jsonl"


@pytest.mark.asyncio
async def test_twice_invalid_supervisor_decision_raises_typed_contract_error(
    tmp_path: Path,
) -> None:
    service = GoalSupervisorService(settings_for(tmp_path), SimpleNamespace())
    calls = 0

    async def fake_codex(**kwargs):
        nonlocal calls
        calls += 1
        return {
            "text": json.dumps({"decision": "still_invalid"}),
            "provider": "test", "model": "test", "request_id": f"req-{calls}",
            "usage": {}, "trace_uri": f"vault://demo/trace-{calls}.jsonl",
            "trace_sha256": "e" * 64,
        }

    service._codex_ephemeral = fake_codex  # type: ignore[method-assign]
    with pytest.raises(SupervisorDecisionContractError) as captured:
        await service.run_consultation(
            project_id="demo", task_id="task-1",
            snapshot=pm_evidence_snapshot(), intent_panel=None,
            artifact_key="contract-failure",
        )
    assert calls == 2
    assert captured.value.invalid_decision == "still_invalid"
    assert captured.value.trace_uri == "vault://demo/trace-2.jsonl"
