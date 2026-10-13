"""Five-stage supervision gates against disposable PostgreSQL databases."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import yaml

from test_core4 import core_settings, prepare_agent
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.discord import build_discord_view
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.storage import ProjectRuntimeManager


async def root_task(settings):
    prepare_agent(settings)
    vault = ProjectRuntimeManager(settings.data_root).describe("demo").vault_root
    (vault / "project.yaml").write_text(yaml.safe_dump({
        "task_protocol": "single-agent.v2", "supervisor": {"enabled": True},
        "work_modes": {"design": {"profile": "design"}},
    }), encoding="utf-8")
    repository = CoreRepository(settings)
    incoming = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="200", user_id="300", external_event_id="root:start",
        external_message_id="root-start", content={"text": "Confirm the project requirements"},
    )
    worker = str(uuid.uuid4())
    task = await repository.claim_run("test", worker, 60)
    task = await repository.patch_claimed_run_request(
        run_id=str(task["run_id"]), worker_instance_id=worker, claim_token=str(task["claim_token"]),
        request_patch={"task_protocol": "single-agent.v2", "current_stage": "requirements",
                       "work_mode": "requirements", "task_turn_count": 1, "state_revision": 1},
    )
    return repository, task, worker, incoming["message"]


async def question(repository, owner, worker, source, *, chat_id="200"):
    session = await repository.ensure_session("demo", owner["agent_key"], "discord", chat_id)
    return await repository.finalize_agent_waiting(
        run_id=str(owner["run_id"]), worker_instance_id=worker, claim_token=str(owner["claim_token"]),
        session_id=str(session["session_id"]), native_session_id="test-native-question",
        input_tokens=0, context_window_tokens=1000, archive_uri=None, archive_sha256=None,
        project_id="demo", channel="discord", chat_id=chat_id, user_id="300",
        source_message_id=str(source["message_id"]), question={
            "summary": "Clarify scope", "questions": [{"question_id": "scope",
                "question_text": "What is the approved project scope?", "required": True}],
        },
    )


async def answer(repository, card, action, *, value=None, event=None):
    state = card["content_json"]["questionnaire"]
    return await repository.record_questionnaire_action(
        message_id=str(card["message_id"]), project_id="demo", channel="discord",
        chat_id=str(card["chat_id"]), user_id="300", external_event_id=event or uuid.uuid4().hex,
        revision=state["revision"], page=state["cursor"], action=action, value=value,
    )


async def submit(repository, card):
    card = (await answer(repository, card, "text", value="Implement only the confirmed scope"))["message"]
    card = (await answer(repository, card, "next"))["message"]
    return await answer(repository, card, "submit", event="answer:final")


async def supervisor_run(repository, task, source):
    current = await repository.get_run(str(task["run_id"]))
    epoch = int(current["request_json"].get("supervisor_context_epoch") or 0)
    return await repository.create_run(
        project_id="demo", agent_key="project:demo:supervisor", run_type="chat",
        trigger_message_id=str(source["message_id"]),
        request={"interaction_kind": "supervisor_consultation", "task_id": str(task["run_id"]),
                 "turn_number": 1, "source_turn_number": 1, "source_context_epoch": epoch,
                 "trigger": "after_agent_turn", "channel": "discord", "chat_id": "555", "user_id": "300"},
        idempotency_key="supervisor:test",
    )


async def paused_task(settings):
    repository, task, worker, source = await root_task(settings)
    await repository.finish_run(str(task["run_id"]), worker, str(task["claim_token"]),
                                 status="waiting_supervisor", result={"outcome": "continue"})
    supervisor = await supervisor_run(repository, task, source)
    pause = await repository.pause_supervision(task_id=str(task["run_id"]), turn_number=1,
                                               reason="temporary supervisor failure",
                                               retry_run_id=str(supervisor["run_id"]))
    return repository, task, supervisor, pause["card"], source


async def test_final_questionnaire_answer_waits_for_supervisor_and_stays_shared(core_settings):
    repository, task, worker, source = await root_task(core_settings)
    card = await question(repository, task, worker, source)
    result = await submit(repository, card)
    updated = await repository.get_run(str(task["run_id"]))
    assert result["resumed"] is True
    assert updated["status"] == "waiting_supervisor"
    assert updated["request_json"]["answer"]["answers"]["scope"] == "Implement only the confirmed scope"
    evaluations = [item for item in await repository.list_runs("demo")
                   if (item["request_json"] or {}).get("interaction_kind") == "supervisor_consultation"]
    assert len(evaluations) == 1 and evaluations[0]["status"] == "queued"
    assert evaluations[0]["request_json"]["task_id"] == str(task["run_id"])
    pending = await repository.list_pending_task_inputs(str(task["run_id"]))
    assert any(item["content_json"].get("schema_version") == "questionnaire-answer.v1" for item in pending)
    claimed = await repository.claim_run("test", str(uuid.uuid4()), 60)
    assert str(claimed["run_id"]) == str(evaluations[0]["run_id"])


async def test_supervisor_owned_answer_resumes_owner_and_rebases_root_epoch(core_settings):
    repository, task, supervisor, recovery, source = await paused_task(core_settings)
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    assert str(claimed["run_id"]) == str(supervisor["run_id"])
    prior_epoch = claimed["request_json"]["source_context_epoch"]
    card = await question(repository, claimed, worker, source, chat_id="555")
    async with repository.database.connection() as connection:
        await connection.execute("UPDATE assistant.runs SET attempt=4,max_attempts=4 WHERE run_id=%s", (supervisor["run_id"],))
        await connection.commit()
    await repository.bind_supervisor_interaction(str(task["run_id"]), str(supervisor["run_id"]), str(card["message_id"]))
    await submit(repository, card)
    root = await repository.get_run(str(task["run_id"]))
    owner = await repository.get_run(str(supervisor["run_id"]))
    assert root["status"] == "waiting_supervisor"
    assert owner["status"] == "queued"
    assert owner["request_json"]["source_context_epoch"] > prior_epoch
    assert owner["request_json"]["source_context_epoch"] == root["request_json"]["supervisor_context_epoch"]
    assert owner["request_json"]["trigger"] == "user_consultation"
    assert owner["attempt"] == 4 and owner["max_attempts"] == 8
    assert len([item for item in await repository.list_runs("demo")
                if item["agent_key"] == "project:demo:supervisor"]) == 1


async def test_deferred_supervisor_questions_keep_original_business_card_and_draft(core_settings):
    repository, task, worker, source = await root_task(core_settings)
    card = await question(repository, task, worker, source)
    draft = (await answer(repository, card, "text", value="My original answer"))["message"]
    supervisor = await supervisor_run(repository, task, source)
    await repository.defer_supervisor_questions(str(task["run_id"]), str(supervisor["run_id"]),
        [{"question_id": "deploy", "question_text": "Which deployment?"}], decision_ref={"uri": "vault://demo/decision.json"})
    root = await repository.get_run(str(task["run_id"]))
    current_card = await repository.get_message(str(card["message_id"]))
    assert str(root["waiting_question_message_id"]) == str(card["message_id"])
    assert root["status"] == "waiting_user"
    assert root["request_json"]["pending_supervisor_questions"][0]["question_id"] == "deploy"
    assert current_card["content_json"]["questionnaire"] == draft["content_json"]["questionnaire"]


async def test_recovery_retry_only_queues_supervisor_and_is_idempotent(core_settings):
    repository, task, supervisor, card, source = await paused_task(core_settings)
    result = await repository.record_supervision_recovery(card_message_id=str(card["message_id"]),
        external_event_id="recovery:retry", actor_user_id="300", action="retry")
    duplicate = await repository.record_supervision_recovery(card_message_id=str(card["message_id"]),
        external_event_id="recovery:retry", actor_user_id="300", action="retry")
    assert result["action"] == "retry" and duplicate["duplicate"] is True
    assert (await repository.get_run(str(task["run_id"]))) ["status"] == "waiting_supervisor"
    assert (await repository.get_run(str(supervisor["run_id"]))) ["status"] == "queued"
    claimed = await repository.claim_run("test", str(uuid.uuid4()), 60)
    assert str(claimed["run_id"]) == str(supervisor["run_id"])


async def test_cancel_recovery_invalidates_old_supervisor_plan(core_settings):
    repository, task, supervisor, card, source = await paused_task(core_settings)
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    epoch = claimed["request_json"]["source_context_epoch"]
    action_ref = TaskDocumentService(core_settings).write_revision(
        project_id="demo", task_id=str(task["run_id"]), document_kind="supervisor-action-plan",
        document_schema="supervisor-action-plan.v1", payload={"plan_id": "old-plan"},
        status="proposed", actor_type="supervisor", actor_id="project:demo:supervisor",
        run_id=str(supervisor["run_id"]), reason="test cancelled plan", source_refs=[],
    )
    await repository.record_supervision_recovery(card_message_id=str(card["message_id"]),
        external_event_id="recovery:cancel", actor_user_id="300", action="cancel")
    assert not await repository.supervisor_context_is_current(str(task["run_id"]), epoch)
    with pytest.raises(PermissionError):
        await repository.commit_supervisor_action(supervisor_run_id=str(supervisor["run_id"]),
            worker_instance_id=worker, claim_token=str(claimed["claim_token"]), task_id=str(task["run_id"]),
            source_context_epoch=epoch, source_turn_number=1, action_ref=action_ref,
            confirmation_class="safe", pm_instruction="continue", target_work_mode="requirements",
            delegations=[], result={"outcome": "evaluated"}, artifact_refs=[])
    assert (await repository.get_run(str(task["run_id"]))) ["status"] == "cancelled"


async def test_restart_inventory_contains_persistent_recovery_components(core_settings):
    repository, task, supervisor, card, source = await paused_task(core_settings)
    async with repository.database.connection() as connection:
        await connection.execute("UPDATE assistant.messages SET delivery_status='sent',external_message_id='123456' WHERE message_id=%s",
                                 (card["message_id"],))
        await connection.commit()
    restarted = CoreRepository(core_settings)
    inventory = await restarted.list_active_discord_component_messages()
    restored = next(item for item in inventory if str(item["message_id"]) == str(card["message_id"]))
    view = build_discord_view(restored, SimpleNamespace(handle_kernel_component=lambda *args: None))
    assert view.is_persistent()
    assert {item.custom_id.rsplit(":", 1)[1] for item in view.children} == {"retry", "wait", "cancel"}


async def test_explicit_no_task_consultation_can_queue_new_requirement_task(core_settings):
    from test_five_stage_supervision import Supervisor
    from assistant_kernel.services.supervisor_processor import SupervisorProcessor

    repository, old_task, old_worker, old_source = await root_task(core_settings)
    await repository.finish_run(str(old_task["run_id"]), old_worker, str(old_task["claim_token"]),
                                 status="succeeded", result={"outcome": "completed"})
    incoming = await repository.ingest_message(project_id="demo", agent_key="project:demo:supervisor",
        channel="discord", chat_id="555", user_id="300", external_event_id="consult:implement",
        external_message_id="consult-implement", content={"text": "请创建一个新任务并开始分析实现方案。"})
    source_id = str(incoming["message"]["message_id"])
    consultation = await repository.create_run(project_id="demo", agent_key="project:demo:supervisor",
        run_type="chat", trigger_message_id=source_id, request={"interaction_kind": "supervisor_consultation",
            "task_id": None, "consultation_id": "consult-new-work", "trigger": "user_consultation",
            "source_message_id": source_id, "channel": "discord", "chat_id": "555", "user_id": "300"},
        idempotency_key="consult:new-work")

    class RequestedWork(Supervisor):
        async def run_consultation(self, **kwargs):
            result = await super().run_consultation(**kwargs)
            result["proposed_actions"] = [{"kind": "queue_task", "goal": "User-requested new implementation",
                                            "source_message_id": source_id, "priority": "normal"}]
            return result

    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    assert str(claimed["run_id"]) == str(consultation["run_id"])
    result = await SupervisorProcessor(core_settings, repository, RequestedWork(core_settings)).process(claimed, worker)
    assert result["task_action_results"][0]["status"] == "accepted"
    tasks = [item for item in await repository.list_runs("demo")
             if item["agent_key"] == "project:demo:manager" and str(item["run_id"]) != str(old_task["run_id"])]
    assert len(tasks) == 1
    request = tasks[0]["request_json"]
    assert request["current_stage"] == "requirements"
    assert not request.get("requirements_approval") and not request.get("design_approval")
    assert (await repository.get_run(str(consultation["run_id"]))) ["result_json"]["task_action_results"]


@pytest.mark.parametrize("preempt", [False, True])
async def test_user_routing_actions_are_safe_and_urgent_preemption_preserves_old_task(core_settings, preempt):
    from test_five_stage_supervision import Supervisor
    from assistant_kernel.services.supervisor_processor import SupervisorProcessor

    repository, task, supervisor, recovery, source = await paused_task(core_settings)
    incoming = await repository.ingest_message(project_id="demo", agent_key="project:demo:supervisor",
        channel="discord", chat_id="555", user_id="300", external_event_id="user:routing",
        external_message_id="routing", content={"text": "请把新的需求排队；严重事件先挂起原任务。"})
    user_id = str(incoming["message"]["message_id"])
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    root = await repository.get_run(str(task["run_id"]))
    claimed = await repository.patch_claimed_run_request(run_id=str(claimed["run_id"]),
        worker_instance_id=worker, claim_token=str(claimed["claim_token"]),
        request_patch={"trigger": "user_consultation", "source_message_id": user_id,
                       "source_context_epoch": root["request_json"]["supervisor_context_epoch"]})

    class Routing(Supervisor):
        async def run_consultation(self, **kwargs):
            result = await super().run_consultation(**kwargs)
            result["proposed_actions"] = [{"kind": "queue_task", "goal": "New user requested goal",
                "priority": "urgent" if preempt else "normal", "source_message_id": user_id}]
            if preempt:
                result["proposed_actions"].append({"kind": "suspend_task", "target_task_id": str(task["run_id"]),
                                                   "source_message_id": user_id, "reason": "Confirmed urgent incident"})
            return result

    result = await SupervisorProcessor(core_settings, repository, Routing(core_settings)).process(claimed, worker)
    assert result["confirmation_class"] == "safe"
    assert result["task_action_results"][0]["status"] == "accepted"
    old = await repository.get_run(str(task["run_id"]))
    pm_tasks = [item for item in await repository.list_runs("demo") if item["agent_key"] == "project:demo:manager"]
    if preempt:
        assert old["request_json"]["task_activation"] == "suspended" and old["status"] == "blocked"
        assert len(pm_tasks) == 2
        new = next(item for item in pm_tasks if str(item["run_id"]) != str(task["run_id"]))
        assert new["request_json"]["current_stage"] == "requirements"
        assert not new["request_json"].get("requirements_approval") and not new["request_json"].get("design_approval")
    else:
        assert len(pm_tasks) == 1 and old["request_json"].get("task_activation", "active") == "active"


async def test_maintenance_activates_work_after_completed_task_enqueue_gap(core_settings):
    from assistant_kernel.services.task_queue import ProjectTaskQueue
    from assistant_kernel.worker.main import _reconcile_project_changes

    repository, task, worker, source = await root_task(core_settings)
    await ProjectTaskQueue(core_settings, repository).queue("demo", "Queued next goal", str(source["message_id"]))
    await repository.finish_run(str(task["run_id"]), worker, str(task["claim_token"]),
                                 status="succeeded", result={"outcome": "completed"})
    await _reconcile_project_changes(core_settings, repository)
    tasks = [item for item in await repository.list_runs("demo") if item["agent_key"] == "project:demo:manager"]
    assert len(tasks) == 2
    new = next(item for item in tasks if str(item["run_id"]) != str(task["run_id"]))
    assert new["request_json"]["current_stage"] == "requirements"
    assert not new["request_json"].get("requirements_approval")
