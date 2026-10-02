from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant_kernel.services.discord import (
    _start_supervisor_consultation,
    build_discord_view,
)
@pytest.mark.asyncio
async def test_supervisor_card_view_supports_three_actions_and_disabled_projection() -> None:
    class Client:
        async def handle_kernel_component(self, *args):
            return None

    message = {
        "message_id": "card-1",
        "message_type": "kernel_notice",
        "content_json": {
            "interaction_kind": "supervisor_status",
            "action_status": "open",
            "actions": [
                {"id": "adopt", "label": "采纳计划", "style": "success"},
                {"id": "dismiss", "label": "取消计划", "style": "secondary"},
                {"id": "revise", "label": "需要修改", "style": "primary"},
            ],
        },
    }
    view = build_discord_view(message, Client())
    assert [item.custom_id.rsplit(":", 1)[-1] for item in view.children] == [
        "adopt", "dismiss", "revise",
    ]
    assert not any(item.disabled for item in view.children)

    message["content_json"]["action_status"] = "superseded"
    disabled = build_discord_view(message, Client())
    assert all(item.disabled for item in disabled.children)


@pytest.mark.asyncio
async def test_user_consultation_advances_epoch_and_queues_v3_contract() -> None:
    class Repository:
        def __init__(self) -> None:
            self.request = None
            self.epoch_call = None

        async def current_project_task(self, project_id):
            return {"run_id": "task-1"}

        async def ingest_message(self, **kwargs):
            return {"message": {"message_id": "inbound-1"}, "duplicate": False}

        async def advance_supervisor_context_epoch(self, task_id, reason, source_id=None):
            self.epoch_call = (task_id, reason, source_id)
            return 4

        async def create_run(self, **kwargs):
            self.request = kwargs["request"]
            return {"run_id": "supervisor-run-1"}

        async def create_outbound(self, **kwargs):
            return {"message_id": "ack-1"}

    repository = Repository()
    result = await _start_supervisor_consultation(
        settings=SimpleNamespace(), repository=repository,
        project_id="demo", channel="discord", chat_id="channel-1",
        user_id="user-1", external_event_id="event-1",
        external_message_id="external-1", text="please revise",
        trigger="user_consultation",
    )
    assert result["run"]["run_id"] == "supervisor-run-1"
    assert repository.epoch_call == (
        "task-1", "new_supervisor_message", "inbound-1",
    )
    assert repository.request["schema_version"] == "supervisor-consultation.v3"
    assert repository.request["source_context_epoch"] == 4
    assert repository.request["source_message_id"] == "inbound-1"


@pytest.mark.asyncio
async def test_duplicate_user_event_does_not_advance_epoch_again() -> None:
    class Repository:
        async def current_project_task(self, project_id):
            return {"run_id": "task-1"}

        async def ingest_message(self, **kwargs):
            return {"message": {"message_id": "inbound-1"}, "duplicate": True}

        async def advance_supervisor_context_epoch(self, *args, **kwargs):
            raise AssertionError("duplicate event advanced epoch")

    result = await _start_supervisor_consultation(
        settings=SimpleNamespace(), repository=Repository(),
        project_id="demo", channel="discord", chat_id="channel-1",
        user_id="user-1", external_event_id="event-1",
        external_message_id="external-1", text="same event",
        trigger="user_consultation",
        task_id="task-1",
    )
    assert result["duplicate"] is True
    assert result["run"] is None
