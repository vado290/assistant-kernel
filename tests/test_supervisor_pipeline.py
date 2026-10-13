"""Supervisor consultation pipeline tests: gates, contracts, persistence."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.current_artifacts import CurrentArtifactService
from assistant_kernel.services.decision_records import DecisionRecordService
from assistant_kernel.services.goal_supervisor import (
    GoalSupervisorService, SupervisorDecisionContractError,
)
from assistant_kernel.services.interaction_memory import InteractionMemory
from assistant_kernel.services.supervisor_processor import (
    SupervisorProcessor, render_status_card,
)


def _project_yaml(settings: KernelSettings, project_id: str, **supervisor) -> None:
    from assistant_kernel.storage import ProjectRuntimeManager
    root = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    root.mkdir(parents=True, exist_ok=True)
    payload = {"task_protocol": "single-agent.v2"}
    if supervisor:
        payload["supervisor"] = supervisor
    (root / "project.yaml").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _sample_decision() -> dict:
    return {
        "decision": "continue_current_stage",
        "reason": ["build turn settled", "no gate change"],
        "evidence_refs": ["message:m1"],
        "missing_evidence": [],
        "progress_made": True,
        "intent_hypothesis": [
            {
                "statement": "定义第一套正式策略及其交易场所",
                "confidence": "medium",
                "evidence_refs": ["message:m1"],
            }
        ],
        "insufficient_evidence": False,
        "next_prompt": {
            "assembled_prompt": "继续Task。请先创建子agent精读x_strategy.py与order_planner.py对比市场语义差异，然后向我确认唯一场所。",
            "recommended_user_prompt": "请继续Task。请先让子agent精读x_strategy.py与order_planner.py，对比市场语义差异后，向我确认唯一交易场所。",
        },
        "clarifications": [],
        "user_message": "意图已更新：优先确定交易场所。",
        "fetch_requests": [],
    }


class FakeSupervisor:
    """Test double replacing every model-touching surface."""

    def __init__(self, decision: dict | None = None, *, settings=None):
        self.decision = decision or _sample_decision()
        self.current = CurrentArtifactService(settings)
        self.memory = InteractionMemory(settings)
        self.settings = settings

    def configuration(self, project_id: str) -> dict:
        return {
            "enabled": True, "model": "gpt-5.6-sol",
            "reasoning_tiers": {"default": "medium", "critical": "high"},
            "max_task_turns": 24,
            "daily_input_tokens": 200_000,
            "daily_output_tokens": 40_000,
            "business_state_sources": [],
        }

    async def build_snapshot(self, **kwargs) -> dict:
        return {
            "meta": {"task_id": kwargs.get("task_id"), "trigger": kwargs.get("trigger")},
            "interaction_delta": kwargs.get("current_message") and [
                {"message_id": "m1", "direction": "inbound", "text": "继续任务",
                 "created_at": datetime.now(timezone.utc).isoformat()}
            ] or [],
            "task_state": {"state": {"next_action": "等待场所确认"}},
            "memory_hits": [],
            "decisions": [],
        }

    async def run_intent_panel(self, **kwargs) -> dict:
        return {"schema_version": "supervisor-intent-panel.v1", "hypotheses": []}

    async def run_consultation(self, **kwargs) -> dict:
        brief = GoalSupervisorService(self.settings, SimpleNamespace())._supervisor_brief(
            project_id=kwargs["project_id"], task_id=kwargs["task_id"],
            snapshot=kwargs["snapshot"], intent_panel=kwargs.get("intent_panel"), decision=self.decision,
        )
        return {
            **self.decision,
            "project_assessment": brief["project_assessment"],
            "user_intent_context": brief["user_intent_context"],
            "handoff_ref": {
                "uri": "vault://p/runs/t/supervisor/handoff-x.json",
                "sha256": "a" * 64, "size": 10,
            },
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }


class FakeRepository:
    def __init__(self):
        self.finished: list[tuple] = []
        self.cards: list[dict] = []
        self.supervisor_input_tokens = 0
        self.supervisor_output_tokens = 0
        self.degraded: list[dict] = []
        self.paused: list[dict] = []

    async def pause_supervision(self, **kwargs):
        self.paused.append(kwargs)
        return {"run_id": kwargs["task_id"], "status": "waiting_supervisor"}

    async def project_usage_rollup(self, project_id: str) -> dict:
        return {
            "window": "24h",
            "by_actor_role": [{
                "actor_role": "goal_supervisor",
                "input_tokens": self.supervisor_input_tokens,
                "output_tokens": self.supervisor_output_tokens,
            }],
        }

    async def supervisor_context_is_current(self, **kwargs) -> bool:
        return True

    async def run_claim_is_active(self, *args, **kwargs) -> bool:
        return True

    async def get_message(self, message_id: str) -> dict | None:
        return {
            "message_id": message_id, "channel": "discord",
            "chat_id": "sup-channel", "user_id": "u1",
            "content_json": {"text": "你好supervisor"},
        }

    async def queue_supervisor_status(self, **kwargs) -> dict:
        self.cards.append(kwargs)
        return {"message_id": str(uuid.uuid4())}

    async def finish_run(self, run_id, worker, token, *, status, result=None,
                         error=None, artifact_refs=None) -> bool:
        self.finished.append((run_id, status, result))
        return True

    async def resume_supervision_degraded(self, **kwargs):
        self.degraded.append(kwargs)
        return {"run_id": kwargs["task_id"], "status": "queued"}


def _consultation_run(project_id="p", task_id="t1", trigger="after_agent_turn",
                      turn=3) -> dict:
    return {
        "run_id": str(uuid.uuid4()), "project_id": project_id,
        "run_type": "chat", "agent_key": f"project:{project_id}:supervisor",
        "trigger_message_id": str(uuid.uuid4()),
        "created_at": "2026-09-29T00:00:00+00:00",
        "request_json": {
            "interaction_kind": "supervisor_consultation",
            "task_id": task_id, "trigger": trigger, "turn_number": turn,
            "agent_turn_report": {"outcome": "needs_input"},
        },
        "claim_token": "claim-1",
    }


def test_parse_decision_v2_accepts_contract_and_rejects_unknown_keys(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    service = GoalSupervisorService(settings, SimpleNamespace())
    parsed = service.parse_decision_v2(json.dumps(_sample_decision()))
    assert parsed["decision"] == "continue_current_stage"
    assert len(parsed["intent_hypothesis"]) == 1
    assert parsed["next_prompt"]["recommended_user_prompt"]

    overloaded = _sample_decision()
    overloaded["artifact_updates"] = {"requirements": {"scope": []}}
    with pytest.raises(ValueError, match="unknown keys"):
        service.parse_decision_v2(json.dumps(overloaded))

    chatty = _sample_decision()
    chatty["next_prompt"]["assembled_prompt"] = "x" * 401
    with pytest.raises(ValueError, match="400"):
        service.parse_decision_v2(json.dumps(chatty))


def test_parse_decision_v2_bounds_hypotheses_and_clarifications(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    service = GoalSupervisorService(settings, SimpleNamespace())
    payload = _sample_decision()
    payload["intent_hypothesis"] = [
        {"statement": f"h{index}", "confidence": "low"} for index in range(4)
    ]
    with pytest.raises(ValueError, match="three"):
        service.parse_decision_v2(json.dumps(payload))
    payload["intent_hypothesis"] = []
    payload["clarifications"] = [
        {"question_id": "a", "question_text": "one"},
        {"question_id": "b", "question_text": "two"},
    ]
    assert len(service.parse_decision_v2(json.dumps(payload))["clarifications"]) == 2
    payload["clarifications"] = [
        {"question_id": str(index), "question_text": f"Question {index}"} for index in range(11)
    ]
    with pytest.raises(ValueError, match="ten questions"):
        service.parse_decision_v2(json.dumps(payload))


def test_parse_decision_v2_normalizes_bounded_model_aliases(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    service = GoalSupervisorService(settings, SimpleNamespace())
    payload = _sample_decision()
    payload["intent_hypothesis"] = [{
        "hypothesis": "用户要继续当前Task", "confidence": 0.94,
    }]
    parsed = service.parse_decision_v2(json.dumps(payload))
    assert parsed["intent_hypothesis"] == [{
        "statement": "用户要继续当前Task",
        "confidence": "high",
        "evidence_refs": [],
    }]
    payload["clarifications"] = ["请提供目标市场和合约类型。"]
    parsed = service.parse_decision_v2(json.dumps(payload))
    assert parsed["clarifications"][0]["question_id"].startswith("supq-")
    assert parsed["clarifications"][0]["question_text"] == (
        "请提供目标市场和合约类型。"
    )


def test_interaction_memory_status_lifecycle_and_retrieval(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    memory = InteractionMemory(settings)
    memory.append("demo", {
        "task_id": "old-task", "status": "proposed", "created_by": "supervisor",
        "user_text_excerpt": "确定第一套策略的交易场所",
        "intent_statement": "以xStrategy为蓝本定义策略",
        "tags": ["策略", "场所"],
    })
    confirmed = memory.append("demo", {
        "task_id": "old-task", "status": "user_confirmed", "created_by": "kernel",
        "user_text_excerpt": "就用Binance合约",
        "intent_statement": "目标场所是Binance合约",
        "tags": ["场所"],
    })
    assert confirmed["status"] == "user_confirmed"
    hits = memory.retrieve("demo", ["策略 场所"], limit=5, exclude_task_id="new-task")
    assert 1 <= len(hits) <= 5
    assert all(hit.get("status") in {"proposed", "user_confirmed"} for hit in hits)
    with pytest.raises(ValueError):
        memory.append("demo", {"status": "final-truth"})


def test_decision_records_append_and_recent(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    records = DecisionRecordService(settings)
    first = records.append(
        "demo", "t1", kind="prompt_adoption",
        text="采用supervisor推荐的prompt",
        source_message_ids=["m1"],
    )
    second = records.append(
        "demo", "t1", kind="approval", text="批准Requirements",
        source_message_ids=["m2"],
    )
    recent = records.recent("demo", "t1", limit=10)
    assert [item["id"] for item in recent] == [first["id"], second["id"]]
    with pytest.raises(ValueError):
        records.append("demo", "t1", kind="rumor", text="x")


def test_render_status_card_includes_prompt_and_reply_hint():
    decision = _sample_decision()
    card = render_status_card(task_id="t1", trigger="after_agent_turn",
                              turn_number=3, decision=decision)
    assert "意图假设" in card
    assert "```text" in card
    assert "回复这条消息" in card
    assert len(card) <= 1900


def test_render_status_card_handles_cold_start():
    decision = _sample_decision()
    decision["insufficient_evidence"] = True
    decision["intent_hypothesis"] = []
    card = render_status_card(task_id="t1", trigger="after_agent_turn",
                              turn_number=1, decision=decision)
    assert "还在理解你的目标" in card


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "with_intent", "expected_model", "expected_effort"),
    [
        ("after_agent_turn", True, "gpt-5.6-sol", "medium"),
        ("kernel_incident", True, "gpt-5.6-sol", "high"),
        ("user_consultation", False, "gpt-5.6-sol", "medium"),
    ],
)
async def test_real_run_consultation_reaches_main_evaluation(
    tmp_path, trigger, with_intent, expected_model, expected_effort,
):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    _project_yaml(settings, "demo", enabled=True, model="gpt-5.6-sol")
    service = GoalSupervisorService(settings, SimpleNamespace())
    calls = []

    async def fake_ephemeral(**kwargs):
        calls.append(kwargs)
        return {
            "text": json.dumps(_sample_decision()),
            "provider": "openai", "model": kwargs["model"],
            "request_id": "native-1",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "trace_uri": None, "trace_sha256": None,
        }

    service._codex_ephemeral = fake_ephemeral
    result = await service.run_consultation(
        project_id="demo", task_id="task-1",
        snapshot={
            "schema_version": "supervisor-snapshot.v1",
            "meta": {
                "task_id": "task-1", "trigger": trigger,
                "source": {"source_actor_type": "pm"},
            },
            "task_state": {"state": {"next_action": "确认手工订单入口"}},
            "interaction_delta": [{
                "message_id": "m-new", "direction": "inbound",
                "text": "我要做账户级手工订单风控，不是交易策略",
            }],
            "agent_turn_report": {
                "outcome": "needs_input",
                "summary": "PM已确认新目标是账户级手工订单风控",
            },
            "semantic_context": {"selected": [{
                "component_id": "trading.risk.manager", "score": 100,
            }]},
        },
        intent_panel=(
            {
                "schema_version": "supervisor-intent-panel.v1",
                "hypotheses": [{
                    "statement": "目标是账户级手工订单风控",
                    "confidence": "high",
                    "evidence_refs": ["message:m-new"],
                }],
                "notes": "旧策略目标已过期",
            }
            if with_intent else None
        ),
        artifact_key=f"run-{trigger}-a1",
    )
    assert result["decision"] == "continue_current_stage"
    assert calls[0]["model"] == expected_model
    assert calls[0]["reasoning_effort"] == expected_effort
    assert "# Skill" in calls[0]["prompt"]
    assert "supervisor-brief.v1" in calls[0]["prompt"]
    assert "账户级手工订单风控" in calls[0]["prompt"]
    assert result["handoff_ref"]["uri"].endswith(
        f"handoff-run-{trigger}-a1.json"
    )
    brief = (
        settings.data_root / "vaults" / "demo" / "runs" / "task-1" /
        "supervisor" / f"brief-run-{trigger}-a1.json"
    )
    assert brief.is_file()


@pytest.mark.asyncio
async def test_supervisor_fetches_large_jsonl_as_bounded_tail(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    _project_yaml(settings, "demo", enabled=True)
    service = GoalSupervisorService(settings, SimpleNamespace())
    target = settings.data_root / "vaults/demo/runs/trace.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x" * 120_000 + "FINAL_EVENT", encoding="utf-8")
    result = await service._resolve_fetch_requests("demo", [{
        "kind": "file", "uri": "vault://demo/runs/trace.jsonl",
    }])
    assert result[0]["truncated"] is True
    assert result[0]["returned_bytes"] == 40_000
    assert "FINAL_EVENT" in result[0]["text"]


@pytest.mark.asyncio
async def test_supervisor_processor_evaluates_each_queued_turn(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    repository = FakeRepository()
    processor = SupervisorProcessor(
        settings, repository, supervisor=FakeSupervisor(settings=settings)
    )
    run = _consultation_run()
    _project_yaml(settings, "p", enabled=True)
    result = await processor.process(run, "worker-1")
    assert result["outcome"] == "evaluated"
    assert repository.finished[0][1] == "succeeded"


@pytest.mark.asyncio
async def test_supervisor_invalid_contract_finishes_degraded_without_retry(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    _project_yaml(settings, "p", enabled=True)

    class InvalidSupervisor(FakeSupervisor):
        async def run_consultation(self, **kwargs):
            raise SupervisorDecisionContractError(
                "unsupported decision", invalid_decision="invented",
                trace_uri="vault://p/invalid.jsonl", trace_sha256="f" * 64,
            )

    repository = FakeRepository()
    processor = SupervisorProcessor(
        settings, repository, supervisor=InvalidSupervisor(settings=settings),
    )
    result = await processor.process(_consultation_run(), "worker-1")
    assert result["outcome"] == "degraded_invalid_decision"
    assert repository.paused[0]["task_id"] == "t1"
    assert repository.degraded == []
    assert result["task_resumed"] is False
    assert repository.finished[0][1] == "succeeded"
    assert repository.cards[0]["confirmation_class"] == "invalid"
    assert repository.cards[0]["decision"] == "degraded_invalid_decision"


@pytest.mark.asyncio
async def test_supervisor_processor_skips_over_budget(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    repository = FakeRepository()
    repository.supervisor_input_tokens = 200_000
    processor = SupervisorProcessor(
        settings, repository, supervisor=FakeSupervisor(settings=settings)
    )
    _project_yaml(settings, "p", enabled=True)
    result = await processor.process(_consultation_run(), "worker-1")
    assert result["outcome"] == "skipped_due_to_budget"
    assert repository.paused[0]["reason"] == "Supervisor budget gate: daily_input_tokens"
    assert repository.degraded == []


@pytest.mark.asyncio
async def test_supervisor_processor_full_path_persists_decision_and_action_plan(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    _project_yaml(settings, "p", enabled=True)
    repository = FakeRepository()
    supervisor = FakeSupervisor(settings=settings)
    # Wire the real CurrentArtifactService into the fake for persistence.
    from assistant_kernel.services.current_artifacts import CurrentArtifactService
    artifacts = CurrentArtifactService(settings)
    supervisor.current = artifacts
    supervisor.memory = InteractionMemory(settings)
    processor = SupervisorProcessor(settings, repository, supervisor=supervisor)
    result = await processor.process(_consultation_run(), "worker-1")
    assert result["outcome"] == "evaluated"
    assert result["decision"] == "continue_current_stage"

    run_id, status, payload = repository.finished[0]
    assert status == "succeeded"
    assert payload["decision_ref"].startswith("vault://p/runs/t1/supervisor/")

    current = artifacts.read("p", "t1", "supervisor")
    assert current is not None
    state = current["payload"]
    assert state["status"] == "ready"
    assert state["card_revision"] == 1
    assert state["last_decision"]["decision"] == "continue_current_stage"
    assert state["watermark"]["ts"]

    assert artifacts.read("p", "t1", "prompt") is None
    assert result["action_plan_ref"]["document_schema"] == (
        "supervisor-action-plan.v1"
    )

    card = repository.cards[0]
    assert card["channel"] == "discord"
    assert "```text" in card["text"]

    hits = InteractionMemory(settings).retrieve("p", ["策略 场所"])
    assert hits == []
    intent = state["user_intent_context"]
    assert intent["scope"] == "project_only"
    hypothesis = intent["hypotheses"][0]
    assert datetime.fromisoformat(hypothesis["expires_at"]) - datetime.fromisoformat(hypothesis["observed_at"]) == timedelta(days=7)


@pytest.mark.asyncio
async def test_supervisor_processor_evaluates_when_no_new_info_after_continue(tmp_path):
    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path}
    )
    _project_yaml(settings, "p", enabled=True)
    repository = FakeRepository()
    supervisor = FakeSupervisor(settings=settings)
    from assistant_kernel.services.current_artifacts import CurrentArtifactService
    supervisor.current = CurrentArtifactService(settings)
    supervisor.memory = InteractionMemory(settings)
    async def _snapshot(**kwargs):
        return {
            "meta": {}, "interaction_delta": [], "task_state": {},
            "memory_hits": [], "decisions": [],
        }
    supervisor.build_snapshot = _snapshot
    processor = SupervisorProcessor(settings, repository, supervisor=supervisor)
    run = _consultation_run(trigger="after_agent_turn")
    run["request_json"]["agent_turn_report"] = {"outcome": "continue"}
    artifacts = supervisor.current
    artifacts.write("p", "t1", "supervisor", {
        "schema_version": "supervisor-current.v1",
        "status": "ready", "last_agent_outcome": "continue",
    })
    result = await processor.process(run, "worker-1")
    assert result["outcome"] == "evaluated"
    assert repository.finished[0][2]["outcome"] == "evaluated"


@pytest.mark.asyncio
async def test_worker_dispatches_supervisor_consultation_branch(tmp_path):
    """The claim loop routes interaction_kind=supervisor_consultation correctly."""
    from assistant_kernel.worker import main as worker_main

    settings = KernelSettings.from_environment().model_copy(
        update={"data_root": tmp_path, "worker_claim_loops": 1}
    )
    _project_yaml(settings, "p", enabled=True)

    processed: dict = {}

    class RecordingProcessor:
        def __init__(self, *args):
            pass

        async def process(self, run, worker_instance_id, cancel_event=None):
            processed["interaction_kind"] = (
                run.get("request_json") or {}
            ).get("interaction_kind")
            raise asyncio.CancelledError

    import asyncio as _asyncio

    class ClaimOnce:
        """Return one supervisor run, then stop the loop via shutdown."""
        def __init__(self):
            self.called = False

        async def __call__(self, *args, **kwargs):
            if self.called:
                return None
            self.called = True
            return _consultation_run(project_id="p", task_id="t-dispatch")

    monkey_targets = {
        "CoreRepository": lambda settings: SimpleNamespace(
            database=SimpleNamespace(ping=AsyncTrue()),
            claim_run=ClaimOnce(),
            finish_cancelled_run=AsyncNone(),
        ),
        "ProjectRuntimeManager": lambda data_root: SimpleNamespace(),
        "CodexCliHarness": lambda runtime: SimpleNamespace(),
        "RunProcessor": lambda *args: RecordingProcessor(args),
        "SupervisorProcessor": lambda *args: RecordingProcessor(args),
    }
    saved = {name: getattr(worker_main, name) for name in monkey_targets}
    for name, replacement in monkey_targets.items():
        setattr(worker_main, name, replacement)
    shutdown = _asyncio.Event()
    try:
        task = _asyncio.create_task(
            worker_main.run_worker_loop(settings, shutdown)
        )
        for _ in range(100):
            if "interaction_kind" in processed:
                break
            await _asyncio.sleep(0.05)
        shutdown.set()
        import contextlib
        with contextlib.suppress(_asyncio.CancelledError):
            await _asyncio.wait_for(task, timeout=5)
    finally:
        for name, original in saved.items():
            setattr(worker_main, name, original)
    assert processed.get("interaction_kind") == "supervisor_consultation"


class AsyncTrue:
    async def __call__(self, *args, **kwargs):
        return True


class AsyncNone:
    async def __call__(self, *args, **kwargs):
        return None


