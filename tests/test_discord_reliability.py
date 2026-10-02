from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
from psycopg import OperationalError

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.channel_health import ChannelHealthReporter
from assistant_kernel.services.discord import (
    InteractionAckResult, KernelDiscordClient,
    ingest_discord_event_with_retry,
)


def settings_for(tmp_path):
    return KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})


def unknown_interaction_error() -> discord.NotFound:
    error = discord.NotFound.__new__(discord.NotFound)
    Exception.__init__(error, "Unknown interaction")
    error.code = 10062
    return error


class FakeResponse:
    def __init__(self, error=None):
        self.done = False
        self.error = error
        self.modal = None

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.done = True

    async def send_modal(self, modal):
        if self.error is not None:
            raise self.error
        self.done = True
        self.modal = modal

    async def send_message(self, *args, **kwargs):
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


def interaction(*, error=None, age_seconds=0):
    channel = SimpleNamespace(id=200, parent_id=None, name="pm", parent=None)
    return SimpleNamespace(
        id=987654, guild_id=100, channel_id=200, channel=channel,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        user=SimpleNamespace(id=300, bot=False, roles=[], display_name="owner"),
        response=FakeResponse(error), followup=FakeFollowup(), message=None,
    )


@pytest.mark.asyncio
async def test_interaction_ack_normal_expired_network_and_stale(tmp_path):
    settings = settings_for(tmp_path)
    client = KernelDiscordClient(
        settings, {"allow_dm": False, "message_content": True},
        SimpleNamespace(), ChannelHealthReporter(settings, "discord"),
    )
    try:
        normal = await client._ack_interaction(interaction())
        assert normal.status == "ok"
        assert normal.fallback_allowed is False

        expired = await client._ack_interaction(
            interaction(error=unknown_interaction_error(), age_seconds=2)
        )
        assert expired.status == "expired"
        assert expired.fallback_allowed is True

        uncertain = await client._ack_interaction(
            interaction(error=aiohttp.ServerDisconnectedError(), age_seconds=2)
        )
        assert uncertain.status == "network_uncertain"
        assert uncertain.fallback_allowed is True

        stale = await client._ack_interaction(
            interaction(error=unknown_interaction_error(), age_seconds=31)
        )
        assert stale.status == "expired"
        assert stale.fallback_allowed is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slash_command_expired_ack_is_ingested_once(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    client = KernelDiscordClient(
        settings, {"allow_dm": False, "message_content": True},
        SimpleNamespace(), ChannelHealthReporter(settings, "discord"),
    )
    captured = []

    async def ingest(settings, payload, **kwargs):
        captured.append(payload)
        return {"duplicate": False}

    monkeypatch.setattr(
        "assistant_kernel.services.discord.ingest_discord_event_with_retry", ingest
    )
    client._ack_interaction = AsyncMock(return_value=InteractionAckResult(
        status="expired", interaction_age_ms=2000,
        defer_duration_ms=3000, fallback_allowed=True,
    ))
    try:
        command = client.tree.get_command("new")
        await command._callback(interaction())
        assert len(captured) == 1
        assert captured[0]["event_id"] == "987654"
        assert captured[0]["content"] == "/new"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stale_slash_and_expired_component_do_not_execute(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    client = KernelDiscordClient(
        settings, {"allow_dm": False, "message_content": True},
        SimpleNamespace(), ChannelHealthReporter(settings, "discord"),
    )
    ingest = AsyncMock()
    monkeypatch.setattr(
        "assistant_kernel.services.discord.ingest_discord_event_with_retry", ingest
    )
    client._ack_interaction = AsyncMock(return_value=InteractionAckResult(
        status="expired", interaction_age_ms=31_000,
        defer_duration_ms=3000, fallback_allowed=False,
    ))
    client._queue_component_retry_notice = AsyncMock()
    try:
        await client.tree.get_command("new")._callback(interaction(age_seconds=31))
        ingest.assert_not_awaited()

        await client.handle_kernel_component(
            interaction(), "message-1", "supervisor:adopt", None,
            {"message_id": "message-1"},
        )
        client._queue_component_retry_notice.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_credential_modal_opens_from_snapshot_before_database_read(tmp_path):
    settings = settings_for(tmp_path)
    repository = SimpleNamespace()
    client = KernelDiscordClient(
        settings, {"allow_dm": False, "message_content": True}, repository,
        ChannelHealthReporter(settings, "discord"),
    )
    current = interaction()
    snapshot = {
        "message_id": "question-1", "user_id": "300",
        "content_json": {"input_schema": {
            "kind": "project_credential",
            "fields": [{"name": "api_key", "label": "API key"}],
        }},
    }
    try:
        await client.handle_kernel_component(
            current, "question-1", "credential", None, snapshot,
        )
        assert current.response.modal is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingress_retry_is_bounded_and_reuses_payload(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    payload = {"event_id": "same", "message_id": "same"}
    calls = []

    async def flaky(settings, received, before_resume=None):
        calls.append(received)
        if len(calls) < 3:
            raise OperationalError("temporary")
        return {"duplicate": len(calls) > 1}

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("assistant_kernel.services.discord.ingest_discord_event", flaky)
    monkeypatch.setattr("assistant_kernel.services.discord.asyncio.sleep", no_sleep)
    result = await ingest_discord_event_with_retry(settings, payload)
    assert result["duplicate"] is True
    assert calls == [payload, payload, payload]


@pytest.mark.asyncio
async def test_ingress_permission_error_is_not_retried(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    calls = 0

    async def denied(settings, payload, before_resume=None):
        nonlocal calls
        calls += 1
        raise PermissionError("denied")

    monkeypatch.setattr("assistant_kernel.services.discord.ingest_discord_event", denied)
    with pytest.raises(PermissionError):
        await ingest_discord_event_with_retry(settings, {"event_id": "one"})
    assert calls == 1
