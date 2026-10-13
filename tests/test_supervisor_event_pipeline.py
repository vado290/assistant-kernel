"""Focused contracts for immutable Turn handoffs and stale Supervisor fencing."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.current_artifacts import CurrentArtifactService
from assistant_kernel.services.run_processor import RunProcessor
from assistant_kernel.services.supervisor_processor import SupervisorProcessor


class TurnRepository:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.legacy_degraded: list[dict] = []
        self.task = {
            "run_id": "task-1", "project_id": "p", "status": "waiting_user",
            "request_json": {
                "task_turn_count": 1, "task_phase": "waiting_user",
                "current_stage": "requirements", "supervisor_context_epoch": 7,
            },
        }

    async def get_run(self, run_id: str):
        return self.task if run_id == "task-1" else None

    async def get_message(self, message_id: str):
        return {
            "message_id": message_id, "created_at": "2026-09-30T01:02:03+00:00",
        }

    async def create_run(self, **kwargs):
        self.created.append(kwargs)
        return {"run_id": "supervisor-1"}

    async def resume_supervision_degraded(self, **kwargs):
        self.legacy_degraded.append(kwargs)
        return self.task


@pytest.mark.asyncio
async def test_turn_handoff_is_pinned_and_consultation_v3(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    repository = TurnRepository()
    processor = RunProcessor(settings, repository, SimpleNamespace())
    processor.goal_supervisor.configuration = lambda _project_id: {"enabled": True}

    args = {
        "project_id": "p", "task_id": "task-1", "turn_number": 1,
        "trigger": {
            "message_id": "user-1", "channel": "discord",
            "chat_id": "pm", "user_id": "u1",
        },
        "reply_message_id": "reply-1", "outcome": "needs_input",
        "summary": "need one answer", "usage": {"input_tokens": 1},
    }
    await processor._queue_turn_evaluation(**args)
    await processor._queue_turn_evaluation(**args)

    target = (
        tmp_path / "vaults" / "p" / "runs" / "task-1" / "supervisor"
        / "turn-handoffs" / "turn-1.json"
    )
    handoff = json.loads(target.read_text(encoding="utf-8"))
    assert handoff["schema_version"] == "supervisor-turn-handoff.v1"
    assert handoff["source_context_epoch"] == 7
    assert handoff["pm_reply_message_id"] == "reply-1"
    request = repository.created[0]["request"]
    assert request["schema_version"] == "supervisor-consultation.v3"
    assert request["turn_handoff_ref"]["sha256"] == request["turn_handoff_sha256"]
    assert request["interaction_head"]["context_epoch"] == 7

    repository.task["request_json"]["task_phase"] = "working"
    await processor._queue_turn_evaluation(**args)
    # Queueing is deliberately best-effort, but a changed pinned Turn is not
    # overwritten and no third consultation is created.
    assert len(repository.created) == 2
    assert json.loads(target.read_text(encoding="utf-8")) == handoff
    assert repository.legacy_degraded[0]["reason"] == "Supervisor evaluation could not be queued"


class MemoryRecorder:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def append(self, project_id: str, item: dict) -> None:
        self.items.append(item)


class PipelineSupervisor:
    def __init__(self, settings: KernelSettings, *, inbound: bool) -> None:
        self.current = CurrentArtifactService(settings)
        self.memory = MemoryRecorder()
        self.inbound = inbound
        self.intent_calls = 0
        self.main_calls = 0

    def configuration(self, project_id: str) -> dict:
        return {
            "enabled": True, "max_task_turns": 24,
            "daily_input_tokens": 200_000, "daily_output_tokens": 40_000,
        }

    async def build_snapshot(self, **kwargs) -> dict:
        delta = []
        if self.inbound:
            delta = [{
                "message_id": "m1", "direction": "inbound", "text": "继续",
                "created_at": "2026-09-30T00:00:00+00:00",
            }]
        return {
            "meta": {"task_id": kwargs["task_id"]},
            "interaction_delta": delta,
        }

    async def run_intent_panel(self, **kwargs) -> dict:
        self.intent_calls += 1
        return {"schema_version": "supervisor-intent-panel.v1", "hypotheses": []}

    async def run_consultation(self, **kwargs) -> dict:
        self.main_calls += 1
        return {
            "decision": "continue_current_stage", "reason": ["reviewed"],
            "intent_hypothesis": [], "insufficient_evidence": False,
            "next_prompt": {"assembled_prompt": "", "recommended_user_prompt": ""},
            "clarifications": [], "usage": {"input_tokens": 1, "output_tokens": 1},
            "handoff_ref": {"uri": "vault://p/handoff.json", "sha256": "a" * 64},
        }


class PipelineRepository:
    def __init__(self, *, current: bool) -> None:
        self.current = current
        self.finished: list[dict] = []
        self.cards: list[dict] = []

    async def record_process_identity(self, *args):
        return True

    async def project_usage_rollup(self, project_id: str):
        return {"by_actor_role": []}

    async def get_message(self, message_id: str):
        return {
            "message_id": message_id, "channel": "feishu", "chat_id": "sup",
            "user_id": "u1", "content_json": {"text": ""},
        }

    async def run_claim_is_active(self, *args):
        return True

    async def supervisor_context_is_current(self, **kwargs):
        return self.current

    async def queue_supervisor_status(self, **kwargs):
        self.cards.append(kwargs)
        return {"message_id": "card-1"}

    async def finish_run(self, *args, **kwargs):
        self.finished.append(dict(kwargs.get("result") or {}))
        return True


def consultation_run() -> dict:
    return {
        "run_id": "supervisor-run", "project_id": "p", "attempt": 1,
        "trigger_message_id": "trigger", "claim_token": "claim",
        "request_json": {
            "interaction_kind": "supervisor_consultation",
            "schema_version": "supervisor-consultation.v3",
            "task_id": "task-1", "trigger": "after_agent_turn",
            "turn_number": 2, "source_turn_number": 2,
            "source_context_epoch": 9,
            "interaction_head": {"context_epoch": 9},
            "agent_turn_report": {"outcome": "continue"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("inbound, expected_intent_calls", [(False, 0), (True, 1)])
async def test_main_always_runs_but_luna_only_runs_for_user_text(
    tmp_path, inbound, expected_intent_calls,
):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    repository = PipelineRepository(current=True)
    supervisor = PipelineSupervisor(settings, inbound=inbound)
    processor = SupervisorProcessor(settings, repository, supervisor=supervisor)

    result = await processor.process(consultation_run(), "worker")

    assert result["outcome"] == "evaluated"
    assert supervisor.main_calls == 1
    assert supervisor.intent_calls == expected_intent_calls
    assert len(repository.cards) == 1


@pytest.mark.asyncio
async def test_stale_epoch_keeps_audit_decision_but_no_current_or_card(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    repository = PipelineRepository(current=False)
    supervisor = PipelineSupervisor(settings, inbound=True)
    processor = SupervisorProcessor(settings, repository, supervisor=supervisor)

    result = await processor.process(consultation_run(), "worker")

    assert result["outcome"] == "evaluated_stale"
    assert supervisor.main_calls == 1
    assert supervisor.intent_calls == 1
    assert not repository.cards
    assert not supervisor.memory.items
    assert supervisor.current.read("p", "task-1", "prompt") is None
    assert supervisor.current.read("p", "task-1", "supervisor") is None
    decisions = list(
        (tmp_path / "vaults" / "p" / "runs" / "task-1" / "supervisor").glob(
            "decision-*.json"
        )
    )
    assert len(decisions) == 1
