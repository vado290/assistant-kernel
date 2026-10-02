from __future__ import annotations

import uuid

import pytest

from assistant_kernel.repositories.core import CoreRepository
from test_core4 import core_settings, prepare_bindable_project  # noqa: F401


async def _waiting_task(repository: CoreRepository) -> dict:
    created = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager",
        channel="discord", chat_id="pm", user_id="user-1",
        external_event_id=f"task-{uuid.uuid4()}", external_message_id="task",
        content={"text": "prepare one bounded task"},
    )
    task = created["run"]
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.runs
            SET status='waiting_user',
                request_json=request_json || %s::jsonb
            WHERE run_id=%s
            """,
            ('{"supervisor_context_epoch":1,"task_turn_count":1,'
             '"task_protocol":"single-agent.v2"}', task["run_id"]),
        )
        await connection.commit()
    return task


@pytest.mark.asyncio
async def test_context_epoch_retry_is_exact_and_supersedes_open_card(core_settings):
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    task = await _waiting_task(repository)
    card = await repository.queue_supervisor_status(
        project_id="demo", run_id=str(task["run_id"]), channel="discord",
        chat_id="supervisor", user_id="user-1", text="recommendation",
        turn_number=1, decision="needs_user_input", prompt_sha256=None,
        card_revision=1, decision_id="decision-1",
        source_context_epoch=1, source_turn_number=1,
        source_supervisor_run_id="supervisor-run-1",
    )

    epoch = await repository.advance_supervisor_context_epoch(
        str(task["run_id"]), reason="new_supervisor_message", source_id="message-2",
    )
    repeated = await repository.advance_supervisor_context_epoch(
        str(task["run_id"]), reason="new_supervisor_message", source_id="message-2",
    )

    assert epoch == repeated == 2
    assert not await repository.supervisor_context_is_current(str(task["run_id"]), 1)
    assert await repository.supervisor_context_is_current(str(task["run_id"]), 2)
    stored = await repository.get_message(str(card["message_id"]))
    assert stored["content_json"]["action_status"] == "superseded"
    assert stored["content_json"]["superseded_reason"] == "new_supervisor_message"
    assert all(item["disabled"] for item in stored["content_json"]["actions"])


@pytest.mark.asyncio
async def test_dismiss_is_single_terminal_and_legacy_actions_fail_closed(core_settings):
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    task = await _waiting_task(repository)
    card = await repository.queue_supervisor_status(
        project_id="demo", run_id=str(task["run_id"]), channel="discord",
        chat_id="supervisor", user_id="user-1", text="recommendation",
        turn_number=1, decision="continue_current_stage", prompt_sha256=None,
        card_revision=1, decision_id="decision-1",
        source_context_epoch=1, source_turn_number=1,
        source_supervisor_run_id="supervisor-run-1",
    )

    result = await repository.record_supervisor_action(
        card_message_id=str(card["message_id"]), external_event_id="dismiss-1",
        actor_user_id="user-1", action="dismiss",
    )
    assert result["status"] == "dismissed"
    repeated = await repository.record_supervisor_action(
        card_message_id=str(card["message_id"]), external_event_id="dismiss-1",
        actor_user_id="user-1", action="dismiss",
    )
    assert repeated["duplicate"] is True
    with pytest.raises(PermissionError):
        await repository.record_supervisor_action(
            card_message_id=str(card["message_id"]), external_event_id="revise-2",
            actor_user_id="user-1", action="revise",
        )
    with pytest.raises(PermissionError):
        await repository.record_supervisor_action(
            card_message_id=str(card["message_id"]), external_event_id="legacy-3",
            actor_user_id="user-1", action="continue",
        )
    current = await repository.get_run(str(task["run_id"]))
    assert current["status"] == "waiting_user"
    assert current["request_json"]["supervisor_context_epoch"] == 2


@pytest.mark.asyncio
async def test_supervisor_runs_for_one_task_share_conversation_key(core_settings):
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    task = await _waiting_task(repository)
    first_trigger = await repository.create_outbound(
        project_id="demo", session_id=None, run_id=str(task["run_id"]),
        channel="discord", chat_id="pm", user_id="user-1",
        message_type="reply", content={"text": "pm turn"},
        reply_to_message_id=None, idempotency_key="supervisor-trigger-pm",
    )
    second_trigger = await repository.create_outbound(
        project_id="demo", session_id=None, run_id=str(task["run_id"]),
        channel="discord", chat_id="supervisor", user_id="user-1",
        message_type="reply", content={"text": "user consultation"},
        reply_to_message_id=None, idempotency_key="supervisor-trigger-user",
    )
    request = {
        "interaction_kind": "supervisor_consultation",
        "schema_version": "supervisor-consultation.v3",
        "task_id": str(task["run_id"]),
    }
    first = await repository.create_run(
        project_id="demo", agent_key="project:demo:supervisor", run_type="chat",
        trigger_message_id=str(first_trigger["message_id"]), request=request,
        idempotency_key="supervisor-conversation-pm",
    )
    second = await repository.create_run(
        project_id="demo", agent_key="project:demo:supervisor", run_type="chat",
        trigger_message_id=str(second_trigger["message_id"]), request=request,
        idempotency_key="supervisor-conversation-user",
    )
    assert first["conversation_key"] == second["conversation_key"]
    assert str(first["conversation_key"]).startswith("supervisor-task:")


@pytest.mark.asyncio
async def test_terminal_task_supersedes_and_queues_disabled_card_projection(core_settings):
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    task = await _waiting_task(repository)
    task_id = str(task["run_id"])
    card = await repository.queue_supervisor_status(
        project_id="demo", run_id=task_id, channel="discord",
        chat_id="supervisor", user_id="user-1", text="recommendation",
        turn_number=1, decision="needs_user_input", prompt_sha256=None,
        card_revision=1, decision_id="decision-1",
        source_context_epoch=1, source_turn_number=1,
        source_supervisor_run_id="supervisor-run-1",
    )
    worker = str(uuid.uuid4())
    claim = str(uuid.uuid4())
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.messages
            SET delivery_status='sent',external_message_id='external-card-1',sent_at=now()
            WHERE message_id=%s
            """,
            (card["message_id"],),
        )
        await connection.execute(
            """
            UPDATE assistant.runs
            SET status='running',worker_instance_id=%s,claim_token=%s
            WHERE run_id=%s
            """,
            (worker, claim, task_id),
        )
        await connection.commit()
    assert await repository.finish_run(
        task_id, worker, claim, status="succeeded", result={"ok": True},
    )
    stored = await repository.get_message(str(card["message_id"]))
    assert stored["content_json"]["action_status"] == "superseded"
    assert stored["content_json"]["superseded_reason"] == "task_terminal"
    async with repository.database.connection() as connection:
        projection = await (await connection.execute(
            """
            SELECT content_json FROM assistant.messages
            WHERE idempotency_key LIKE 'supervisor-card-disable:%'
            """
        )).fetchone()
    assert projection is not None
    assert projection["content_json"]["update_external_message_id"] == "external-card-1"
    assert all(item["disabled"] for item in projection["content_json"]["actions"])
