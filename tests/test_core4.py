"""Isolated PostgreSQL tests for the four-table first-release core."""
from __future__ import annotations

import json
import asyncio
import base64
import hashlib
import importlib
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
import httpx
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from psycopg import sql
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.harness import AgentTurnResult
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.repositories.core import approval_decision, canonical_sha256
from assistant_kernel.services.context_budget import TurnUsage
from assistant_kernel.services.capability_bundle import CapabilityBundleService
from assistant_kernel.services.current_artifacts import CurrentArtifactService
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.run_processor import (
    RunProcessor, message_context_visibility, normalize_question_options,
    normalize_work_order, prompt_message_content,
)
from assistant_kernel.services.feishu import (
    FeishuSender, enable_card_frame_dispatch, _extract_feishu_message_text, _websocket_card_action_payload,
    _websocket_message_payload, ingest_event, parse_slash_command,
    render_feishu_message,
)
from assistant_kernel.services.discord import (
    DiscordSender, KernelDiscordClient, build_discord_view,
    discord_origin_rejection, ingest_discord_event,
    load_discord_config, render_discord_text, split_discord_text,
    validate_discord_config,
)
from assistant_kernel.services.agent_files import RouteResolver
from assistant_kernel.services.project_directory import ProjectDirectoryService
from assistant_kernel.services.discord_projects import DiscordProjectProvisioner
from assistant_kernel.services.discord_interfaces import DiscordInterfaceRegistry
from assistant_kernel.services.audit import audit_read_only_tools
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.reconciliation import ReconciliationService
from assistant_kernel.services.recovery import RecoveryService
from assistant_kernel.services.source_scope import SourceScopeService
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager


@pytest.fixture
def core_settings(tmp_path):
    base = KernelSettings.from_environment()
    database_name = "kernel_core4_test_" + uuid.uuid4().hex
    connection_args = {
        "host": base.postgres_host,
        "port": base.postgres_port,
        "dbname": base.postgres_database,
        "user": base.postgres_user,
        "password": base.postgres_password.get_secret_value(),
    }
    with psycopg.connect(**connection_args, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    settings = base.model_copy(update={
        "postgres_database": database_name,
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "provider-secrets.json",
        "shared_volcengine_path": tmp_path / "shared-models/volcengine.json",
        "service_credentials_path": tmp_path / "service-credentials.json",
    })
    try:
        with psycopg.connect(
            **{**connection_args, "dbname": database_name}, autocommit=True
        ) as connection:
            schema = Path(__file__).parents[1] / "migrations" / "schema_core4.sql"
            connection.execute(schema.read_text(encoding="utf-8"))
            upgrade = Path(__file__).parents[1] / "migrations" / "upgrade_core5_v1.sql"
            connection.execute(upgrade.read_text(encoding="utf-8"))
            upgrade = Path(__file__).parents[1] / "migrations" / "upgrade_core5_v2.sql"
            connection.execute(upgrade.read_text(encoding="utf-8"))
            upgrade = Path(__file__).parents[1] / "migrations" / "upgrade_core5_v3.sql"
            connection.execute(upgrade.read_text(encoding="utf-8"))
            upgrade = Path(__file__).parents[1] / "migrations" / "upgrade_core5_v4.sql"
            connection.execute(upgrade.read_text(encoding="utf-8"))
            upgrade = Path(__file__).parents[1] / "migrations" / "upgrade_core5_v5.sql"
            connection.execute(upgrade.read_text(encoding="utf-8"))
            upgrade = Path(__file__).parents[1] / "migrations" / "upgrade_core5_v6.sql"
            connection.execute(upgrade.read_text(encoding="utf-8"))
        yield settings
    finally:
        assert database_name.startswith("kernel_core4_test_")
        with psycopg.connect(**connection_args, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def prepare_agent(settings: KernelSettings, project_id: str = "demo") -> None:
    root = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    (root / "agent-config.yaml").write_text(
        "\n".join(
            (
                f"agent_key: project:{project_id}:manager",
                "reasoning_effort: medium",
                "timeout_seconds: 60",
                "sandbox: read-only",
                "skills:",
                "  - demo-skill",
                "",
            )
        ),
        encoding="utf-8",
    )
    (root / "role.md").write_text("You are the demo assistant.", encoding="utf-8")
    skill = root / ".agents" / "skills" / "demo-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("Always answer using verified facts.", encoding="utf-8")


def test_prompt_message_content_separates_semantics_from_transport() -> None:
    sanitized = prompt_message_content({
        "direction": "outbound",
        "message_type": "approval",
        "content_json": {
            "text": "display wrapper and duplicate document",
            "summary": "ready for approval",
            "document_markdown": "# Requirements",
            "approval_question": "Approve it?",
            "interaction_seq": 2,
            "correlation": {"run_id": "internal"},
            "delivery": {"mode": "reply"},
            "raw": {"event": "transport"},
            "discord": {"guild_id": "100", "member": {"role_ids": ["200"]}},
            "author_context": {"principal_id": "not-resolved"},
        },
    })
    assert sanitized == {
        "summary": "ready for approval",
        "document_markdown": "# Requirements",
        "approval_question": "Approve it?",
    }
    assert prompt_message_content({
        "message_type": "question",
        "content_json": {
            "text": "interface wrapper", "question_id": "path",
            "question_text": "Which path?", "required": True,
            "options": [{"id": "a", "label": "Path A", "description": "first"}],
        },
    }) == {
        "question_id": "path", "question_text": "Which path?",
        "required": True,
        "options": [{"id": "a", "label": "Path A", "description": "first"}],
    }
    assert prompt_message_content({
        "message_type": "answer",
        "content_json": {
            "schema_version": "choice-answer.v1", "question_id": "path",
            "choice_id": "a", "text": "Path A", "choice_status": "recorded",
        },
    }) == {"question_id": "path", "choice_id": "a", "text": "Path A"}


def prepare_stage(settings: KernelSettings, project_id: str, stage: str) -> None:
    root = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    stage_root = root / "stages" / stage
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / "agent-config.yaml").write_text(
        "\n".join((
            f"agent_key: project:{project_id}:{stage}",
            "reasoning_effort: medium", "timeout_seconds: 60",
            "sandbox: read-only", "skills: []", "",
        )),
        encoding="utf-8",
    )
    (stage_root / "role.md").write_text(
        f"You are the {stage} stage.", encoding="utf-8"
    )


def prepare_v2_stage(settings: KernelSettings, project_id: str, stage: str) -> None:
    root = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    stage_root = root / "agents" / stage
    stage_root.mkdir(parents=True, exist_ok=True)
    (stage_root / "config.yaml").write_text(yaml.safe_dump({
        "schema_version": "agent-config.v2", "agent_id": stage,
        "agent_key": f"project:{project_id}:{stage}", "kind": "stage",
        "status": "active", "display_name": stage.title(), "aliases": [],
        "role": "role.md", "scope_profile": "current-project",
        "harness": {"provider": "openai", "reasoning_effort": "medium",
                    "context_policy": "max", "timeout_seconds": 60,
                    "sandbox": "read-only"},
        "skills": [], "mcp_servers": {}, "entrypoints": {},
        "triggers": ["stage"], "tools": [],
    }, sort_keys=False), encoding="utf-8")
    (stage_root / "role.md").write_text(
        f"You are the {stage} stage.", encoding="utf-8"
    )


def prepare_bindable_project(settings: KernelSettings, project_id: str = "demo") -> None:
    root = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    (root / "project.yaml").write_text(yaml.safe_dump({
        "schema_version": "project.v2",
        "project_id": project_id,
        "layout_version": 2,
        "task_protocol": "task.v1",
        "status": "active",
        "display_name": project_id,
        "purpose": "binding test",
        "default_agent": "pm",
        "work_modes": {
            mode: {
                "mode_id": mode, "profile": mode,
                "context_profile": mode,
                "permission_profile": (
                    "candidate-write" if mode in {"build", "repair"}
                    else "read-only"
                ),
                "validation_policy": {
                    "required_scopes": ["structure", "component"]
                },
            }
            for mode in ("requirements", "build", "audit", "operate", "repair")
        },
    }), encoding="utf-8")
    agent = root / "agents" / "pm"
    agent.mkdir(parents=True)
    (agent / "config.yaml").write_text(yaml.safe_dump({
        "schema_version": "agent-config.v2",
        "agent_id": "pm",
        "agent_key": f"project:{project_id}:manager",
        "kind": "manager",
        "status": "active",
        "display_name": "Project Manager",
        "aliases": ["manager"],
        "role": "role.md",
        "scope_profile": "current-project",
        "harness": {
            "provider": "openai", "reasoning_effort": "medium",
            "context_policy": "max", "timeout_seconds": 60,
            "sandbox": "read-only",
        },
        "skills": [],
        "mcp_servers": {},
        "entrypoints": {"pm_handoff": True, "feishu_slash": True},
        "triggers": ["user_message"],
        "tools": [],
    }, sort_keys=False), encoding="utf-8")
    (agent / "role.md").write_text("You are the Project PM.", encoding="utf-8")
    directory = settings.data_root / "config" / "project-directory.yaml"
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.write_text(yaml.safe_dump({
        "schema_version": "project-directory.v1",
        "projects": [{
            "project_id": project_id,
            "purpose": "binding test",
            "status": "active",
        }],
    }, sort_keys=False), encoding="utf-8")


async def prepare_single_agent_build_run(
    settings: KernelSettings, repository: CoreRepository, run: dict,
    worker_instance_id: str, goal: str, *, validation_preapproved: bool = False,
) -> dict:
    task_documents = TaskDocumentService(settings)
    contract_ref = task_documents.write_revision(
        project_id="demo", task_id=str(run["run_id"]),
        document_kind="task-contract", document_schema="task-contract.v1",
        payload={
            "goal": goal,
            "scope": {"included": [], "excluded": []},
            "requirements": ["Candidate must validate."],
            "acceptance_criteria": ["candidate validates"],
            "constraints": [], "source_refs": [],
        },
        status="ready", actor_type="pm", actor_id="project:demo:manager",
        run_id=str(run["run_id"]), reason="test approved Task Contract",
    )
    return await repository.patch_claimed_run_request(
        run_id=str(run["run_id"]), worker_instance_id=worker_instance_id,
        claim_token=str(run["claim_token"]), request_patch={
            "task_protocol": "single-agent.v2", "task_root_run_id": str(run["run_id"]),
            "task_goal": goal, "task_turn_count": 0, "task_phase": "working",
            "current_stage": "build", "candidate_only": True,
            "network_allowed": True, "max_task_turns": 12,
            "audit_required": False,
            "validation_policy": {"required_scopes": ["structure", "component"]},
            "state_revision": 1,
            "accepted_documents": {"task_contract": contract_ref},
            "task_contract_ref": contract_ref,
            "task_contract_sha256": contract_ref["sha256"],
            "requirements_sha256": contract_ref["sha256"],
            "requirements_approval": {
                "message_id": "test-approval",
                "approved_document_sha256": contract_ref["sha256"],
                "task_contract_ref": contract_ref,
            },
            **({
                "pending_validation_supervisor_turn": 1,
                "validation_supervisor_approved_turn": 1,
            } if validation_preapproved else {}),
        },
    )


def prepare_discord_config(
    settings: KernelSettings, *, users: list[str] | None = None,
    owners: list[str] | None = None, archived: list[str] | None = None,
    auto_provision: bool = False,
) -> None:
    settings.service_credentials_path.parent.mkdir(parents=True, exist_ok=True)
    settings.service_credentials_path.write_text(json.dumps({
        "services": {
            "discord": {
                "enabled": True,
                "application_id": "1000",
                "bot_token": "test-token",
                "home_channel_id": "999",
                "guild_ids": ["100"],
                "allowed_channel_ids": ["200"],
                "allowed_user_ids": ["300"] if users is None else users,
                "owner_user_ids": ["300"] if owners is None else owners,
                "archived_channel_ids": [] if archived is None else archived,
                "allow_dm": False,
                "message_content": True,
                "auto_provision_projects": auto_provision,
            }
        }
    }), encoding="utf-8")


class FakeHarness:
    name = "fake"

    def __init__(self, responses: list[tuple]):
        self.responses = responses
        self.requests = []

    async def run_turn(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        final_text, input_tokens, native_session_id = response[:3]
        metadata = {}
        if len(response) == 4:
            metadata["context_usage_known"] = bool(response[3])
        return AgentTurnResult(
            physical_session_id=native_session_id,
            usage=TurnUsage(input_tokens=input_tokens),
            events=[],
            final_text=final_text,
            archive_uri=f"vault://{request.project_id}/runs/fake.jsonl",
            archive_sha256="f" * 64,
            provider=request.provider,
            model=request.model,
            metadata=metadata,
        )


def test_project_creation_approval_phrase_is_recognized_exactly() -> None:
    assert approval_decision({"text": "批准创建"}) == "approve"
    assert approval_decision({"text": "创建"}) is None


def test_audit_tool_policy_is_fail_closed_to_read_only_capabilities() -> None:
    assert audit_read_only_tools((
        "read_authorized_file", "start_project_task", "list_recent_history",
        "delete_project_credential", "handoff_to_project",
    )) == ("read_authorized_file", "list_recent_history")


def test_feishu_rich_post_text_is_extracted_without_content_v2_duplication() -> None:
    content = json.dumps({
        "title": "",
        "content": [[{"tag": "text", "text": "first", "style": []}], [
            {"tag": "text", "text": "second", "style": []}
        ]],
        "content_v2": [[{"tag": "text", "text": "duplicate", "style": []}]],
    })
    assert _extract_feishu_message_text(content) == "first\nsecond"


@pytest.mark.asyncio
async def test_feishu_standby_records_without_starting_a_run(core_settings) -> None:
    core_settings.service_credentials_path.parent.mkdir(parents=True, exist_ok=True)
    core_settings.service_credentials_path.write_text(json.dumps({
        "services": {
            "feishu": {
                "enabled": True, "receive_mode": "websocket", "mode": "standby",
                "app_id": "app-1", "app_secret": "test",
            }
        }
    }), encoding="utf-8")
    result = await ingest_event(core_settings, {
        "header": {"event_id": "standby-message"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "chat_type": "group",
                "message_id": "message-1",
                "content": json.dumps({"text": "ordinary project chat"}),
            },
        },
    }, "app-1")
    assert result["standby"] is True
    assert result["run"] is None
    assert "Discord" in result["outbound"]["content_json"]["text"]
    assert await CoreRepository(core_settings).list_runs("system") == []


def test_extended_slash_commands_are_small_and_deterministic() -> None:
    assert parse_slash_command("/new") == ("/new", None)
    assert parse_slash_command("@_user_1 /goal 继续完善策略") == (
        "/goal", "继续完善策略"
    )
    assert parse_slash_command("/resume 00000000-0000-0000-0000-000000000001") == (
        "/resume", "00000000-0000-0000-0000-000000000001"
    )
    assert parse_slash_command("/audit quick") == ("/audit", "quick")
    assert parse_slash_command("/status") == ("/status", None)
    assert parse_slash_command("/kernel-status") == ("/kernel-status", None)
    assert parse_slash_command("/kernel_status") == ("/kernel-status", None)
    assert parse_slash_command("/supervisor 帮我整理下一轮Prompt") == (
        "/supervisor", "帮我整理下一轮Prompt"
    )
    assert parse_slash_command("/prompt") == ("/prompt", None)
    assert parse_slash_command("/stage") == ("/stage", None)
    assert parse_slash_command("/why") == ("/why", None)
    assert parse_slash_command("/home/v/AI") is None


def test_discord_config_is_fail_closed_and_text_splitting_is_bounded(tmp_path) -> None:
    settings = KernelSettings.from_environment().model_copy(update={
        "service_credentials_path": tmp_path / "service-credentials.json",
    })
    prepare_discord_config(settings)
    config = load_discord_config(settings)
    validate_discord_config(config)
    assert config["allowed_user_ids"] == {"300"}
    assert config["owner_user_ids"] == {"300"}

    config["allowed_user_ids"] = set()
    config["allowed_channel_ids"] = set()
    validate_discord_config(config)
    assert discord_origin_rejection(
        config, guild_id="100", channel_id="any-channel",
        parent_channel_id=None, user_id="any-user",
    ) is None
    assert discord_origin_rejection(
        config, guild_id="other-guild", channel_id="any-channel",
        parent_channel_id=None, user_id="any-user",
    ) == "guild_not_allowed"
    assert discord_origin_rejection(
        config, guild_id=None, channel_id="dm-1",
        parent_channel_id=None, user_id="300",
    ) == "dm_disabled"
    config["allow_dm"] = True
    assert discord_origin_rejection(
        config, guild_id=None, channel_id="dm-1",
        parent_channel_id=None, user_id="300",
    ) is None
    assert discord_origin_rejection(
        config, guild_id=None, channel_id="dm-1",
        parent_channel_id=None, user_id="301",
    ) == "dm_user_not_owner"
    config["allow_dm"] = False
    config["guild_ids"] = set()
    with pytest.raises(ValueError, match="guild_ids"):
        validate_discord_config(config)

    chunks = split_discord_text(("line\n" * 900).strip())
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 1900 for chunk in chunks)


@pytest.mark.asyncio
async def test_discord_project_channel_provisioning_is_idempotent(core_settings):
    prepare_discord_config(core_settings, auto_provision=True)
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/channels/555"):
            return httpx.Response(200, json={
                "id": "555", "guild_id": "100", "name": "demo",
            })
        if request.method == "GET":
            return httpx.Response(200, json=[])
        assert json.loads(request.content)["topic"].endswith(
            "assistant-kernel-project:demo"
        )
        return httpx.Response(201, json={
            "id": "555", "name": "demo",
            "topic": "binding test\nassistant-kernel-project:demo",
        })

    config = load_discord_config(core_settings)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://discord.test/api/v10",
    ) as client:
        provisioner = DiscordProjectProvisioner(repository, config, client)
        first = await provisioner.provision(
            project_id="demo", display_name="Demo", purpose="binding test",
            guild_id="100", bound_by_user_id="300", source_message_id="source",
            project_routing_hash="a" * 64,
        )
        second = await provisioner.provision(
            project_id="demo", display_name="Demo", purpose="binding test",
            guild_id="100", bound_by_user_id="300", source_message_id="source",
            project_routing_hash="a" * 64,
        )

    assert first == {
        "project_id": "demo", "guild_id": "100", "channel_id": "555",
        "created": True, "binding_changed": True, "recovered": False,
    }
    assert second["channel_id"] == "555"
    assert second["created"] is False
    assert second["recovered"] is True
    assert requests == [
        ("GET", "/api/v10/guilds/100/channels"),
        ("POST", "/api/v10/guilds/100/channels"),
        ("GET", "/api/v10/channels/555"),
    ]
    assert (await repository.resolve_project_chat_binding(
        "discord", "demo"
    ))["chat_id"] == "555"


@pytest.mark.asyncio
async def test_discord_project_interfaces_are_grouped_named_and_registered(core_settings):
    prepare_discord_config(core_settings, auto_provision=True)
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    await repository.bind_chat_project(
        channel="discord", chat_id="555", project_id="demo",
        agent_key="project:demo:manager", bound_by_user_id="300",
        source_message_id="source", project_routing_hash="a" * 64,
        idempotency_key="interface-main-binding",
    )
    patched: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/channels/555"):
            return httpx.Response(200, json={
                "id": "555", "guild_id": "100", "name": "demo", "parent_id": None,
            })
        if request.method == "GET" and request.url.path.endswith("/guilds/100/channels"):
            return httpx.Response(200, json=[
                {"id": "500", "type": 4, "name": "PROJECT · demo"},
                {"id": "556", "type": 0, "name": "demo-supervisor",
                 "parent_id": None,
                 "topic": "assistant-kernel-interface:supervisor:demo"},
            ])
        if request.method == "PATCH":
            body = json.loads(request.content)
            patched.append((request.url.path, body))
            channel_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": channel_id, **body})
        raise AssertionError(f"unexpected Discord request: {request.method} {request.url.path}")

    config = load_discord_config(core_settings)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://discord.test/api/v10",
    ) as client:
        result = await DiscordProjectProvisioner(
            repository, config, client
        ).provision_supervisor_interface(project_id="demo", guild_id="100")

    assert result["category_id"] == "500"
    assert result["pm_channel_id"] == "555"
    assert result["supervisor_channel_id"] == "556"
    assert patched == [
        ("/api/v10/channels/555", {"parent_id": "500", "name": "pm"}),
        ("/api/v10/channels/556", {"parent_id": "500", "name": "supervisor"}),
    ]
    registry = json.loads(
        (core_settings.data_root / "config" / "discord-interfaces.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["interfaces"]["556"] == {
        "guild_id": "100", "project_id": "demo", "role": "supervisor",
    }
    assert DiscordInterfaceRegistry(core_settings).find(
        project_id="demo", role="supervisor"
    )["channel_id"] == "556"


@pytest.mark.asyncio
async def test_discord_project_provisioning_replaces_deleted_bound_channel(
    core_settings,
) -> None:
    prepare_discord_config(core_settings, auto_provision=True)
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    await repository.bind_chat_project(
        channel="discord", chat_id="555", project_id="demo",
        agent_key="project:demo:manager", bound_by_user_id="300",
        source_message_id="original", project_routing_hash="a" * 64,
        idempotency_key="original-binding",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/channels/555"):
            return httpx.Response(404, json={"message": "Unknown Channel"})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={
            "id": "777", "guild_id": "100", "name": "demo",
            "topic": "binding test\nassistant-kernel-project:demo",
        })

    config = load_discord_config(core_settings)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://discord.test/api/v10",
    ) as client:
        result = await DiscordProjectProvisioner(repository, config, client).provision(
            project_id="demo", display_name="Demo", purpose="binding test",
            guild_id="100", bound_by_user_id="300", source_message_id="retry",
            project_routing_hash="a" * 64,
        )

    assert result["channel_id"] == "777"
    assert result["created"] is True
    assert (await repository.resolve_chat_project_binding(
        "discord", "555"
    )) is None
    assert (await repository.resolve_project_chat_binding(
        "discord", "demo"
    ))["chat_id"] == "777"


@pytest.mark.asyncio
async def test_project_handoff_can_move_first_pm_run_to_new_discord_channel(
    core_settings,
) -> None:
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="system", agent_key="system-butler", channel="discord",
        chat_id="999", user_id="300", external_event_id="create:source",
        external_message_id="source", content={
            "text": "create demo",
            "discord": {"guild_id": "100", "channel_id": "999"},
        },
    )
    worker = str(uuid.uuid4())
    source = await repository.claim_run("test", worker, 60)
    moved = await repository.handoff_from_claim(
        source_run_id=str(source["run_id"]), worker_instance_id=worker,
        claim_token=str(source["claim_token"]), target_project_id="demo",
        handoff_summary="Start in the Project channel.",
        target_channel="discord", target_chat_id="555",
    )
    assert moved["message"]["chat_id"] == "555"
    assert moved["message"]["user_id"] == "300"
    assert str(moved["run"]["conversation_key"]).startswith("conversation:")
    assert moved["run"]["conversation_key"] != source["conversation_key"]


@pytest.mark.asyncio
async def test_discord_thread_binding_identity_dedup_and_owner_control(core_settings):
    prepare_discord_config(core_settings, users=[], owners=["300"])
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)

    def event(
        event_id: str, user_id: str, content: str, *, thread: bool = False,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "message_id": event_id,
            "guild_id": "100",
            "channel_id": "201" if thread else "200",
            "parent_channel_id": "200" if thread else None,
            "user_id": user_id,
            "member": {
                "user_id": user_id,
                "display_name": "Reviewer",
                "role_ids": ["role-reviewer"],
            },
            "author_bot": False,
            "content": content,
            "is_thread": thread,
            "attachments": [],
        }

    denied = await ingest_discord_event(
        core_settings, event("bind-denied", "301", "/bind_project demo")
    )
    assert denied["run"] is None
    assert "无权" in denied["outbound"]["content_json"]["text"]
    assert await repository.resolve_chat_project_binding("discord", "200") is None

    bound = await ingest_discord_event(
        core_settings, event("bind-allowed", "300", "/bind_project demo")
    )
    assert bound["run"] is None
    assert (await repository.resolve_chat_project_binding(
        "discord", "200"
    ))["project_id"] == "demo"
    inherited_bind = await ingest_discord_event(
        core_settings, event("thread-bind", "300", "/bind_project demo", thread=True)
    )
    assert "Thread继承父频道Project" in inherited_bind["outbound"]["content_json"]["text"]

    first = await ingest_discord_event(
        core_settings, event("message-1", "301", "continue in this thread", thread=True)
    )
    duplicate = await ingest_discord_event(
        core_settings, event("message-1", "301", "continue in this thread", thread=True)
    )
    assert first["run"] is not None
    assert first["message"]["channel"] == "discord"
    assert first["message"]["chat_id"] == "201"
    assert first["message"]["user_id"] == "301"
    assert first["message"]["content_json"]["discord"]["parent_channel_id"] == "200"
    assert first["message"]["content_json"]["discord"]["member"]["role_ids"] == [
        "role-reviewer"
    ]
    assert duplicate["duplicate"] is True
    assert duplicate["run"] is None
    assert len(await repository.list_runs("demo")) == 1


@pytest.mark.asyncio
async def test_discord_unbound_guild_channel_defaults_to_butler(core_settings):
    prepare_discord_config(core_settings, users=[], owners=["300"])
    result = await ingest_discord_event(core_settings, {
        "event_id": "new-channel-message",
        "message_id": "new-channel-message",
        "guild_id": "100",
        "channel_id": "999",
        "user_id": "777",
        "author_bot": False,
        "content": "route this through the Butler",
        "attachments": [],
    })
    assert result["message"]["project_id"] == "system"
    assert result["message"]["user_id"] == "777"
    assert result["run"]["agent_key"] == "system-butler"


@pytest.mark.asyncio
async def test_discord_audit_command_queues_versioned_read_only_run(core_settings):
    prepare_discord_config(core_settings, users=[], owners=["300"])
    repository = CoreRepository(core_settings)
    event = {
        "event_id": "audit-1", "interaction_id": "audit-1",
        "guild_id": "100", "channel_id": "999", "user_id": "300",
        "author_bot": False, "content": "/audit full", "attachments": [],
    }
    first = await ingest_discord_event(core_settings, event)
    duplicate = await ingest_discord_event(core_settings, event)
    assert first["command"] == "/audit"
    assert first["audit_mode"] == "full"
    assert first["run"] is not None
    assert first["message"]["content_json"]["schema_version"] == (
        "session-impact-audit.v2"
    )
    assert first["message"]["content_json"]["audit_scope"] == (
        "current_session_latest_round"
    )
    prompt = first["message"]["content_json"]["text"]
    assert "只读检查" in prompt
    assert "最近一轮" in prompt
    assert "实际影响到的依赖关系" in prompt
    assert duplicate["duplicate"] is True
    assert duplicate["run"] is None
    assert len(await repository.list_runs("system")) == 1


@pytest.mark.asyncio
async def test_discord_unbound_non_home_channel_requires_project_binding(core_settings):
    prepare_discord_config(core_settings, users=[], owners=["300"])
    result = await ingest_discord_event(core_settings, {
        "event_id": "unbound-channel-message", "message_id": "unbound-channel-message",
        "guild_id": "100", "channel_id": "555", "user_id": "777",
        "author_bot": False, "content": "should not start an LLM Run", "attachments": [],
    })
    assert result["run"] is None
    assert result["unbound_group"] is True
    assert "/bind_project" in result["outbound"]["content_json"]["text"]


@pytest.mark.asyncio
async def test_discord_supervisor_interface_queues_consultation_without_pm_run(
    core_settings, monkeypatch,
):
    """v6.1: supervisor-channel text queues a consultation Run (or explains
    when no Task is open); it never creates a PM chat Run."""
    prepare_discord_config(core_settings, users=[], owners=["300"])
    prepare_bindable_project(core_settings)
    registry = core_settings.data_root / "config" / "discord-interfaces.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({
        "schema_version": "discord-interfaces.v1",
        "interfaces": {
            "555": {"guild_id": "100", "project_id": "demo", "role": "supervisor"}
        },
    }), encoding="utf-8")

    result = await ingest_discord_event(core_settings, {
        "event_id": "supervisor-plain", "message_id": "supervisor-plain",
        "guild_id": "100", "channel_id": "555", "user_id": "300",
        "author_bot": False, "content": "请解释当前阶段", "attachments": [],
    })
    repository = CoreRepository(core_settings)
    assert result["run"] is None
    assert result["message"]["project_id"] == "demo"
    assert result["outbound"]["content_json"]["origin"] == "kernel"
    assert "没有进行中的Task" in result["outbound"]["content_json"]["text"]
    assert await repository.list_runs("demo") == []

    # With an open Project task, the same channel text queues a consultation.
    execution = await repository.create_run(
        project_id="demo", agent_key="project:demo:python", run_type="python",
        request={"source_uri": "vault://demo/tools/x.py"},
        idempotency_key="supervisor-routing-task",
    )
    result = await ingest_discord_event(core_settings, {
        "event_id": "supervisor-plain-2", "message_id": "supervisor-plain-2",
        "guild_id": "100", "channel_id": "555", "user_id": "300",
        "author_bot": False, "content": "继续任务的下一步建议", "attachments": [],
    })
    assert result["run"] is not None
    consultation = result["run"]
    assert consultation["agent_key"] == "project:demo:supervisor"
    assert consultation["run_type"] == "chat"
    assert consultation["executor_json"]["scheduling_lane"] == "conversation"
    assert consultation["request_json"]["interaction_kind"] == "supervisor_consultation"
    assert consultation["request_json"]["task_id"] == str(execution["run_id"])
    assert consultation["request_json"]["trigger"] == "user_consultation"
    # The ack notice references the consultation, and no PM chat run appeared.
    assert result["outbound"]["content_json"]["origin"] == "kernel"
    assert "正在基于当前Task状态评估" in result["outbound"]["content_json"]["text"]
    runs = await repository.list_runs("demo")
    assert not any(
        str(item["agent_key"]) == "project:demo:manager" for item in runs
    )


@pytest.mark.asyncio
async def test_supervisor_action_plan_adoption_is_transactional_and_resumes_task(
    core_settings,
):
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    created = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager",
        channel="discord", chat_id="pm-channel", user_id="user-1",
        external_event_id="adopt-task", external_message_id="adopt-task",
        content={"text": "prepare one bounded task"},
    )
    task = created["run"]
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    assert str(claimed["run_id"]) == str(task["run_id"])
    action_plan_ref = {
        "schema_version": "resource-ref.v1", "kind": "document",
        "id": f"supervisor-action-plan:{task['run_id']}",
        "uri": "vault://demo/runs/task/documents/action.json",
        "document_schema": "supervisor-action-plan.v1",
        "revision_id": "rev-test", "sha256": "b" * 64,
        "purpose": "supervisor-action-plan",
    }
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.runs
            SET status='waiting_user',worker_id=NULL,worker_instance_id=NULL,
                claim_token=NULL,lease_until=NULL,process_pid=NULL,
                process_start_ticks=NULL,
                request_json=request_json || %s::jsonb
            WHERE run_id=%s
            """,
            (json.dumps({
                "accepted_documents": {"current_action_plan": action_plan_ref},
                "task_turn_count": 1,
                "pending_validation_supervisor_turn": 1,
            }), task["run_id"]),
        )
        await connection.commit()
    prompt = "Please continue the same Task using the confirmed bounded objective."
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    card = await repository.queue_supervisor_status(
        project_id="demo", run_id=str(task["run_id"]),
        channel="discord", chat_id="supervisor-channel", user_id="user-1",
        text="Supervisor recommendation", turn_number=1,
        decision="continue_current_stage", prompt_sha256=prompt_sha,
        card_revision=1, delivery_route="project_supervisor_interface",
        decision_id="supdec-1", decision_ref="vault://demo/decision.json",
        action_plan_ref=action_plan_ref, recommended_prompt=prompt,
    )
    result = await repository.record_supervisor_action(
        card_message_id=str(card["message_id"]),
        external_event_id="discord-app:adopt-click-1",
        actor_user_id="user-1", action="adopt",
    )
    assert result["status"] == "adopted"
    current = await repository.get_run(str(task["run_id"]))
    assert current["status"] == "queued"
    assert "adopted_supervisor_prompt" not in current["request_json"]
    assert current["request_json"]["validation_supervisor_approved_turn"] == 1
    repeated = await repository.record_supervisor_action(
        card_message_id=str(card["message_id"]),
        external_event_id="discord-app:adopt-click-1",
        actor_user_id="user-1", action="adopt",
    )
    assert repeated["duplicate"] is True
    async with repository.database.connection() as connection:
        decision_count = await (await connection.execute(
            """
            SELECT count(*) AS total FROM assistant.messages
            WHERE project_id='demo'
                  AND content_json->>'schema_version'='decision-record.v2'
              AND content_json->>'action'='adopt'
            """
        )).fetchone()
    assert int(decision_count["total"]) == 2  # authority record + PM handoff projection


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_discord_archived_channel_is_kernel_read_only(core_settings):
    prepare_discord_config(core_settings, users=[], owners=["300"], archived=["555"])
    result = await ingest_discord_event(core_settings, {
        "event_id": "archive-message", "message_id": "archive-message",
        "guild_id": "100", "channel_id": "555", "user_id": "300",
        "author_bot": False, "content": "/bind_project demo", "attachments": [],
    })
    assert result["run"] is None
    assert result["archived_channel"] is True
    assert "已归档" in result["outbound"]["content_json"]["text"]
    assert await CoreRepository(core_settings).resolve_chat_project_binding(
        "discord", "555"
    ) is None


@pytest.mark.asyncio
async def test_discord_client_registers_commands_without_connecting(core_settings):
    prepare_discord_config(core_settings)
    config = load_discord_config(core_settings)
    client = KernelDiscordClient(core_settings, config, CoreRepository(core_settings))
    try:
        assert {command.name for command in client.tree.get_commands()} == {
            "new", "goal", "resume", "bind_project", "unbind_project",
            "pending", "status", "kernel-status", "audit", "binding",
            "supervisor", "prompt", "stage", "why",
            "whoami", "help", "pm", "butler",
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_discord_sender_chunks_once_and_persists_all_platform_ids(core_settings):
    class FakeRepository:
        def __init__(self) -> None:
            self.ack = None

        async def confirm_delivery_current(self, message_id, worker_instance_id):
            return True

        async def delivery_reply_external_id(self, message_id):
            return "900"

        async def acknowledge_delivery(
            self, message_id, worker_instance_id, external_message_id, delivery,
        ):
            self.ack = (message_id, external_message_id, delivery)
            return True

    class FakeChannel:
        def __init__(self) -> None:
            self.sent = []

        def get_partial_message(self, message_id):
            return SimpleNamespace(id=message_id)

        async def send(self, content, **kwargs):
            self.sent.append((content, kwargs))
            return SimpleNamespace(id=1000 + len(self.sent))

    class FakeClient:
        def __init__(self, channel) -> None:
            self.channel = channel

        def get_channel(self, channel_id):
            return self.channel

    repository = FakeRepository()
    channel = FakeChannel()
    sender = DiscordSender(core_settings, repository, FakeClient(channel))
    await sender.send({
        "message_id": "message-1",
        "chat_id": "201",
        "content_json": {
            "text": "x" * 4000,
            "origin": "kernel", "context_visibility": "ui_only",
        },
        "message_type": "reply",
    }, "worker-1")

    assert len(channel.sent) == 3
    assert all(len(content) <= 1900 for content, _ in channel.sent)
    assert channel.sent[0][0].startswith("Kernel: ")
    assert "reference" in channel.sent[0][1]
    assert "reference" not in channel.sent[1][1]
    assert repository.ack == (
        "message-1", "1001", {
            "mode": "reply",
            "message_ids": ["1001", "1002", "1003"],
            "chunk_count": 3,
            "msg_type": "text",
        },
    )


def test_discord_kernel_prefix_does_not_mark_agent_output() -> None:
    assert render_discord_text({
        "message_type": "kernel_notice",
        "content_json": {"text": "execution paused"},
    }) == "Kernel: execution paused"
    assert render_discord_text({
        "message_type": "reply", "run_id": str(uuid.uuid4()),
        "content_json": {"text": "PM answer"},
    }) == "PM answer"


@pytest.mark.asyncio
async def test_discord_views_cover_question_approval_resume_and_credentials(core_settings):
    prepare_discord_config(core_settings)
    client = KernelDiscordClient(
        core_settings, load_discord_config(core_settings), CoreRepository(core_settings),
    )
    try:
        approval = build_discord_view({
            "message_id": str(uuid.uuid4()), "message_type": "approval",
            "content_json": {},
        }, client)
        assert [item.label for item in approval.children] == [
            "Approve", "Request changes", "Reject",
        ]
        question = build_discord_view({
            "message_id": str(uuid.uuid4()), "message_type": "question",
            "content_json": {"options": [
                {"id": "a", "label": "A"}, {"id": "b", "label": "B"},
            ]},
        }, client)
        assert [item.label for item in question.children] == ["A", "B"]
        resume = build_discord_view({
            "message_id": str(uuid.uuid4()), "message_type": "reply",
            "content_json": {
                "interaction_kind": "resume_select",
                "resume_options": [{"id": str(uuid.uuid4()), "label": "Run one"}],
            },
        }, client)
        assert len(resume.children) == 1
        credential = build_discord_view({
            "message_id": str(uuid.uuid4()), "message_type": "question",
            "content_json": {"input_schema": {
                "kind": "project_credential", "fields": [
                    {"name": "api_key", "label": "API key", "required": True},
                ],
            }},
        }, client)
        assert credential.children[0].label == "Enter credential"
        reconciliation = build_discord_view({
            "message_id": str(uuid.uuid4()), "message_type": "reconciliation",
            "content_json": {
                "interaction_kind": "reconcile_run",
                "actions": [
                    {"id": "retry", "label": "重新执行", "style": "success"},
                    {"id": "fail", "label": "标记失败", "style": "secondary"},
                    {"id": "cancel", "label": "取消任务", "style": "danger"},
                ],
            },
        }, client)
        assert [item.label for item in reconciliation.children] == [
            "重新执行", "标记失败", "取消任务",
        ]
    finally:
        await client.close()


def test_approval_renders_as_an_interactive_card() -> None:
    message = {
        "message_id": "question-1",
        "project_id": "demo",
        "run_id": "run-1",
        "message_type": "approval",
        "content_json": {"text": "Requirements ready"},
    }
    msg_type, card = render_feishu_message(message)
    assert msg_type == "interactive"
    actions = card["elements"][1]["actions"]
    assert [item["value"]["decision"] for item in actions] == [
        "approve", "revise", "reject"
    ]
    assert all(item["value"]["question_message_id"] == "question-1" for item in actions)


def test_finite_question_renders_choice_card_and_open_question_stays_text() -> None:
    message = {
        "message_id": "question-2", "project_id": "demo", "run_id": "run-2",
        "message_type": "question",
        "content_json": {
            "text": "Which path?\n\n1. A\n2. B", "question_id": "path",
            "interaction_seq": 3,
            "options": [
                {"id": "a", "label": "A", "description": "first"},
                {"id": "b", "label": "B", "description": "second"},
            ],
        },
    }
    msg_type, card = render_feishu_message(message)
    assert msg_type == "interactive"
    actions = card["elements"][1]["actions"]
    assert [item["value"]["choice_id"] for item in actions] == ["a", "b"]
    assert all(item["value"]["question_message_id"] == "question-2" for item in actions)
    assert all(item["value"]["kind"] == "question_choice" for item in actions)
    message["content_json"].pop("options")
    assert render_feishu_message(message) == ("text", {"text": "Which path?\n\n1. A\n2. B"})
    assert normalize_question_options([
        {"id": "a", "label": "A"}, {"id": "b", "label": "B"},
    ]) == [
        {"id": "a", "label": "A", "description": ""},
        {"id": "b", "label": "B", "description": ""},
    ]
    with pytest.raises(ValueError, match="question option"):
        normalize_question_options([{"id": "a", "label": "A"}] )


def test_feishu_card_action_is_normalized_for_existing_ingestion() -> None:
    event = SimpleNamespace(
        header=SimpleNamespace(
            event_id="card-event", event_type="card.action.trigger",
            app_id="app", tenant_key="tenant",
        ),
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="user-1", user_id=""),
            action=SimpleNamespace(
                value={"decision": "approve", "run_id": "run-1"},
                form_value={"TOKEN": "value"}, tag="button", name="submit",
            ),
            context=SimpleNamespace(open_message_id="external-1", open_chat_id="chat-1"),
        ),
    )
    payload = _websocket_card_action_payload(event)
    assert payload["header"]["event_id"] == "card-event"
    assert payload["event"]["action"]["value"]["decision"] == "approve"
    assert payload["event"]["action"]["form_value"] == {"TOKEN": "value"}
    assert payload["event"]["context"]["open_message_id"] == "external-1"


async def test_project_credential_form_saves_plaintext_and_resumes_run(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="credential:initial",
        external_message_id="initial", content={"text": "download data"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    store = ProjectCredentialsStore(core_settings.data_root)
    requested = store.request(
        project_id="demo", run_id=str(run["run_id"]),
        credential_key="alpaca_market_data", description="请填写Alpaca行情凭据",
        fields=[
            {"name": "APCA_API_KEY_ID", "label": "Key ID"},
            {"name": "APCA_API_SECRET_KEY", "label": "Secret"},
        ],
    )
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "credential missing",
                "questions": [requested["question"]],
            }), 10, "credential-native",
        )]),
    ).process(run, worker)
    delivery_worker = str(uuid.uuid4())
    outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
    assert await repository.acknowledge_delivery(
        str(outbound["message_id"]), delivery_worker, "credential-card-external",
        {"mode": "chat", "msg_type": "interactive"},
    )
    question = await repository.get_message(waiting["question_message_id"])
    msg_type, card = render_feishu_message(question)
    assert msg_type == "interactive"
    form = card["elements"][1]
    assert form["tag"] == "form"
    submit = form["elements"][-1]
    assert submit["action_type"] == "form_submit"
    event = {
        "header": {"event_id": "credential:submit"},
        "event": {
            "operator": {"open_id": "user-1"},
            "action": {
                "value": submit["value"],
                "form_value": {
                    "APCA_API_KEY_ID": "id-value",
                    "APCA_API_SECRET_KEY": "secret-value",
                },
            },
            "context": {
                "open_chat_id": "chat-1",
                "open_message_id": "credential-card-external",
            },
        },
    }
    accepted = await ingest_event(core_settings, event, "app-1")
    assert accepted["run"]["status"] == "queued"
    assert store.get("demo", "alpaca_market_data")["fields"]["APCA_API_SECRET_KEY"] == "secret-value"
    answer = accepted["message"]["content_json"]
    assert answer["credential_result"]["status"] == "saved"
    assert "id-value" not in json.dumps(answer)
    assert "secret-value" not in json.dumps(answer)


async def test_discord_credential_modal_saves_and_resumes_waiting_run(core_settings):
    prepare_agent(core_settings)
    prepare_discord_config(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="200", user_id="300", external_event_id="discord-credential:initial",
        external_message_id="discord-credential:initial", content={"text": "download data"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    store = ProjectCredentialsStore(core_settings.data_root)
    requested = store.request(
        project_id="demo", run_id=str(run["run_id"]),
        credential_key="market_data", description="请填写行情凭据",
        fields=[{"name": "MARKET_API_KEY", "label": "行情 API Key"}],
    )
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "credential missing",
                "questions": [requested["question"]],
            }), 10, "discord-credential-native",
        )]),
    ).process(run, worker)
    delivery_worker = str(uuid.uuid4())
    outbound = await repository.claim_delivery(
        "sender", delivery_worker, 60, "discord"
    )
    assert await repository.acknowledge_delivery(
        str(outbound["message_id"]), delivery_worker,
        "discord-credential-card", {"mode": "question"},
    )
    question = await repository.get_message(waiting["question_message_id"])

    class Response:
        def __init__(self):
            self.done = False

        def is_done(self):
            return self.done

        async def defer(self, **kwargs):
            self.done = True

        async def send_message(self, *args, **kwargs):
            self.done = True

    class Followup:
        def __init__(self):
            self.messages = []

        async def send(self, content, **kwargs):
            self.messages.append(content)

    class PartialMessage:
        async def edit(self, **kwargs):
            return None

    class Channel:
        def get_partial_message(self, message_id):
            return PartialMessage()

    interaction = SimpleNamespace(
        id=987654,
        guild_id=100,
        channel_id=200,
        user=SimpleNamespace(
            id=300, bot=False, roles=[], display_name="owner",
        ),
        response=Response(),
        followup=Followup(),
        channel=Channel(),
        message=SimpleNamespace(id="discord-credential-card"),
    )
    client = KernelDiscordClient(
        core_settings, load_discord_config(core_settings), repository,
    )
    try:
        await client.handle_credential_submit(
            interaction, question, {"MARKET_API_KEY": "secret-value"},
        )
    finally:
        await client.close()

    current = await repository.get_run(str(run["run_id"]))
    assert current["status"] == "queued"
    assert store.get("demo", "market_data")["fields"] == {
        "MARKET_API_KEY": "secret-value"
    }
    assert interaction.followup.messages


async def test_feishu_card_frame_dispatch_preserves_card_response_and_event_path() -> None:
    ws = importlib.import_module("lark_oapi.ws.client")
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
    calls = []

    def dispatch(event):
        calls.append(event.event.action.value["choice_id"])
        return P2CardActionTriggerResponse({
            "toast": {"type": "info", "content": "recorded"}
        })

    dispatcher = EventDispatcherHandler.builder("", "", ws.LogLevel.WARNING).register_p2_card_action_trigger(dispatch).build()

    class FakeClient:
        def __init__(self):
            self._event_handler = dispatcher
            self.sent = []
            self.normal_frames = 0

        async def _handle_data_frame(self, frame):
            self.normal_frames += 1

        async def _write_message(self, payload):
            self.sent.append(payload)

    client = FakeClient()
    enable_card_frame_dispatch(client, ws)
    card = ws.Frame()
    card.SeqID, card.LogID, card.service = 0, 0, 0
    card.method = ws.FrameType.DATA.value
    for key, value in (
        (ws.HEADER_TYPE, ws.MessageType.CARD.value),
        (ws.HEADER_SUM, "1"),
        (ws.HEADER_MESSAGE_ID, "card-1"),
        (ws.HEADER_SEQ, "0"),
    ):
        header = card.headers.add()
        header.key, header.value = key, value
    card.payload = json.dumps({
        "schema": "2.0",
        "header": {"event_id": "card-event", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "user-1"},
            "action": {"value": {"choice_id": "a"}},
            "context": {"open_message_id": "message-1", "open_chat_id": "chat-1"},
        },
    }).encode()
    original_card = card.SerializeToString()
    await client._handle_data_frame(card)
    assert calls == ["a"]
    assert len(client.sent) == 1
    response_frame = ws.Frame()
    response_frame.ParseFromString(client.sent[0])
    assert ws._get_by_key(response_frame.headers, ws.HEADER_TYPE) == ws.MessageType.CARD.value
    response = json.loads(response_frame.payload)
    assert response["code"] == 200
    assert json.loads(base64.b64decode(response["data"]))["toast"]["content"] == "recorded"
    sdk_client = ws.Client("test-app", "test-secret", event_handler=dispatcher)
    sdk_responses = []

    async def record_sdk_response(payload):
        sdk_responses.append(payload)

    sdk_client._write_message = record_sdk_response
    enable_card_frame_dispatch(sdk_client, ws)
    sdk_card = ws.Frame()
    sdk_card.ParseFromString(original_card)
    await sdk_client._handle_data_frame(sdk_card)
    assert calls == ["a", "a"]
    assert len(sdk_responses) == 1
    event = ws.Frame()
    event_header = event.headers.add()
    event_header.key, event_header.value = ws.HEADER_TYPE, ws.MessageType.EVENT.value
    await client._handle_data_frame(event)
    assert client.normal_frames == 1


def _choice_card_event(event_id: str, external_message_id: str, value: dict) -> dict:
    return {
        "header": {"event_id": event_id},
        "event": {
            "operator": {"open_id": "user-1"},
            "action": {"value": value},
            "context": {
                "open_chat_id": "chat-1",
                "open_message_id": external_message_id,
            },
        },
    }


async def test_question_choice_click_is_durable_and_never_creates_correction_run(
    core_settings,
):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="choice:initial",
        external_message_id="initial", content={"text": "choose"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "two paths",
                "questions": [{
                    "question_id": "path", "question_text": "Which path?",
                    "required": True,
                    "options": [
                        {"id": "a", "label": "Path A"},
                        {"id": "b", "label": "Path B"},
                    ],
                }],
            }), 10, "choice-native",
        )]),
    ).process(run, worker)
    delivery_worker = str(uuid.uuid4())
    outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
    assert await repository.acknowledge_delivery(
        str(outbound["message_id"]), delivery_worker, "choice-card-external",
        {"mode": "chat", "msg_type": "interactive"},
    )
    question = await repository.get_message(waiting["question_message_id"])
    _, card = render_feishu_message(question)
    first_value, second_value = [
        item["value"] for item in card["elements"][1]["actions"]
    ]
    with pytest.raises(ValueError, match="not an option"):
        await ingest_event(
            core_settings,
            _choice_card_event(
                "choice:forged", "choice-card-external",
                {**first_value, "choice_id": "forged"},
            ), "app-1",
        )
    wrong_user = _choice_card_event(
        "choice:wrong-user", "choice-card-external", first_value,
    )
    wrong_user["event"]["operator"]["open_id"] = "user-2"
    with pytest.raises((ValueError, PermissionError)):
        await ingest_event(core_settings, wrong_user, "app-1")
    assert (await repository.get_run(str(run["run_id"])))["status"] == "waiting_user"
    event = _choice_card_event("choice:click-1", "choice-card-external", first_value)
    accepted = await ingest_event(core_settings, event, "app-1")
    assert accepted["choice_status"] == "recorded"
    assert accepted["resumed"] is True
    updated = await repository.get_run(str(run["run_id"]))
    assert updated["status"] == "queued"
    assert updated["request_json"]["answer"]["answers"] == {"path": "Path A"}
    duplicate = await ingest_event(core_settings, event, "app-1")
    assert duplicate["duplicate"] is True
    assert duplicate["choice_status"] == "recorded"
    late = await ingest_event(
        core_settings,
        _choice_card_event("choice:click-2", "choice-card-external", second_value),
        "app-1",
    )
    assert late["choice_status"] == "expired"
    assert len(await repository.list_runs("demo")) == 1


async def test_invalid_optional_choices_keep_the_existing_text_question(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="invalid-choice:initial",
        external_message_id="initial", content={"text": "ask"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "open question",
                "questions": [{
                    "question_id": "open", "question_text": "Explain your preference?",
                    "required": True,
                    "options": [{"id": "only", "label": "Only one"}],
                }],
            }), 10, "invalid-choice-native",
        )]),
    ).process(run, worker)
    question = await repository.get_message(waiting["question_message_id"])
    assert question["content_json"]["options"] == []
    assert render_feishu_message(question)[0] == "text"


async def test_multiple_choice_cards_resurface_only_unanswered_after_new(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    initial = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="choices:initial",
        external_message_id="initial", content={"text": "choose twice"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    questions = [
        {
            "question_id": key, "question_text": f"Choose {key}?", "required": True,
            "options": [
                {"id": "a", "label": f"{key} A"},
                {"id": "b", "label": f"{key} B"},
            ],
        }
        for key in ("one", "two")
    ]
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({"outcome": "needs_input", "summary": "two choices",
                        "questions": questions}), 10, "choices-native",
        )]),
    ).process(run, worker)
    values = {}
    for _ in waiting["question_message_ids"]:
        delivery_worker = str(uuid.uuid4())
        outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
        question_id = outbound["content_json"]["question_id"]
        external_id = f"choices-card-{question_id}"
        assert await repository.acknowledge_delivery(
            str(outbound["message_id"]), delivery_worker, external_id,
            {"mode": "chat", "msg_type": "interactive"},
        )
        _, card = render_feishu_message(await repository.get_message(str(outbound["message_id"])))
        values[question_id] = (
            external_id, [item["value"] for item in card["elements"][1]["actions"]]
        )
    first = await ingest_event(
        core_settings,
        _choice_card_event("choices:first", values["one"][0], values["one"][1][0]),
        "app-1",
    )
    assert first["partial_answer"] is True
    repeated = await ingest_event(
        core_settings,
        _choice_card_event("choices:second-click", values["one"][0], values["one"][1][1]),
        "app-1",
    )
    assert repeated["choice_status"] == "already_answered"
    assert (await repository.get_run(str(run["run_id"])))["request_json"][
        "answer_state"
    ] == {"one": "one A"}
    await repository.reset_conversation(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(initial["message"]["message_id"]),
        idempotency_key="choices:reset",
    )
    old_click = await ingest_event(
        core_settings,
        _choice_card_event("choices:old", values["two"][0], values["two"][1][0]),
        "app-1",
    )
    assert old_click["choice_status"] == "expired"
    resumed = await repository.resume_waiting_run(
        run_id=str(run["run_id"]), project_id="demo", channel="feishu",
        chat_id="chat-1", user_id="user-1",
        source_message_id=str(initial["message"]["message_id"]),
        idempotency_key="choices:resume",
    )
    assert len(resumed["messages"]) == 1
    assert resumed["message"]["content_json"]["question_id"] == "two"
    delivery_worker = str(uuid.uuid4())
    outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
    assert await repository.acknowledge_delivery(
        str(outbound["message_id"]), delivery_worker, "choices-card-resumed",
        {"mode": "chat", "msg_type": "interactive"},
    )
    _, card = render_feishu_message(await repository.get_message(str(outbound["message_id"])))
    final = await ingest_event(
        core_settings,
        _choice_card_event(
            "choices:final", "choices-card-resumed",
            card["elements"][1]["actions"][1]["value"],
        ), "app-1",
    )
    assert final["resumed"] is True
    assert (await repository.get_run(str(run["run_id"])))["request_json"][
        "answer"
    ] == {"answers": {"one": "one A", "two": "two B"}}
    assert len(await repository.list_runs("demo")) == 1


async def test_schema_contains_expected_core_tables(core_settings):
    repository = CoreRepository(core_settings)
    assert await repository.schema_version() == "core5.v6"
    assert await repository.table_names() == [
        "kernel_meta", "messages", "model_invocations", "project_grants", "runs", "sessions"
    ]


async def test_execution_and_delivery_health_expose_stalls(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    created = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat", user_id="user", external_event_id="health:run",
        external_message_id="health-run", content={"text": "queued"},
    )
    execution = await repository.execution_health()
    assert execution["queued"] >= 1
    await repository.create_outbound(
        project_id="demo", session_id=None,
        run_id=str(created["run"]["run_id"]), channel="discord",
        chat_id="chat", user_id="user", message_type="reply",
        content={"text": "pending"}, reply_to_message_id=None,
        idempotency_key="health:delivery",
    )
    delivery = await repository.delivery_health("discord")
    assert delivery["pending"] >= 1
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.messages
            SET delivery_status='failed',updated_at=now()
            WHERE idempotency_key='health:delivery'
            """
        )
        await connection.commit()
    failed = await repository.delivery_health("discord")
    assert failed["failed_recent_15m"] >= 1


async def test_supervisor_barrier_atomically_resumes_parent(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    ingested = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat", user_id="user", external_event_id="barrier:in",
        external_message_id="m", content={"text": "continue"},
    )
    worker = str(uuid.uuid4())
    task = await repository.claim_run("pm", worker, 60)
    continued = await repository.finalize_agent_continue(
        run_id=str(task["run_id"]), worker_instance_id=worker,
        claim_token=str(task["claim_token"]), project_id="demo",
        channel="discord", chat_id="chat", user_id="user",
        source_message_id=str(ingested["message"]["message_id"]),
        summary="turn settled",
        request_patch={
            "task_turn_count": 1, "supervisor_context_epoch": 0,
            "state_revision": 1, "accepted_documents": {},
            "pending_validation_supervisor_turn": 1,
        },
        artifact_refs=[], wait_for_supervisor=True,
    )
    assert continued["run"]["status"] == "waiting_supervisor"
    supervisor = await repository.create_run(
        project_id="demo", agent_key="project:demo:supervisor", run_type="chat",
        trigger_message_id=str(continued["reply"]["message_id"]),
        parent_run_id=str(task["run_id"]),
        request={
            "interaction_kind": "supervisor_consultation",
            "task_id": str(task["run_id"]), "turn_number": 1,
            "source_turn_number": 1, "source_context_epoch": 0,
        },
        idempotency_key=f"supervisor-test:{task['run_id']}",
    )
    supervisor_worker = str(uuid.uuid4())
    claimed = await repository.claim_run("supervisor", supervisor_worker, 60)
    assert str(claimed["run_id"]) == str(supervisor["run_id"])
    action_ref = {
        "schema_version": "resource-ref.v1", "kind": "document",
        "id": "supervisor-action-plan:test", "uri": "vault://demo/action.json",
        "document_schema": "supervisor-action-plan.v1", "revision_id": "rev-test",
        "sha256": "a" * 64, "purpose": "supervisor-action-plan",
    }
    committed = await repository.commit_supervisor_action(
        supervisor_run_id=str(claimed["run_id"]),
        worker_instance_id=supervisor_worker,
        claim_token=str(claimed["claim_token"]),
        task_id=str(task["run_id"]), source_context_epoch=0,
        source_turn_number=1, action_ref=action_ref,
        confirmation_class="safe", pm_instruction="continue safely",
        target_work_mode="requirements", delegations=[],
        result={"outcome": "evaluated"}, artifact_refs=[],
        session_action="rotate",
    )
    assert committed["task"]["status"] == "queued"
    assert committed["supervisor_run"]["status"] == "succeeded"
    assert committed["task"]["request_json"]["accepted_documents"][
        "current_action_plan"
    ] == action_ref
    assert committed["task"]["request_json"]["force_session_rotation"][
        "source_action_plan_ref"
    ] == action_ref
    assert committed["task"]["request_json"][
        "validation_supervisor_approved_turn"
    ] == 1


async def test_degraded_supervision_bypasses_matching_validation_barrier(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    created = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat", user_id="user", external_event_id="degraded-validation",
        external_message_id="m", content={"text": "validate"},
    )
    run_id = str(created["run"]["run_id"])
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.runs
            SET status='waiting_supervisor',
                request_json=request_json || %s::jsonb
            WHERE run_id=%s
            """,
            (json.dumps({
                "task_turn_count": 1,
                "pending_validation_supervisor_turn": 1,
            }), run_id),
        )
        await connection.commit()
    resumed = await repository.resume_supervision_degraded(
        task_id=run_id, turn_number=1, reason="injected supervisor failure",
    )
    assert resumed["status"] == "queued"
    assert resumed["request_json"]["validation_supervisor_approved_turn"] == 1
    assert resumed["request_json"]["validation_supervision_bypassed_turn"] == 1
    assert resumed["request_json"]["degraded_supervision"] is True


async def test_kernel_child_run_is_parent_only_and_resumes_parent(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    ingested = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="child-chat", user_id="user", external_event_id="child:in",
        external_message_id="child-m", content={"text": "delegate"},
    )
    worker = str(uuid.uuid4())
    task = await repository.claim_run("pm", worker, 60)
    continued = await repository.finalize_agent_continue(
        run_id=str(task["run_id"]), worker_instance_id=worker,
        claim_token=str(task["claim_token"]), project_id="demo",
        channel="discord", chat_id="child-chat", user_id="user",
        source_message_id=str(ingested["message"]["message_id"]),
        summary="delegate", request_patch={
            "task_turn_count": 1, "supervisor_context_epoch": 0,
            "state_revision": 1, "accepted_documents": {},
        }, artifact_refs=[], wait_for_supervisor=True,
    )
    supervisor = await repository.create_run(
        project_id="demo", agent_key="project:demo:supervisor", run_type="chat",
        trigger_message_id=str(continued["reply"]["message_id"]),
        request={"interaction_kind": "supervisor_consultation", "task_id": str(task["run_id"])},
        idempotency_key=f"child-supervisor:{task['run_id']}",
    )
    supervisor_worker = str(uuid.uuid4())
    claimed = await repository.claim_run("supervisor", supervisor_worker, 60)
    request_ref = {
        "schema_version": "resource-ref.v1", "kind": "document",
        "id": "delegation:test", "uri": "vault://demo/delegation.json",
        "document_schema": "delegation-request.v1", "revision_id": "rev-d",
        "sha256": "b" * 64, "purpose": "delegation",
    }
    committed = await repository.commit_supervisor_action(
        supervisor_run_id=str(supervisor["run_id"]),
        worker_instance_id=supervisor_worker,
        claim_token=str(claimed["claim_token"]), task_id=str(task["run_id"]),
        source_context_epoch=0, source_turn_number=0,
        action_ref={**request_ref, "id": "action:test"},
        confirmation_class="safe", pm_instruction="delegate",
        target_work_mode="requirements",
        delegations=[{
            "delegation_id": "d1", "executor": "kernel_child_run",
            "goal": "bounded work", "required": True, "request_ref": request_ref,
        }],
        result={"outcome": "evaluated"}, artifact_refs=[],
    )
    assert committed["task"]["status"] == "waiting_children"
    assert len(committed["children"]) == 1
    child = committed["children"][0]
    child_worker = str(uuid.uuid4())
    claimed_child = await repository.claim_run("child", child_worker, 60)
    assert str(claimed_child["run_id"]) == str(child["run_id"])
    result_ref = {**request_ref, "id": "child-result:d1", "purpose": "child-result"}
    finalized = await repository.finalize_child_task(
        run_id=str(child["run_id"]), worker_instance_id=child_worker,
        claim_token=str(claimed_child["claim_token"]),
        result={"outcome": "completed", "summary": "done"},
        result_ref=result_ref, artifact_refs=[],
    )
    assert finalized["parent"]["status"] == "queued"
    outbound = [
        item for item in await repository.list_messages_through(
            "demo", "discord", "child-chat", str(continued["reply"]["message_id"]),
            limit=50,
        )
        if item["direction"] == "outbound"
    ]
    assert all(item["run_id"] != child["run_id"] for item in outbound)


async def test_lane_indexes_allow_cross_agent_conversation_concurrency(core_settings):
    """v6.1 conflict-domain minimization at the hard-constraint level.

    Conversation runs serialize per conversation_key only: a supervisor
    conversation and a PM conversation in one Project may be running at the
    same time, while execution runs stay project-exclusive and a second run
    in the same conversation still cannot start.
    """
    repository = CoreRepository(core_settings)
    project = "lane-demo"
    prepare_agent(core_settings, project)
    message = await repository.ingest_message(
        project_id=project, agent_key=f"project:{project}:manager",
        channel="discord", chat_id="pm-channel", user_id="u1",
        external_event_id="evt-pm-1", external_message_id="ext-pm-1",
        content={"text": "start the pm conversation"},
    )
    pm_run = message["run"]
    assert pm_run is not None
    claimed_pm = await repository.claim_run("w1", str(uuid.uuid4()), 30)
    assert str(claimed_pm["run_id"]) == str(pm_run["run_id"])

    supervisor_message = await repository.ingest_message(
        project_id=project, agent_key=f"project:{project}:supervisor",
        channel="discord", chat_id="supervisor-channel", user_id="u1",
        external_event_id="evt-sup-1", external_message_id="ext-sup-1",
        content={"text": "supervisor consultation"},
    )
    # v6.1: supervisor messages never create a plain chat Run through
    # ingest; the consultation Run is created explicitly (as discord.py does).
    assert supervisor_message["run"] is None
    supervisor_run = await repository.create_run(
        project_id=project, agent_key=f"project:{project}:supervisor",
        run_type="chat",
        trigger_message_id=str(supervisor_message["message"]["message_id"]),
        request={
            "interaction_kind": "supervisor_consultation",
            "task_id": "none", "trigger": "user_consultation",
        },
        idempotency_key="lane-supervisor-consult-1",
    )
    assert supervisor_run["executor_json"]["scheduling_lane"] == "conversation"
    claimed_supervisor = await repository.claim_run("w1", str(uuid.uuid4()), 30)
    assert claimed_supervisor is not None
    assert str(claimed_supervisor["run_id"]) == str(supervisor_run["run_id"])
    assert claimed_supervisor["agent_key"] == f"project:{project}:supervisor"

    # A queued execution run in the same Project still cannot start while
    # another execution run is active.
    python_execution = await repository.create_run(
        project_id=project, agent_key=f"project:{project}:python",
        run_type="python", request={"source_uri": "vault://unused/x.py"},
        idempotency_key="lane-python-1",
    )
    await repository.create_run(
        project_id=project, agent_key=f"project:{project}:python",
        run_type="python", request={"source_uri": "vault://unused/y.py"},
        idempotency_key="lane-python-2",
    )
    first_python = await repository.claim_run("w2", str(uuid.uuid4()), 30)
    assert str(first_python["run_id"]) == str(python_execution["run_id"])
    assert await repository.claim_run("w2", str(uuid.uuid4()), 30) is None

    # Same conversation stays exclusive: nothing else is claimable while the
    # supervisor conversation and the execution lane both hold active runs.
    await repository.ingest_message(
        project_id=project, agent_key=f"project:{project}:supervisor",
        channel="discord", chat_id="supervisor-channel", user_id="u1",
        external_event_id="evt-sup-2", external_message_id="ext-sup-2",
        content={"text": "second supervisor message"},
    )
    assert await repository.claim_run("w3", str(uuid.uuid4()), 30) is None




async def test_duplicate_message_one_run_and_delivery(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    first = await repository.ingest_message(
        project_id="demo",
        agent_key="project:demo:manager",
        channel="feishu",
        chat_id="chat-1",
        user_id="user-1",
        external_event_id="app:event-1",
        external_message_id="external-1",
        content={"text": "hello"},
    )
    duplicate = await repository.ingest_message(
        project_id="demo",
        agent_key="project:demo:manager",
        channel="feishu",
        chat_id="chat-1",
        user_id="user-1",
        external_event_id="app:event-1",
        external_message_id="external-1",
        content={"text": "hello"},
    )
    assert duplicate["duplicate"] is True
    assert duplicate["run"] is None

    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    assert str(claimed["run_id"]) == str(first["run"]["run_id"])
    harness = FakeHarness([("done", 10, "native-1")])
    result = await RunProcessor(core_settings, repository, harness).process(claimed, worker)
    assert result["outcome"] == "completed"
    assert (await repository.get_run(str(claimed["run_id"])))["status"] == "succeeded"

    delivery_worker = str(uuid.uuid4())
    outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
    assert outbound["message_type"] == "reply"
    assert await repository.acknowledge_delivery(
        str(outbound["message_id"]), delivery_worker, "feishu-result-1"
    )
    assert (await repository.get_message(str(outbound["message_id"])))["delivery_status"] == "sent"


async def test_identical_queued_pm_message_keeps_only_latest_run(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    first = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="repeat:first",
        external_message_id="repeat-first", content={
            "text": "same update",
            "discord": {"attachments": []},
        },
    )
    second = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="repeat:second",
        external_message_id="repeat-second", content={
            "text": "same update",
            "discord": {"attachments": []},
        },
    )
    assert second["superseded_run_ids"] == [str(first["run"]["run_id"])]
    assert (await repository.get_run(str(first["run"]["run_id"])))["status"] == "cancelled"
    assert (await repository.get_run(str(second["run"]["run_id"])))["status"] == "queued"


@pytest.mark.asyncio
async def test_run_claims_parallelize_across_projects_but_serialize_each_conversation(core_settings):
    """v6.1 conflict domain: conversation runs serialize per conversation_key.

    Cross-project claims parallelize. Within one Project, two conversation
    runs in different chats may now run concurrently (the historical
    cross-agent same-lane project exclusion was removed); the same
    conversation and the execution lane stay exclusive.
    """
    repository = CoreRepository(core_settings)
    for event_id, project_id, chat_id in (
        ("project-a-1", "project-a", "chat-a-1"),
        ("project-a-2", "project-a", "chat-a-2"),
        ("project-b-1", "project-b", "chat-b-1"),
    ):
        await repository.ingest_message(
            project_id=project_id, agent_key=f"project:{project_id}:manager",
            channel="discord", chat_id=chat_id, user_id="user-1",
            external_event_id=event_id, external_message_id=event_id,
            content={"text": event_id},
        )

    first = await repository.claim_run("worker-a", str(uuid.uuid4()), 60)
    second = await repository.claim_run("worker-b", str(uuid.uuid4()), 60)
    third = await repository.claim_run("worker-c", str(uuid.uuid4()), 60)
    assert {first["project_id"], second["project_id"], third["project_id"]} == {
        "project-a", "project-b",
    }
    same_project = [
        item for item in (first, second, third) if item["project_id"] == "project-a"
    ]
    assert len(same_project) == 2
    assert len({item["conversation_key"] for item in same_project}) == 2
    assert await repository.claim_run("worker-d", str(uuid.uuid4()), 60) is None

    follow_up = await repository.ingest_message(
        project_id="project-a", agent_key="project:project-a:manager",
        channel="discord", chat_id="chat-a-1", user_id="user-1",
        external_event_id="project-a-3", external_message_id="project-a-3",
        content={"text": "same conversation follow-up"},
    )
    assert await repository.claim_run("worker-e", str(uuid.uuid4()), 60) is None
    active = next(
        item for item in same_project
        if item["conversation_key"] == follow_up["run"]["conversation_key"]
    )
    assert await repository.finish_run(
        str(active["run_id"]), str(active["worker_instance_id"]),
        str(active["claim_token"]),
        status="succeeded", result={"ok": True},
    )
    resumed = await repository.claim_run("worker-f", str(uuid.uuid4()), 60)
    assert resumed is not None
    assert str(resumed["run_id"]) == str(follow_up["run"]["run_id"])


@pytest.mark.asyncio
async def test_concurrent_claim_race_never_runs_two_for_one_conversation(core_settings):
    repository = CoreRepository(core_settings)
    for suffix in ("one", "two"):
        await repository.ingest_message(
            project_id="race", agent_key="project:race:manager", channel="discord",
            chat_id="chat-one", user_id="user-1",
            external_event_id=f"race-{suffix}", external_message_id=f"race-{suffix}",
            content={"text": suffix},
        )
    claims = await asyncio.gather(*(
        repository.claim_run(f"worker-{index}", str(uuid.uuid4()), 60)
        for index in range(2)
    ))
    assert sum(item is not None for item in claims) == 1


@pytest.mark.asyncio
async def test_claim_prioritizes_conversation_lane_over_older_execution_run(core_settings):
    """v6.1 anti-starvation: conversation runs claim ahead of older execution work.

    Two workers share one execution pool; without lane priority a long
    execution backlog could starve interactive chats.
    """
    repository = CoreRepository(core_settings)
    prepare_agent(core_settings, "priority-demo")
    python_execution = await repository.create_run(
        project_id="priority-demo", agent_key="project:priority-demo:python",
        run_type="python", request={"source_uri": "vault://unused/old.py"},
        idempotency_key="priority-python-old",
    )
    await repository.ingest_message(
        project_id="priority-demo", agent_key="project:priority-demo:manager",
        channel="discord", chat_id="chat-1", user_id="user-1",
        external_event_id="priority-chat", external_message_id="priority-chat",
        content={"text": "interactive question"},
    )
    claimed = await repository.claim_run("worker", str(uuid.uuid4()), 60)
    assert claimed is not None
    assert claimed["run_type"] == "chat"
    assert str(claimed["run_id"]) != str(python_execution["run_id"])


async def test_reconciling_execution_does_not_block_pm_conversation_lane(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    repository = CoreRepository(core_settings)
    initial = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="lane:initial",
        external_message_id="lane-initial", content={"text": "start requirements"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("manager", manager_worker, 60)
    stage = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="design safely",
        stages=["requirements"], change_class="major",
    )
    assert await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    execution_worker = str(uuid.uuid4())
    claimed_stage = await repository.claim_run("execution", execution_worker, 60)
    assert str(claimed_stage["run_id"]) == str(stage["run_id"])
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET status='reconciling',worker_id=NULL,"
            "worker_instance_id=NULL,claim_token=NULL,lease_until=NULL WHERE run_id=%s",
            (stage["run_id"],),
        )
        await connection.commit()
    follow_up = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="lane:follow-up",
        external_message_id="lane-follow-up", content={"text": "keep talking"},
    )
    conversation_worker = str(uuid.uuid4())
    claimed_chat = await repository.claim_run("conversation", conversation_worker, 60)
    assert str(claimed_chat["run_id"]) == str(follow_up["run"]["run_id"])
    assert (await repository.active_execution_blocker("demo"))["run_id"] == stage["run_id"]


async def test_recent_history_is_project_scoped_bounded_and_paginated(core_settings):
    repository = CoreRepository(core_settings)
    first = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="history:1",
        external_message_id="history-1", content={"text": "first", "raw": {"secret": "hidden"}},
    )
    second = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="api",
        chat_id="chat-2", user_id="user-1", external_event_id="history:2",
        external_message_id="history-2", content={"text": "second"},
    )
    await repository.ingest_message(
        project_id="other", agent_key="project:other:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="history:other",
        external_message_id="history-other", content={"text": "other-project"},
    )
    page = await repository.list_recent_history(
        project_id="demo", lookback_days=365, limit=1,
    )
    assert len(page["items"]) == 1
    assert page["items"][0]["message_id"] == str(second["message"]["message_id"])
    assert set(page["items"][0]) == {
        "message_id", "session_id", "run_id", "channel", "chat_id", "direction",
        "message_type", "reply_to_message_id", "text", "text_truncated", "created_at",
    }
    assert page["next_cursor"]
    older = await repository.list_recent_history(
        project_id="demo", lookback_days=365, limit=1, cursor=page["next_cursor"],
    )
    assert older["items"][0]["message_id"] == str(first["message"]["message_id"])
    current = await repository.list_recent_history(
        project_id="demo", lookback_days=365, channel="feishu", chat_id="chat-1",
    )
    assert [item["text"] for item in current["items"]] == ["first"]
    with pytest.raises(ValueError, match="lookback_days"):
        await repository.list_recent_history(project_id="demo", lookback_days=0)
    with pytest.raises(ValueError, match="cursor"):
        await repository.list_recent_history(project_id="demo", cursor="not-a-cursor")


async def test_route_resolver_uses_reply_then_durable_binding_then_default(core_settings):
    repository = CoreRepository(core_settings)
    resolver = RouteResolver(core_settings, repository)
    assert await resolver.resolve("api", "chat-1", "user-1") == (
        "system", "system-butler"
    )
    source = await repository.ingest_message(
        project_id="system", agent_key="system-butler", channel="api",
        chat_id="chat-1", user_id="user-1", external_event_id="route:source",
        external_message_id="route-source", content={"text": "route"},
    )
    outbound = await repository.create_outbound(
        project_id="system", session_id=None, run_id=str(source["run"]["run_id"]),
        channel="api", chat_id="chat-1", user_id="user-1", message_type="question",
        content={"text": "question"}, reply_to_message_id=str(source["message"]["message_id"]),
        idempotency_key="route-question",
    )
    assert await resolver.resolve(
        "api", "chat-1", "user-1", reply_to_message_id=str(outbound["message_id"]),
    ) == ("system", "system-butler")
    await repository.set_route_binding(
        channel="api", chat_id="chat-1", user_id="user-1", project_id="demo",
        agent_key="project:demo:manager", reason="test",
        source_message_id=str(source["message"]["message_id"]), idempotency_key="route-demo",
    )
    assert await resolver.resolve("api", "chat-1", "user-1") == (
        "demo", "project:demo:manager"
    )
    assert await resolver.resolve("api", "chat-1", "user-1", "explicit") == (
        "explicit", "project:explicit:manager"
    )


async def test_recent_history_is_available_through_scoped_mcp(core_settings):
    repository = CoreRepository(core_settings)
    ingested = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="api",
        chat_id="chat-1", user_id="user-1", external_event_id="mcp-history:1",
        external_message_id="mcp-history-1", content={"text": "remember this"},
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    scope_uri, scope_sha = WorkspaceScopeService(core_settings).snapshot(
        "demo", str(claimed["run_id"]), None,
    )
    environment = {
        **os.environ,
        "AI_HOME": str(core_settings.workspace_root),
        "ASSISTANT_DATA_ROOT": str(core_settings.data_root),
        "ASSISTANT_PROVIDER_SECRETS": str(core_settings.provider_secrets_path),
        "ASSISTANT_POSTGRES_HOST": core_settings.postgres_host,
        "ASSISTANT_POSTGRES_PORT": str(core_settings.postgres_port),
        "POSTGRES_DB": core_settings.postgres_database,
        "POSTGRES_USER": core_settings.postgres_user,
        "POSTGRES_PASSWORD": core_settings.postgres_password.get_secret_value(),
        "KERNEL_SCOPE_PROJECT_ID": "demo",
        "KERNEL_SCOPE_RUN_ID": str(claimed["run_id"]),
        "KERNEL_SCOPE_CLAIM_TOKEN": str(claimed["claim_token"]),
        "KERNEL_SCOPE_WORKER_INSTANCE_ID": worker,
        "KERNEL_SCOPE_ALLOWED_TOOLS": "get_authorized_workspace,list_recent_history",
        "KERNEL_SCOPE_STAGE": "",
        "KERNEL_SCOPE_SNAPSHOT_URI": scope_uri,
        "KERNEL_SCOPE_SNAPSHOT_SHA256": scope_sha,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "assistant_kernel.mcp_server.scoped_tools"],
        env=environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            await session.call_tool("get_authorized_workspace", {})
            result = await session.call_tool(
                "list_recent_history",
                {"lookback_days": 1, "limit": 10, "scope": "current_conversation"},
            )
    assert {tool.name for tool in tools.tools} == {
        "get_authorized_workspace", "list_recent_history",
    }
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["project_id"] == "demo"
    assert payload["items"][0]["message_id"] == str(ingested["message"]["message_id"])
    assert payload["items"][0]["text"] == "remember this"


async def test_shared_volcengine_status_is_available_without_exposing_key(core_settings):
    shared = core_settings.shared_volcengine_path
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(json.dumps({
        "ark": {"enabled": True, "api_key": "never-return-this-secret"}
    }), encoding="utf-8")
    repository = CoreRepository(core_settings)
    ingested = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="api",
        chat_id="chat-1", user_id="user-1", external_event_id="mcp-volcengine:1",
        external_message_id="mcp-volcengine-1", content={"text": "status"},
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    scope_uri, scope_sha = WorkspaceScopeService(core_settings).snapshot(
        "demo", str(claimed["run_id"]), None,
    )
    environment = {
        **os.environ,
        "AI_HOME": str(core_settings.workspace_root),
        "ASSISTANT_DATA_ROOT": str(core_settings.data_root),
        "ASSISTANT_PROVIDER_SECRETS": str(core_settings.provider_secrets_path),
        "ASSISTANT_SHARED_VOLCENGINE_CONFIG": str(shared),
        "ASSISTANT_POSTGRES_HOST": core_settings.postgres_host,
        "ASSISTANT_POSTGRES_PORT": str(core_settings.postgres_port),
        "POSTGRES_DB": core_settings.postgres_database,
        "POSTGRES_USER": core_settings.postgres_user,
        "POSTGRES_PASSWORD": core_settings.postgres_password.get_secret_value(),
        "KERNEL_SCOPE_PROJECT_ID": "demo",
        "KERNEL_SCOPE_RUN_ID": str(claimed["run_id"]),
        "KERNEL_SCOPE_CLAIM_TOKEN": str(claimed["claim_token"]),
        "KERNEL_SCOPE_WORKER_INSTANCE_ID": worker,
        "KERNEL_SCOPE_ALLOWED_TOOLS": "get_authorized_workspace,volcengine_model_status",
        "KERNEL_SCOPE_STAGE": "",
        "KERNEL_SCOPE_SNAPSHOT_URI": scope_uri,
        "KERNEL_SCOPE_SNAPSHOT_SHA256": scope_sha,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "assistant_kernel.mcp_server.scoped_tools"],
        env=environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            await session.call_tool("get_authorized_workspace", {})
            result = await session.call_tool("volcengine_model_status", {})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["ark"]["configured"] is True
    assert "never-return-this-secret" not in result.content[0].text
    assert str(ingested["run"]["run_id"]) == str(claimed["run_id"])


async def test_queued_runs_use_their_own_causal_message_boundary(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    first = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="causal:first",
        external_message_id="first", content={"text": "CAUSAL_FIRST"},
    )
    notice = await repository.create_outbound(
        project_id="demo", session_id=None, run_id=None, channel="feishu",
        chat_id="chat-1", user_id="user-1", message_type="kernel_notice",
        content={
            "text": "KERNEL_UI_ONLY", "origin": "kernel",
            "context_visibility": "ui_only",
        },
        reply_to_message_id=None, idempotency_key="causal:kernel-notice",
    )
    assert message_context_visibility(notice) == "ui_only"
    second = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="causal:second",
        external_message_id="second", content={"text": "CAUSAL_SECOND"},
    )
    harness = FakeHarness([
        ("FIRST_RESULT", 10, "native-1"),
        ("SECOND_RESULT", 10, "native-1"),
    ])
    processor = RunProcessor(core_settings, repository, harness)
    worker = str(uuid.uuid4())
    claimed_first = await repository.claim_run("test", worker, 60)
    assert claimed_first["run_id"] == first["run"]["run_id"]
    await processor.process(claimed_first, worker)
    assert harness.requests[0].scope_context["KERNEL_SCOPE_SNAPSHOT_URI"].startswith(
        "vault://demo/runs/"
    )
    assert len(harness.requests[0].scope_context["KERNEL_SCOPE_SNAPSHOT_SHA256"]) == 64
    context_view = json.loads(
        harness.requests[0].scope_context["KERNEL_SCOPE_CONTEXT_VIEW_JSON"]
    )
    assert context_view["schema_version"] == "context-view.v1"
    assert any(item["key"] == "project" for item in context_view["roots"])
    assert "CAUSAL_FIRST" in harness.requests[0].prompt
    assert "CAUSAL_SECOND" not in harness.requests[0].prompt

    worker = str(uuid.uuid4())
    claimed_second = await repository.claim_run("test", worker, 60)
    assert claimed_second["run_id"] == second["run"]["run_id"]
    await processor.process(claimed_second, worker)
    assert "CAUSAL_SECOND" in harness.requests[1].prompt
    assert "FIRST_RESULT" not in harness.requests[1].prompt
    assert "KERNEL_UI_ONLY" not in harness.requests[1].prompt


async def test_question_answer_requeues_same_run_and_resumes_session(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    inbound = await repository.ingest_message(
        project_id="demo",
        agent_key="project:demo:manager",
        channel="feishu",
        chat_id="chat-1",
        user_id="user-1",
        external_event_id="app:event-question",
        external_message_id="external-question",
        content={
            "text": "build it",
            "document_ref": "vault://demo/requirements.md",
            "raw": {"transport_secret": "initial-secret"},
        },
    )
    harness = FakeHarness(
        [
            (
                json.dumps(
                    {
                        "outcome": "needs_input",
                        "summary": "need one answer",
                        "questions": [
                            {
                                "question_id": "color",
                                "question_text": "Which color should be used?",
                                "required": True,
                            }
                        ],
                    }
                ),
                10,
                "native-1",
            ),
            ("finished", 20, "native-1"),
        ]
    )
    processor = RunProcessor(core_settings, repository, harness)
    first_worker = str(uuid.uuid4())
    run = await repository.claim_run("test", first_worker, 60)
    waiting = await processor.process(run, first_worker)
    waiting_run = await repository.get_run(str(run["run_id"]))
    assert waiting_run["status"] == "waiting_user"
    assert "initial-secret" not in harness.requests[0].prompt
    assert "vault://demo/requirements.md" in harness.requests[0].prompt

    answer = await repository.ingest_message(
        project_id="demo",
        agent_key="project:demo:manager",
        channel="feishu",
        chat_id="chat-1",
        user_id="user-1",
        external_event_id="app:event-answer",
        external_message_id="external-answer",
        content={"text": "blue", "raw": {"transport_secret": "answer-secret"}},
        message_type="answer",
        reply_to_message_id=waiting["question_message_id"],
    )
    assert answer["resumed"] is True
    assert str(answer["run"]["run_id"]) == str(run["run_id"])

    second_worker = str(uuid.uuid4())
    resumed = await repository.claim_run("test", second_worker, 60)
    await processor.process(resumed, second_worker)
    assert harness.requests[1].physical_session_id == "native-1"
    assert "Which color should be used?" in harness.requests[1].prompt
    assert '"text": "blue"' in harness.requests[1].prompt
    assert "请回复本消息" not in harness.requests[1].prompt
    assert "answer-secret" not in harness.requests[1].prompt
    assert "correlation" not in harness.requests[1].prompt
    assert (await repository.get_run(str(run["run_id"])))["status"] == "succeeded"


async def test_two_question_rounds_use_distinct_messages(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    inbound = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="rounds:initial",
        external_message_id="initial", content={"text": "start"},
    )
    responses = []
    for number in (1, 2):
        responses.append((json.dumps({
            "outcome": "needs_input", "summary": f"round {number}",
            "questions": [{
                "question_id": f"q{number}", "question_text": f"Question {number}?",
                "required": True,
            }],
        }), 10 * number, "native-1"))
    responses.append(("done", 30, "native-1"))
    processor = RunProcessor(core_settings, repository, FakeHarness(responses))
    run_id = str(inbound["run"]["run_id"])
    question_ids = []
    for number in (1, 2):
        worker = str(uuid.uuid4())
        claimed = await repository.claim_run("test", worker, 60)
        waiting = await processor.process(claimed, worker)
        question_ids.append(waiting["question_message_id"])
        answer = await repository.ingest_message(
            project_id="demo", agent_key="project:demo:manager", channel="feishu",
            chat_id="chat-1", user_id="user-1",
            external_event_id=f"rounds:answer:{number}", external_message_id=f"answer-{number}",
            content={"text": f"answer {number}"}, message_type="answer",
            reply_to_message_id=waiting["question_message_id"],
        )
        assert answer["resumed"] is True
    assert question_ids[0] != question_ids[1]
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    await processor.process(claimed, worker)
    assert (await repository.get_run(run_id))["status"] == "succeeded"


async def test_stale_answer_does_not_create_a_new_run(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="stale:initial",
        external_message_id="initial", content={"text": "hello"},
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    completed = await RunProcessor(
        core_settings, repository, FakeHarness([("done", 1, "native")])
    ).process(claimed, worker)
    result = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="stale:answer",
        external_message_id="late", content={"text": "late"}, message_type="answer",
        reply_to_message_id=completed["reply_message_id"],
    )
    assert result["stale_reply"] is True
    assert result["run"] is None
    assert len(await repository.list_runs("demo")) == 1


async def test_later_answer_to_consumed_question_creates_pm_correction_run(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="correction:initial",
        external_message_id="initial", content={"text": "start"},
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(json.dumps({
            "outcome": "needs_input", "summary": "need answer",
            "questions": [{
                "question_id": "q1", "question_text": "Choose?", "required": True,
            }],
        }), 1, "native")])
    ).process(claimed, worker)
    first = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="correction:first",
        external_message_id="first", content={"text": "wrong"}, message_type="answer",
        reply_to_message_id=waiting["question_message_id"],
    )
    assert first["resumed"] is True
    corrected = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="correction:second",
        external_message_id="second", content={"text": "correct"}, message_type="answer",
        reply_to_message_id=waiting["question_message_id"],
    )
    assert corrected["stale_reply"] is True
    assert corrected["correction_run"] is True
    assert corrected["run"]["agent_key"] == "project:demo:manager"
    assert str(corrected["run"]["parent_run_id"]) == str(first["run"]["run_id"])
    assert corrected["run"]["request_json"]["correction"] == {
        "kind": "stale_question_answer",
        "original_run_id": str(first["run"]["run_id"]),
        "original_run_status": "queued",
        "question_message_id": waiting["question_message_id"],
        "question_id": "q1",
    }


async def test_structured_partial_answers_remain_waiting(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="partial:initial",
        external_message_id="initial", content={"text": "start"},
    )
    response = json.dumps({
        "outcome": "needs_input", "summary": "two answers",
        "questions": [
            {"question_id": "one", "question_text": "One?", "required": True},
            {"question_id": "two", "question_text": "Two?", "required": True},
        ],
    })
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "native")])
    ).process(run, worker)
    assert len(waiting["question_message_ids"]) == 2
    first_question = await repository.get_message(waiting["question_message_ids"][0])
    second_question = await repository.get_message(waiting["question_message_ids"][1])
    assert first_question["content_json"]["question_id"] == "one"
    assert second_question["content_json"]["question_id"] == "two"
    assert first_question["content_json"]["text"] == (
        "two answers\n\nOne?\n\n有多个待答问题，请回复对应消息。"
    )
    first = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="partial:one",
        external_message_id="one", content={"text": "A"},
        message_type="answer", reply_to_message_id=waiting["question_message_ids"][0],
    )
    assert first["partial_answer"] is True
    assert first["run"]["status"] == "waiting_user"
    second = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="partial:two",
        external_message_id="two", content={"text": "B"},
        message_type="answer", reply_to_message_id=waiting["question_message_ids"][1],
    )
    assert second["resumed"] is True
    assert second["run"]["request_json"]["answer"] == {
        "answers": {"one": "A", "two": "B"}
    }


async def test_feishu_native_replies_route_each_question_without_channel_binding(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="reply-route:initial",
        external_message_id="initial", content={"text": "start"},
    )
    response = json.dumps({
        "outcome": "needs_input", "summary": "two answers",
        "questions": [
            {"question_id": "one", "question_text": "One?", "required": True},
            {"question_id": "two", "question_text": "Two?", "required": True},
        ],
    })
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "native")])
    ).process(run, worker)
    external_by_question = {"one": "feishu-q-one", "two": "feishu-q-two"}
    for _ in waiting["question_message_ids"]:
        delivery_worker = str(uuid.uuid4())
        outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
        question_id = outbound["content_json"]["question_id"]
        assert await repository.acknowledge_delivery(
            str(outbound["message_id"]), delivery_worker, external_by_question[question_id]
        )

    ambiguous = await ingest_event(core_settings, {
        "header": {"event_id": "reply-route:ambiguous"},
        "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "message_id": "ambiguous-answer",
                "content": json.dumps({"text": "A"}),
            },
        },
    }, "app-1", allow_explicit_route=True)
    assert ambiguous["message"]["message_type"] == "text"
    assert ambiguous["resumed"] is False
    assert str(ambiguous["run"]["run_id"]) != str(run["run_id"])

    def reply_event(event_id: str, incoming_id: str, parent_id: str, text: str) -> dict:
        return {
            "header": {"event_id": event_id},
            "event": {
                "sender": {"sender_id": {"open_id": "user-1"}},
                "message": {
                    "chat_id": "chat-1", "message_id": incoming_id,
                    "parent_id": parent_id, "root_id": parent_id,
                    "content": json.dumps({"text": text}),
                },
            },
        }

    second = await ingest_event(
        core_settings, reply_event("reply-route:two", "answer-two", "feishu-q-two", "B"),
        "app-1",
    )
    assert second["message"]["project_id"] == "demo"
    assert second["partial_answer"] is True
    first = await ingest_event(
        core_settings, reply_event("reply-route:one", "answer-one", "feishu-q-one", "A"),
        "app-1",
    )
    assert first["resumed"] is True
    assert first["run"]["request_json"]["answer"] == {
        "answers": {"two": "B", "one": "A"}
    }
    assert await repository.list_runs("system") == []


async def test_feishu_plain_message_continues_one_waiting_question(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    inbound = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="direct:initial",
        external_message_id="initial", content={"text": "start"},
    )
    response = json.dumps({
        "outcome": "needs_input", "summary": "one answer",
        "questions": [{
            "question_id": "color", "question_text": "Which color?", "required": True,
        }],
    })
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "native")])
    ).process(run, worker)
    question = await repository.get_message(waiting["question_message_id"])
    assert question["content_json"]["text"] == "one answer\n\nWhich color?"
    delivery_worker = str(uuid.uuid4())
    outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
    assert await repository.acknowledge_delivery(
        str(outbound["message_id"]), delivery_worker, "feishu-direct-question"
    )

    event = {
        "header": {"event_id": "direct:answer"},
        "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "message_id": "direct-answer",
                "message_type": "post",
                "content": json.dumps({
                    "title": "", "content": [[{"tag": "text", "text": "blue"}]],
                    "content_v2": [[{"tag": "text", "text": "blue"}]],
                }),
            },
        },
    }
    answered = await ingest_event(
        core_settings, event, "app-1", allow_explicit_route=True
    )
    assert answered["resumed"] is True
    assert answered["message"]["message_type"] == "answer"
    assert str(answered["message"]["reply_to_message_id"]) == waiting["question_message_id"]
    assert str(answered["run"]["run_id"]) == str(inbound["run"]["run_id"])
    duplicate = await ingest_event(
        core_settings, event, "app-1", allow_explicit_route=True
    )
    assert duplicate["duplicate"] is True


async def test_conversation_reset_detaches_waiting_question_and_cuts_prompt_history(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    initial = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="reset:initial",
        external_message_id="initial", content={"text": "old discussion"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "old summary",
                "questions": [{
                    "question_id": "old-question",
                    "question_text": "Old question?", "required": True,
                }],
            }),
            10, "old-native",
        )]),
    ).process(run, worker)
    reset = await repository.reset_conversation(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(initial["message"]["message_id"]),
        idempotency_key="conversation-reset:test",
    )
    assert reset["sealed_session_ids"]
    assert (await repository.get_run(str(run["run_id"])))["status"] == "waiting_user"

    fresh = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="reset:fresh",
        external_message_id="fresh", content={"text": "new discussion"},
        auto_reply_to_unambiguous=True,
    )
    assert fresh["resumed"] is False
    assert fresh["message"]["message_type"] == "text"
    assert str(fresh["run"]["run_id"]) != str(run["run_id"])
    history = await repository.list_messages_through(
        "demo", "feishu", "chat-1", str(fresh["message"]["message_id"]), limit=20
    )
    history_ids = {str(item["message_id"]) for item in history}
    assert waiting["question_message_id"] not in history_ids


async def test_old_question_text_reply_needs_resurfaced_message_after_reset(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    initial = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="boundary:initial",
        external_message_id="initial", content={"text": "choose"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(core_settings, repository, FakeHarness([(
        json.dumps({"outcome": "needs_input", "summary": "choose", "questions": [{
            "question_id": "path", "question_text": "Which path?", "required": True,
        }]}), 10, "boundary-native",
    )])).process(run, worker)
    await repository.reset_conversation(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(initial["message"]["message_id"]),
        idempotency_key="boundary:reset",
    )
    old = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="boundary:old",
        external_message_id="old", content={"text": "old answer"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert old["resumed"] is False
    assert old["run"] is None
    assert len(await repository.list_runs("demo")) == 1
    async with repository.database.connection() as connection:
        receipt = await (await connection.execute(
            "SELECT content_json FROM assistant.messages WHERE idempotency_key=%s",
            (f"expired-question-reply:{old['message']['message_id']}",),
        )).fetchone()
    assert f"/resume {run['run_id']}" in receipt["content_json"]["text"]
    assert (await repository.get_run(str(run["run_id"])))["status"] == "waiting_user"
    resurfaced = await repository.resume_waiting_run(
        run_id=str(run["run_id"]), project_id="demo", channel="feishu",
        chat_id="chat-1", user_id="user-1",
        source_message_id=str(initial["message"]["message_id"]),
        idempotency_key="boundary:resume",
    )
    old_again = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="boundary:old-again",
        external_message_id="old-again", content={"text": "old answer"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert old_again["resumed"] is False
    assert old_again["run"] is None
    assert len(await repository.list_runs("demo")) == 1
    fresh = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="boundary:fresh",
        external_message_id="fresh", content={"text": "fresh answer"},
        message_type="answer",
        reply_to_message_id=str(resurfaced["message"]["message_id"]),
    )
    assert fresh["resumed"] is True
    assert (await repository.get_run(str(run["run_id"])))["status"] == "queued"


async def test_conversation_reset_cancels_running_front_run_without_retry(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    initial = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="reset:running",
        external_message_id="running", content={"text": "work"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    reset = await repository.reset_conversation(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(initial["message"]["message_id"]),
        idempotency_key="conversation-reset:running",
    )
    assert str(run["run_id"]) in reset["cancel_requested_run_ids"]
    status = await repository.retry_run(
        str(run["run_id"]), worker, str(run["claim_token"]),
        {"type": "Cancelled", "message": "reset"},
    )
    assert status == "cancelled"


async def test_feishu_goal_and_new_commands_bypass_reply_correlation(core_settings):
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)

    def command_event(event_id: str, message_id: str, text: str) -> dict:
        return {
            "header": {"event_id": event_id},
            "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
            "event": {
                "sender": {"sender_id": {"open_id": "user-1"}},
                "message": {
                    "chat_id": "chat-1", "chat_type": "p2p",
                    "message_id": message_id, "parent_id": "unknown-old-message",
                    "content": json.dumps({"text": text}),
                },
            },
        }

    goal = await ingest_event(
        core_settings, command_event("command:goal", "goal-message", "/goal build safely"),
        "app-1", allow_explicit_route=True,
    )
    assert goal["command"] == "/goal"
    assert goal["run"] is None
    assert (await repository.current_conversation_goal(
        "demo", "feishu", "chat-1"
    ))["goal"] == "build safely"

    reset = await ingest_event(
        core_settings, command_event("command:new", "new-message", "/new"),
        "app-1", allow_explicit_route=True,
    )
    assert reset["command"] == "/new"
    assert reset["run"] is None
    assert await repository.current_conversation_goal("demo", "feishu", "chat-1") is None


async def test_status_command_is_read_only_and_scoped_to_exact_conversation(core_settings):
    prepare_bindable_project(core_settings)
    repository = CoreRepository(core_settings)
    visible = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="status:visible",
        external_message_id="visible", content={"text": "work"},
    )
    hidden = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-2", user_id="user-2", external_event_id="status:hidden",
        external_message_id="hidden", content={"text": "other work"},
    )
    session = await repository.ensure_session(
        "demo", "project:demo:manager", "feishu", "chat-1",
    )
    await repository.record_session_turn(
        str(session["session_id"]), "native-status-session", 250, 1000, None, None,
    )
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.sessions SET usage_known=true,"
            "usage_source='native_last_token_usage' WHERE session_id=%s",
            (session["session_id"],),
        )
        await connection.execute(
            "UPDATE assistant.runs SET session_id=%s,executor_json=%s WHERE run_id=%s",
            (
                session["session_id"],
                Jsonb({
                    "harness": "codex_cli", "provider": "openai",
                    "model": "gpt-test", "reasoning_effort": "medium",
                    "context_policy": "max",
                    "usage_history": [{
                        "input_tokens": 250, "cached_input_tokens": 200,
                        "output_tokens": 30, "reasoning_output_tokens": 10,
                    }],
                }),
                visible["run"]["run_id"],
            ),
        )
        await connection.commit()
    payload = {
        "header": {"event_id": "command:status"},
        "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "chat_type": "p2p",
                "message_id": "status-message",
                "content": json.dumps({"text": "/status"}),
            },
        },
    }
    result = await ingest_event(
        core_settings, payload, "app-1", allow_explicit_route=True,
    )
    text = str(result["outbound"]["content_json"]["text"])
    assert result["command"] == "/status"
    assert result["run"] is None
    assert result["outbound"]["content_json"]["origin"] == "kernel"
    assert result["outbound"]["content_json"]["context_visibility"] == "ui_only"
    assert message_context_visibility(result["outbound"]) == "ui_only"
    assert "当前 Session 状态" in text
    assert f"Kernel Session ID：{session['session_id']}" in text
    assert f"Logical Session ID：{session['logical_session_id']}" in text
    assert "Codex Session ID：native-status-session" in text
    assert "Harness：codex_cli" in text
    assert "Provider：openai" in text
    assert "Model：gpt-test" in text
    assert "Reasoning：medium" in text
    assert "Context Policy：max" in text
    assert "最近请求占用 250 / 1,000 tokens (25.0%)" in text
    assert "估算余额 750 tokens (75.0%)" in text
    assert "最近 Turn Usage：input 250 · cached 200 · output 30 · reasoning 10" in text
    assert "最近 Run" not in text

    kernel_payload = {
        "header": {"event_id": "command:kernel-status"},
        "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "chat_type": "p2p",
                "message_id": "kernel-status-message",
                "content": json.dumps({"text": "/kernel-status"}),
            },
        },
    }
    kernel_result = await ingest_event(
        core_settings, kernel_payload, "app-1", allow_explicit_route=True,
    )
    kernel_text = str(kernel_result["outbound"]["content_json"]["text"])
    assert kernel_result["command"] == "/kernel-status"
    assert kernel_result["run"] is None
    assert "当前 Kernel 状态" in kernel_text
    assert str(visible["run"]["run_id"])[:8] in kernel_text
    assert str(hidden["run"]["run_id"])[:8] not in kernel_text
    assert "queued" in kernel_text
    assert "Kernel Session ID" not in kernel_text


async def test_goal_is_scoped_to_current_conversation_generation(core_settings):
    repository = CoreRepository(core_settings)
    await repository.record_conversation_goal(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(uuid.uuid4()), goal="old goal",
        idempotency_key="goal:old",
    )
    assert (await repository.current_conversation_goal(
        "demo", "feishu", "chat-1"
    ))["goal"] == "old goal"
    await repository.reset_conversation(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(uuid.uuid4()), idempotency_key="goal:reset",
    )
    assert await repository.current_conversation_goal("demo", "feishu", "chat-1") is None
    await repository.record_conversation_goal(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(uuid.uuid4()), goal="new goal",
        idempotency_key="goal:new",
    )
    assert (await repository.current_conversation_goal(
        "demo", "feishu", "chat-1"
    ))["goal"] == "new goal"


async def test_resume_resurfaces_old_waiting_question_after_reset(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    initial = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="resume:initial",
        external_message_id="initial", content={"text": "old"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "saved summary",
                "questions": [{
                    "question_id": "saved", "question_text": "Saved question?",
                    "required": True,
                }],
            }),
            10, "saved-native",
        )]),
    ).process(run, worker)
    await repository.reset_conversation(
        project_id="demo", channel="feishu", chat_id="chat-1", user_id="user-1",
        source_message_id=str(initial["message"]["message_id"]),
        idempotency_key="resume:reset",
    )
    pending = await repository.list_pending_channel_runs(
        project_id="demo", channel="feishu", chat_id="chat-1",
        user_id="user-1",
    )
    assert [item["run_id"] for item in pending] == [str(run["run_id"])]
    assert pending[0]["question_content"]["question_text"] == "Saved question?"
    with pytest.raises(PermissionError, match="用户"):
        await repository.resume_waiting_run(
            run_id=str(run["run_id"]), project_id="demo", channel="feishu",
            chat_id="chat-1", user_id="another-user",
            source_message_id=str(uuid.uuid4()), idempotency_key="resume:wrong-user",
        )
    with pytest.raises(PermissionError, match="expired"):
        await repository.validate_current_waiting_message(
            message_id=waiting["question_message_id"], project_id="demo",
            channel="feishu", chat_id="chat-1", user_id="user-1",
        )
    command = await repository.record_channel_command(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="resume:command",
        external_message_id="resume-command", content={"text": "/resume"},
    )
    resumed = await repository.resume_waiting_run(
        run_id=str(run["run_id"]), project_id="demo", channel="feishu",
        chat_id="chat-1", user_id="user-1",
        source_message_id=str(command["message"]["message_id"]),
        idempotency_key="resume:question",
    )
    assert resumed["message"]["content_json"]["resumed_from_message_id"] == (
        waiting["question_message_id"]
    )
    assert (await repository.get_run(str(run["run_id"])))[
        "waiting_question_message_id"
    ] == resumed["message"]["message_id"]


async def test_approval_feedback_continues_without_a_fixed_phrase(core_settings):
    prepare_bindable_project(core_settings)
    prepare_v2_stage(core_settings, "demo", "implementation")
    prepare_v2_stage(core_settings, "demo", "review")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="feedback:initial",
        external_message_id="initial", content={"text": "start"},
    )
    response = json.dumps({
        "outcome": "needs_approval", "summary": "ready",
        "document_markdown": "# Proposal", "approval_question": "Approve it?",
        "work_order": {
            "schema_version": "work-order.v1", "package_id": "feedback-plan",
            "goal": "Apply the approved bounded proposal",
            "target_uris": ["workspace://project/proposal.md"],
            "acceptance_criteria": ["proposal feedback is incorporated"],
            "non_goals": [], "requested_skills": [], "risk_class": "medium",
        },
        "artifacts": [],
    })
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "native")])
    ).process(run, worker)
    approval = await repository.get_message(waiting["question_message_id"])
    assert "请选择审批卡片操作" in approval["content_json"]["text"]
    delivery_worker = str(uuid.uuid4())
    outbound = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
    assert await repository.acknowledge_delivery(
        str(outbound["message_id"]), delivery_worker, "feishu-feedback"
    )
    ordinary = await repository.create_outbound(
        project_id="demo", session_id=None, run_id=None, channel="feishu",
        chat_id="chat-1", user_id="user-1", message_type="reply",
        content={"text": "ordinary assistant message"},
        reply_to_message_id=None, idempotency_key="feedback:ordinary",
    )
    ordinary_worker = str(uuid.uuid4())
    claimed_ordinary = await repository.claim_delivery(
        "sender", ordinary_worker, 60, "feishu"
    )
    assert str(claimed_ordinary["message_id"]) == str(ordinary["message_id"])
    assert await repository.acknowledge_delivery(
        str(ordinary["message_id"]), ordinary_worker, "feishu-ordinary-reply"
    )
    feedback = await ingest_event(core_settings, {
        "header": {"event_id": "feedback:details"},
        "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "message_id": "feedback-details",
                "parent_id": "feishu-ordinary-reply",
                "root_id": "feishu-ordinary-reply",
                "content": json.dumps({"text": "请按这些详细意见继续调整方案"}),
            },
        },
    }, "app-1", allow_explicit_route=True)
    assert feedback["resumed"] is True
    assert feedback["approval_decision"] == "revise"
    assert feedback["message"]["message_type"] == "answer"
    resumed = await repository.get_run(str(run["run_id"]))
    assert resumed["status"] == "queued"
    assert resumed["request_json"]["answer"] == {
        "text": "请按这些详细意见继续调整方案"
    }


async def test_legacy_manager_approval_does_not_require_task_v1_work_order(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="legacy:initial",
        external_message_id="initial", content={"text": "legacy maintenance"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_approval", "summary": "legacy approval",
                "document_markdown": "# Legacy maintenance",
                "approval_question": "Approve legacy maintenance?", "artifacts": [],
            }),
            10, "legacy-native",
        )]),
    ).process(run, worker)
    assert waiting["outcome"] == "needs_approval"
    pinned = await repository.get_run(str(run["run_id"]))
    assert pinned["request_json"].get("task_protocol") is None


async def test_stage_approval_creates_next_stage(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    prepare_stage(core_settings, "demo", "implementation")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="task:initial",
        external_message_id="initial", content={"text": "build"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    task = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="build safely",
        stages=["requirements", "implementation"], change_class="major",
        task_metadata={
            "stage_bindings": {
                "implementation": {"config_snapshot_uri": "vault://demo/implementation.json"},
            }
        },
    )
    assert task["stage"] == "requirements"
    assert await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded", result={"task_run_id": str(task["run_id"])},
    )
    stage_worker = str(uuid.uuid4())
    stage_run = await repository.claim_run("test", stage_worker, 60)
    response = json.dumps({
        "outcome": "needs_approval", "summary": "requirements ready",
        "document_markdown": "# Requirements\n\nVerified requirements.",
        "approval_question": "Approve these requirements?", "artifacts": [],
    })
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "requirements-native")])
    ).process(stage_run, stage_worker)
    approved = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:requirements", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="task:approval",
        external_message_id="approval", content={"decision": "approve"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert approved["approval_decision"] == "approve"
    assert approved["run"]["stage"] == "implementation"
    assert approved["run"]["status"] == "queued"
    assert approved["run"]["executor_json"]["config_snapshot_uri"] == (
        "vault://demo/implementation.json"
    )
    assert approved["run"]["request_json"]["requirements_approval"][
        "requirements_run_id"
    ] == str(stage_run["run_id"])
    async with repository.database.connection() as connection:
        receipt = await (
            await connection.execute(
                "SELECT content_json FROM assistant.messages "
                "WHERE message_type='kernel_notice' AND run_id=%s",
                (approved["run"]["run_id"],),
            )
        ).fetchone()
    assert "已创建下一阶段 implementation" in receipt["content_json"]["text"]


async def test_project_agent_plan_approval_creates_one_candidate_only_build(core_settings):
    prepare_bindable_project(core_settings)
    prepare_v2_stage(core_settings, "demo", "implementation")
    prepare_v2_stage(core_settings, "demo", "review")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="plan:initial",
        external_message_id="initial", content={"text": "prepare one package"},
    )
    worker = str(uuid.uuid4())
    plan = await repository.claim_run("test", worker, 60)
    response = json.dumps({
        "outcome": "needs_approval",
        "summary": "one bounded package",
        "document_markdown": "# Work Order\n\nCandidate-only change.",
        "approval_question": "Approve this package?",
        "work_order": {
            "schema_version": "work-order.v1",
            "package_id": "pkg-1",
            "goal": "Update one module",
            "target_uris": ["workspace://project/module.py"],
            "acceptance_criteria": ["unit test passes"],
            "non_goals": ["no deployment"],
            "requested_skills": [],
            "risk_class": "high",
        },
        "artifacts": [],
    })
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "plan-native")])
    ).process(plan, worker)
    pinned = await repository.get_run(str(plan["run_id"]))
    assert pinned["request_json"]["task_protocol"] == "task.v1"
    assert pinned["request_json"]["stage_index"] == -1
    assert pinned["request_json"]["candidate_only"] is True
    approved = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="plan:approve",
        external_message_id="approve", content={"decision": "approve"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    build = approved["run"]
    assert build["stage"] == "implementation"
    assert build["request_json"]["stage_index"] == 0
    assert build["request_json"]["candidate_only"] is True
    assert build["request_json"]["work_order_approval"]["artifact_sha256"]


def test_work_order_is_bounded_and_task_status_phrases_are_deterministic():
    valid = normalize_work_order({
        "schema_version": "work-order.v1", "package_id": "one", "goal": "g",
        "target_uris": ["workspace://project/a.py"],
        "acceptance_criteria": ["passes"], "risk_class": "medium",
    })
    assert valid["package_id"] == "one"
    with pytest.raises(ValueError, match="one and six"):
        normalize_work_order({
            **valid,
            "target_uris": [f"workspace://project/{index}.py" for index in range(7)],
        })
    assert parse_slash_command("请继续完成任务") == ("/task-status", None)


async def test_new_requirements_task_can_supersede_queued_stage(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="supersede:initial",
        external_message_id="initial", content={"text": "start"},
    )
    worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", worker, 60)
    first = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=worker,
        claim_token=str(manager["claim_token"]), goal="old requirements",
        stages=["requirements"], change_class="major",
    )
    replacement = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=worker,
        claim_token=str(manager["claim_token"]), goal="revised requirements",
        stages=["requirements"], change_class="major",
        supersede_run_id=str(first["run_id"]),
        supersede_reason="user changed the approved safety boundary",
    )
    old = await repository.get_run(str(first["run_id"]))
    assert old["status"] == "cancelled"
    assert old["executor_json"]["supersession"]["replacement_run_id"] == str(
        replacement["run_id"]
    )
    assert replacement["request_json"]["supersedes_run_id"] == str(first["run_id"])


async def test_project_rejects_second_open_execution_task(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "implementation")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="single:initial",
        external_message_id="initial", content={"text": "start"},
    )
    worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", worker, 60)
    first = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=worker,
        claim_token=str(manager["claim_token"]), goal="first",
        stages=["implementation"], change_class="minor",
    )
    assert first["status"] == "queued"
    with pytest.raises(RuntimeError, match="already has an open execution task"):
        await repository.start_task_from_claim(
            source_run_id=str(manager["run_id"]), worker_instance_id=worker,
            claim_token=str(manager["claim_token"]), goal="duplicate",
            stages=["implementation"], change_class="minor",
        )


async def test_natural_continue_returns_task_status_without_an_llm_run(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "implementation")
    repository = CoreRepository(core_settings)
    initial = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="continue:initial",
        external_message_id="initial", content={"text": "start"},
    )
    worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", worker, 60)
    task = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=worker,
        claim_token=str(manager["claim_token"]), goal="one package",
        stages=["implementation"], change_class="minor",
    )
    assert await repository.finish_run(
        str(manager["run_id"]), worker, str(manager["claim_token"]),
        status="succeeded",
    )
    result = await ingest_event(core_settings, {
        "header": {"event_id": "continue:status"},
        "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {"chat_id": "chat-1", "chat_type": "p2p",
                        "message_id": "continue-status", "content": json.dumps({
                            "text": "请继续完成任务",
                        })},
        },
    }, "app-1", allow_explicit_route=True)
    assert result["command"] == "/task-status"
    assert result["run"] is None
    assert str(task["run_id"]) in result["outbound"]["content_json"]["text"]
    async with repository.database.connection() as connection:
        count = await (await connection.execute(
            "SELECT count(*) AS count FROM assistant.runs WHERE project_id='demo'"
        )).fetchone()
    assert int(count["count"]) == 2


async def test_single_agent_task_requeues_same_run_with_hash_chained_pack(core_settings):
    prepare_bindable_project(core_settings)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["task_protocol"] = "single-agent.v2"
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    repository = CoreRepository(core_settings)
    created = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="single-agent:initial",
        external_message_id="initial", content={"text": "finish one task end to end"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    response = json.dumps({
        "outcome": "continue", "summary": "baseline inspected; continue candidate",
        "task_checkpoint": {
            "goal": "finish one task end to end",
            "completed": ["baseline inspected"], "pending": ["write candidate"],
            "decisions": [], "constraints": ["candidate only"],
            "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "write candidate",
        },
        "artifacts": [],
    })
    result = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "single-native")])
    ).process(run, worker)
    assert result["outcome"] == "continue"
    assert result["run_id"] == str(run["run_id"])
    current = await repository.get_run(str(run["run_id"]))
    assert current["status"] == "queued"
    assert current["request_json"]["task_protocol"] == "single-agent.v2"
    assert current["request_json"]["task_turn_count"] == 1
    assert current["request_json"]["candidate_only"] is True


    task_session = await repository.current_session(
        "demo", "project:demo:manager", "discord",
        f"chat-1:task:{run['run_id']}",
    )
    assert task_session is not None
    accepted = current["request_json"]["accepted_documents"]
    assert accepted["task_contract"]["document_schema"] == "task-contract.v1"
    assert accepted["last_turn"]["document_schema"] == "turn-record.v1"
    assert not (root / "runs" / str(run["run_id"]) / "candidate").exists()
    busy = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="single-agent:busy",
        external_message_id="busy", content={"text": "another instruction"},
    )
    assert busy["run"] is None
    assert str(busy["active_task"]["run_id"]) == str(run["run_id"])
    assert "不会中断或创建新Task" in busy["outbound"]["content_json"]["text"]
    async with repository.database.connection() as connection:
        count = await (await connection.execute(
            "SELECT count(*) AS count FROM assistant.runs WHERE project_id='demo'"
        )).fetchone()
    assert int(count["count"]) == 1
    claimed_again = await repository.claim_run("test", str(uuid.uuid4()), 60)
    assert str(claimed_again["run_id"]) == str(run["run_id"])


async def test_single_agent_turn_queues_supervisor_evaluation_run(core_settings):
    """v6.1: every settled PM Turn queues a Supervisor consultation Run.

    Disabled Projects must not queue anything (the default path above)."""
    prepare_bindable_project(core_settings)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["task_protocol"] = "single-agent.v2"
    project["supervisor"] = {"enabled": True}
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="sup-eval:initial",
        external_message_id="initial", content={"text": "finish one task"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    response = json.dumps({
        "outcome": "continue", "summary": "turn settled",
        "task_checkpoint": {
            "goal": "finish one task", "completed": ["inspected"],
            "pending": ["build"], "decisions": [], "constraints": [],
            "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "build",
        },
        "artifacts": [],
    })
    await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "sup-native")])
    ).process(run, worker)
    runs = {
        str(item["run_id"]): item
        for item in await repository.list_runs("demo", limit=20)
    }
    consultations = [
        item for item in runs.values()
        if (item["request_json"] or {}).get("interaction_kind")
        == "supervisor_consultation"
    ]
    assert len(consultations) == 1
    consultation = consultations[0]
    assert consultation["agent_key"] == "project:demo:supervisor"
    assert consultation["run_type"] == "chat"
    assert consultation["status"] == "queued"
    assert consultation["executor_json"]["scheduling_lane"] == "conversation"
    request = consultation["request_json"]
    assert request["trigger"] == "after_agent_turn"
    assert request["task_id"] == str(run["run_id"])
    assert request["turn_number"] == 1
    assert request["agent_turn_report"]["outcome"] == "continue"
    assert consultation["idempotency_key"] == (
        f"supervisor-eval:{run['run_id']}:1"
    )


async def test_supervisor_message_delta_accepts_empty_and_timestamp_watermarks(
    core_settings,
):
    repository = CoreRepository(core_settings)
    created = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="delta:initial",
        external_message_id="delta:initial", content={"text": "讨论策略意图"},
    )
    rows = await repository.list_messages_since(
        "demo", ["discord", "feishu"], since=None,
    )
    assert [item["message_id"] for item in rows] == [
        str(created["message"]["message_id"])
    ]
    after = await repository.list_messages_since(
        "demo", ["discord"], since=str(rows[0]["created_at"]),
    )
    assert after == []


@pytest.mark.parametrize("requirements_decision", ["approve", "revise"])
async def test_single_agent_requirements_approval_switches_same_task_to_build(
    core_settings, requirements_decision,
):
    prepare_bindable_project(core_settings)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["task_protocol"] = "single-agent.v2"
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="requirements:initial",
        external_message_id="initial", content={"text": "define and build one rule"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    response = json.dumps({
        "outcome": "needs_approval", "summary": "requirements are complete",
        "document_markdown": "# Requirements\n\n- Rule is explicit.",
        "approval_question": "Approve Requirements and enter Build?",
        "task_checkpoint": {
            "goal": "define and build one rule", "completed": ["requirements"],
            "pending": ["build"], "decisions": [], "constraints": [],
            "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "wait for requirements approval",
        }, "artifacts": [],
    })
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "requirements-native")])
    ).process(run, worker)
    assert waiting["outcome"] == "needs_approval"
    current = await repository.get_run(str(run["run_id"]))
    assert current["status"] == "waiting_user"
    assert current["request_json"]["approval_kind"] == "requirements"
    contract_ref = current["request_json"]["accepted_documents"]["task_contract"]
    contract = TaskDocumentService(core_settings).read("demo", contract_ref)
    assert contract["status"] == "ready"
    assert contract["payload"]["requirements"] == [
        "# Requirements\n\n- Rule is explicit."
    ]
    approved = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="requirements:approve",
        external_message_id=requirements_decision,
        content={"decision": requirements_decision, "text": "add one more requirement"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert str(approved["run"]["run_id"]) == str(run["run_id"])
    assert approved["run"]["status"] == "queued"
    if requirements_decision == "revise":
        assert approved["run"]["request_json"]["current_stage"] == "requirements"
        assert approved["run"]["request_json"]["requirements_approval"] is None
        assert (
            approved["run"]["request_json"]["accepted_documents"]["task_contract"]
            == contract_ref
        )
        return
    assert approved["run"]["request_json"]["current_stage"] == "build"
    approval = approved["run"]["request_json"]["requirements_approval"]
    assert approval["task_contract_ref"] == contract_ref
    assert approval["approved_document_sha256"] == contract_ref["sha256"]


async def test_single_agent_build_returns_to_requirements_with_fresh_session(core_settings):
    prepare_bindable_project(core_settings)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["task_protocol"] = "single-agent.v2"
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="requirements:return",
        external_message_id="initial", content={"text": "change the approved rule"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    run = await prepare_single_agent_build_run(
        core_settings, repository, run, worker, "build one rule"
    )
    returned = json.dumps({
        "outcome": "return_to_requirements",
        "summary": "the approved entry rule must change",
        "document_markdown": "# Requirements\n\n- Use the revised entry rule.",
        "task_checkpoint": {
            "goal": "build one rule", "completed": [],
            "pending": ["requirements revision"], "decisions": [],
            "constraints": [], "external_effects": [], "artifacts": [],
            "open_questions": [], "next_action": "revise and re-approve requirements",
        },
        "artifacts": [],
    })
    waiting = json.dumps({
        "outcome": "needs_input", "summary": "one detail remains",
        "questions": [{
            "question_id": "revised-threshold", "question_text": "Which threshold?",
            "required": True,
        }],
        "task_checkpoint": {
            "goal": "build one rule", "completed": [],
            "pending": ["requirements revision"], "decisions": [],
            "constraints": [], "external_effects": [], "artifacts": [],
            "open_questions": ["revised-threshold"], "next_action": "wait for threshold",
        },
        "artifacts": [],
    })
    harness = FakeHarness([
        (returned, 10, "build-native"),
        (waiting, 10, "requirements-native"),
    ])
    result = await RunProcessor(core_settings, repository, harness).process(run, worker)
    assert result["outcome"] == "return_to_requirements"
    current = await repository.get_run(str(run["run_id"]))
    assert current["status"] == "queued"
    assert current["request_json"]["current_stage"] == "requirements"
    assert current["request_json"]["requirements_approval"] is None
    assert "config_snapshot_uri" not in current["executor_json"]
    contract_ref = current["request_json"]["accepted_documents"]["task_contract"]
    contract = TaskDocumentService(core_settings).read("demo", contract_ref)
    assert contract["status"] == "draft"
    assert "revised entry rule" in contract["payload"]["requirements"][0]
    async with repository.database.connection() as connection:
        invocation = await (await connection.execute(
            """
            SELECT actor_role,harness,provider,model,reasoning_effort,auth_mode,status,
                   input_tokens,output_tokens
            FROM assistant.model_invocations WHERE run_id=%s
            ORDER BY started_at LIMIT 1
            """,
            (run["run_id"],),
        )).fetchone()
        session_model = await (await connection.execute(
            """
            SELECT harness,provider,model,reasoning_effort,auth_mode
            FROM assistant.sessions WHERE session_id=%s
            """,
            (current["session_id"],),
        )).fetchone()
    assert invocation["actor_role"] == "project_agent"
    assert invocation["provider"] == "openai"
    assert invocation["model"] == "gpt-5.6-sol"
    assert invocation["status"] == "succeeded"
    assert invocation["input_tokens"] == 10
    assert session_model["provider"] == "openai"
    assert session_model["model"] == "gpt-5.6-sol"

    next_worker = str(uuid.uuid4())
    resumed = await repository.claim_run("test", next_worker, 60)
    await RunProcessor(core_settings, repository, harness).process(resumed, next_worker)
    assert harness.requests[1].physical_session_id is None
    assert "# Active mode · Requirements" in harness.requests[1].prompt


async def test_ready_for_validation_waits_for_supervisor_before_validation(core_settings):
    prepare_bindable_project(core_settings)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["task_protocol"] = "single-agent.v2"
    project["supervisor"] = {"enabled": True}
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="validation-barrier",
        external_message_id="initial", content={"text": "create safe module"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    run = await prepare_single_agent_build_run(
        core_settings, repository, run, worker, "create safe module"
    )
    candidate = root / "runs" / str(run["run_id"]) / "candidate"
    target = candidate / "project" / "safe_module.py"
    target.parent.mkdir(parents=True)
    data = b"SAFE = True\n"
    target.write_bytes(data)
    (candidate / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "create",
            "target_uri": "workspace://project/safe_module.py",
            "candidate_uri": "workspace://candidate/project/safe_module.py",
            "original_sha256": None,
            "candidate_sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }],
    }), encoding="utf-8")
    response = json.dumps({
        "outcome": "ready_for_validation", "summary": "candidate ready",
        "task_checkpoint": {
            "goal": "create safe module", "completed": ["candidate"],
            "pending": [], "decisions": [], "constraints": [],
            "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "Supervisor before Validation",
        }, "artifacts": [],
    })
    result = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "barrier-native")])
    ).process(run, worker)
    assert result["task_phase"] == "waiting_validation_supervisor"
    current = await repository.get_run(str(run["run_id"]))
    assert current["status"] == "waiting_supervisor"
    assert current["request_json"]["pending_validation_supervisor_turn"] == 1
    assert current["request_json"]["validation_supervisor_approved_turn"] is None
    assert "validation_artifact" not in current["request_json"]


@pytest.mark.parametrize(
    ("reconciliation_action", "expected_parent_status"),
    [("retry", "waiting_user"), ("fail", "failed"), ("cancel", "cancelled")],
)
async def test_single_agent_validation_waits_for_release_then_creates_publish(
    core_settings, reconciliation_action, expected_parent_status,
):
    prepare_bindable_project(core_settings)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["task_protocol"] = "single-agent.v2"
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="single-release:initial",
        external_message_id="initial", content={"text": "create safe module"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    run = await prepare_single_agent_build_run(
        core_settings, repository, run, worker, "create safe module",
        validation_preapproved=True,
    )
    candidate = root / "runs" / str(run["run_id"]) / "candidate"
    target = candidate / "project" / "safe_module.py"
    target.parent.mkdir(parents=True)
    data = b"SAFE = True\n"
    target.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (candidate / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "create",
            "target_uri": "workspace://project/safe_module.py",
            "candidate_uri": "workspace://candidate/project/safe_module.py",
            "original_sha256": None, "candidate_sha256": digest,
            "size": len(data),
        }],
    }), encoding="utf-8")
    response = json.dumps({
        "outcome": "ready_for_validation", "summary": "candidate ready",
        "task_checkpoint": {
            "goal": "create safe module", "completed": ["candidate written"],
            "pending": [], "decisions": [], "constraints": ["candidate only"],
            "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "Kernel validation",
        },
        "artifacts": [],
    })
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(response, 10, "release-native")])
    ).process(run, worker)
    assert waiting["outcome"] == "needs_approval"
    current = await repository.get_run(str(run["run_id"]))
    assert current["status"] == "waiting_user"
    assert current["request_json"]["task_phase"] == "waiting_release"
    assert current["request_json"]["sealed_candidate_bundle"]["bundle_sha256"]
    approved = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="single-release:approve",
        external_message_id="approve", content={"decision": "approve"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert approved["run"]["run_type"] == "publish"
    assert approved["run"]["status"] == "queued"
    parent = await repository.get_run(str(run["run_id"]))
    assert parent["status"] == "waiting_user"
    assert parent["request_json"]["task_phase"] == "publishing"
    assert parent["result_json"]["publication_state"] == "queued"
    assert await repository.request_cancel(str(run["run_id"])) is False
    repeated = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="single-release:again",
        external_message_id="again", content={"decision": "approve"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert repeated["run"] is None
    busy = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="single-release:busy",
        external_message_id="busy", content={"text": "new work"},
    )
    assert busy["run"] is None
    publisher_worker = str(uuid.uuid4())
    publisher = await repository.claim_run("test", publisher_worker, 60)
    assert str(publisher["run_id"]) == str(approved["run"]["run_id"])
    assert await repository.retry_run(
        str(publisher["run_id"]), publisher_worker,
        str(publisher["claim_token"]), {"type": "InjectedPublisherFailure"},
    ) == "reconciling"
    parent = await repository.get_run(str(run["run_id"]))
    assert parent["status"] == "blocked"
    assert parent["request_json"]["task_phase"] == "publication_reconciling"
    card = await repository.queue_reconciliation_card(
        run_id=str(publisher["run_id"]), diagnostic={
            "text": "Publisher outcome needs reconciliation",
            "actions": [{"id": reconciliation_action, "label": "Decision"}],
        },
    )
    assert card is not None
    resolution = await repository.resolve_reconciliation_card(
        card_message_id=str(card["message_id"]),
        external_event_id=f"single-release:reconcile-{reconciliation_action}",
        actor_user_id="user-1", action=reconciliation_action,
    )
    assert resolution["status"] == {
        "retry": "queued", "fail": "failed", "cancel": "cancelled",
    }[reconciliation_action]
    assert (await repository.get_run(str(run["run_id"])))["status"] == expected_parent_status
    if reconciliation_action != "retry":
        assert not (root / "safe_module.py").exists()
        return
    publisher_worker = str(uuid.uuid4())
    publisher = await repository.claim_run("test", publisher_worker, 60)
    publication, artifacts = CapabilityBundleService(core_settings).publish(
        project_id="demo", publish_run_id=str(publisher["run_id"]),
        request=dict(publisher["request_json"]),
    )
    await repository.finish_publish_run(
        str(publisher["run_id"]), publisher_worker,
        str(publisher["claim_token"]),
        result=publication, artifact_refs=artifacts,
    )
    assert publication["outcome"] == "published"
    assert (root / "safe_module.py").read_text(encoding="utf-8") == "SAFE = True\n"
    parent = await repository.get_run(str(run["run_id"]))
    assert parent["status"] == "succeeded"
    assert parent["request_json"]["task_phase"] == "published"
    assert parent["result_json"]["publication_state"] == "published"


async def test_high_risk_single_agent_uses_fresh_read_only_audit_turn(core_settings):
    prepare_bindable_project(core_settings)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    project["task_protocol"] = "single-agent.v2"
    project["single_agent"] = {"audit_required": True}
    (root / "project.yaml").write_text(
        yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
    )
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="audit-task:initial",
        external_message_id="initial", content={"text": "high risk candidate"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    run = await prepare_single_agent_build_run(
        core_settings, repository, run, worker, "high risk candidate",
        validation_preapproved=True,
    )
    await repository.patch_claimed_run_request(
        run_id=str(run["run_id"]), worker_instance_id=worker,
        claim_token=str(run["claim_token"]), request_patch={"audit_required": True},
    )
    run = await repository.get_run(str(run["run_id"]))
    candidate = root / "runs" / str(run["run_id"]) / "candidate"
    target = candidate / "project" / "guard.py"
    target.parent.mkdir(parents=True)
    data = b"DENY = True\n"
    target.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (candidate / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2", "files": [{
            "operation": "create", "target_uri": "workspace://project/guard.py",
            "candidate_uri": "workspace://candidate/project/guard.py",
            "original_sha256": None, "candidate_sha256": digest, "size": len(data),
        }],
    }), encoding="utf-8")
    ready = json.dumps({
        "outcome": "ready_for_validation", "summary": "ready",
        "task_checkpoint": {
            "goal": "high risk candidate", "completed": ["candidate"],
            "pending": [], "decisions": [], "constraints": [],
            "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "validation",
        }, "artifacts": [],
    })
    first = await RunProcessor(
        core_settings, repository, FakeHarness([(ready, 10, "author-native")])
    ).process(run, worker)
    assert first["task_phase"] == "audit"
    after_validation = await repository.get_run(str(run["run_id"]))
    assert after_validation["status"] == "queued"
    assert after_validation["request_json"]["execution_plan_variant"] == "audit"
    audit_worker = str(uuid.uuid4())
    audit_run = await repository.claim_run("test", audit_worker, 60)
    audited = json.dumps({
        "outcome": "ready_for_release", "summary": "cold audit passed",
        "task_checkpoint": {
            "goal": "high risk candidate", "completed": ["cold audit"],
            "pending": [], "decisions": [], "constraints": [],
            "external_effects": [], "artifacts": [], "open_questions": [],
            "next_action": "release approval",
        }, "artifacts": [],
    })
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(audited, 12, "audit-native")])
    ).process(audit_run, audit_worker)
    assert waiting["outcome"] == "needs_approval"
    current = await repository.get_run(str(run["run_id"]))
    assert current["request_json"]["audit_complete"] is True
    build_plan = root / "runs" / str(run["run_id"]) / "resolved" / "execution-plan-build.json"
    audit_plan = root / "runs" / str(run["run_id"]) / "resolved" / "execution-plan-audit.json"
    assert build_plan.is_file() and audit_plan.is_file()
    audit_payload = json.loads(audit_plan.read_text(encoding="utf-8"))
    assert audit_payload["bindings"]["stage_permissions"]["direct_workspace_write"] is False
    assert audit_payload["bindings"]["work_mode_bundle"]["mode_id"] == "audit"
    assert audit_payload["bindings"]["permission_profile_name"] == "read-only"
    assert audit_payload["bindings"]["work_mode_bundle_sha256"]
    scope_uri = audit_payload["bindings"]["workspace_scope_snapshot_uri"]
    scope = json.loads((root / scope_uri.split("vault://demo/", 1)[1]).read_text(encoding="utf-8"))
    assert "candidate" not in {item["key"] for item in scope["roots"]}
    assert audit_payload["runtime"]["may_write_shared"] is False
    assert audit_payload["bindings"]["stage_permissions"]["network"] is True
    assert audit_payload["bindings"]["stage_permissions"]["account_connect"] is True
    assert audit_payload["bindings"]["stage_permissions"]["trade"] is True


async def test_feishu_card_action_approves_current_stage(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    prepare_stage(core_settings, "demo", "implementation")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="card-stage:initial",
        external_message_id="initial", content={"text": "build"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="build safely",
        stages=["requirements", "implementation"], change_class="major",
    )
    await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    stage_worker = str(uuid.uuid4())
    stage_run = await repository.claim_run("test", stage_worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_approval", "summary": "ready",
                "document_markdown": "# Requirements", "approval_question": "Proceed?",
                "artifacts": [],
            }),
            10, "requirements-native",
        )]),
    ).process(stage_run, stage_worker)
    delivery_worker = str(uuid.uuid4())
    approval = await repository.claim_delivery("sender", delivery_worker, 60, "feishu")
    assert await repository.acknowledge_delivery(
        str(approval["message_id"]), delivery_worker, "approval-card-external"
    )
    result = await ingest_event(core_settings, {
        "header": {"event_id": "card-stage:approve"},
        "event": {
            "operator": {"open_id": "user-1"},
            "action": {"value": {
                "decision": "approve",
                "project_id": "demo",
                "run_id": str(stage_run["run_id"]),
                "question_message_id": waiting["question_message_id"],
            }},
            "context": {
                "open_chat_id": "chat-1",
                "open_message_id": "approval-card-external",
            },
        },
    }, "app-1")
    assert result["approval_decision"] == "approve"
    assert result["run"]["stage"] == "implementation"


async def test_cutover_retires_waiting_stage_card_and_resume(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="cutover:initial",
        external_message_id="initial", content={"text": "legacy requirements"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="legacy requirements",
        stages=["requirements", "implementation", "review"],
    )
    await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    stage_worker = str(uuid.uuid4())
    stage = await repository.claim_run("test", stage_worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "clarification",
                "questions": [{
                    "question_id": "legacy-q", "question_text": "Old question?",
                    "required": True,
                }], "artifacts": [],
            }), 10, "legacy-native",
        )]),
    ).process(stage, stage_worker)
    root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    (root / "project.yaml").write_text(
        yaml.safe_dump({"task_protocol": "single-agent.v2"}), encoding="utf-8"
    )
    answer = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="cutover:answer",
        external_message_id="answer", content={"text": "old answer"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert answer["stale_reply"] is True
    assert answer["run"] is None
    assert (await repository.get_run(str(stage["run_id"])))["status"] == "waiting_user"
    with pytest.raises(PermissionError, match="旧阶段"):
        await repository.resume_waiting_run(
            run_id=str(stage["run_id"]), project_id="demo", channel="discord",
            chat_id="chat-1", user_id="user-1",
            source_message_id=str(answer["message"]["message_id"]),
            idempotency_key="cutover:resume",
        )
    assert await repository.request_cancel(str(stage["run_id"])) is True
    assert (await repository.get_run(str(stage["run_id"])))["status"] == "cancelled"
    async with repository.database.connection() as connection:
        stages = await (await connection.execute(
            "SELECT count(*) AS count FROM assistant.runs WHERE project_id='demo' "
            "AND stage='implementation'"
        )).fetchone()
    assert int(stages["count"]) == 0


async def test_failed_validation_creates_bounded_implementation_rework(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "implementation")
    prepare_stage(core_settings, "demo", "review")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="minor:initial",
        external_message_id="initial", content={"text": "small fix"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    task = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="small safe fix",
        stages=["implementation", "review"], change_class="minor",
    )
    assert await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded", result={"task_run_id": str(task["run_id"])},
    )
    stage_worker = str(uuid.uuid4())
    implementation = await repository.claim_run("test", stage_worker, 60)
    completed = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({"outcome": "completed", "summary": "implemented", "artifacts": []}),
            10, "implementation-native",
        )])
    ).process(implementation, stage_worker)
    assert completed["outcome"] == "validation_rework"
    assert completed["next_run_id"] is not None
    rework = await repository.get_run(completed["next_run_id"])
    assert rework["stage"] == "implementation"
    assert rework["request_json"]["validation_rework_count"] == 1
    validation = next(
        item for item in (await repository.get_run(str(implementation["run_id"])))["artifact_refs"]
        if item.get("kind") == "validation_report"
    )
    assert validation["status"] == "failed"
    rework_worker = str(uuid.uuid4())
    claimed_rework = await repository.claim_run("test", rework_worker, 60)
    second = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({"outcome": "completed", "summary": "retry", "artifacts": []}),
            10, "implementation-native-2",
        )]),
    ).process(claimed_rework, rework_worker)
    assert second["outcome"] == "validation_rework"
    assert (await repository.get_run(second["next_run_id"]))["request_json"][
        "validation_rework_count"
    ] == 2

    final_worker = str(uuid.uuid4())
    final_claimed = await repository.claim_run("test", final_worker, 60)
    blocked = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({"outcome": "completed", "summary": "retry", "artifacts": []}),
            10, "implementation-native-final",
        )]),
    ).process(final_claimed, final_worker)
    assert blocked["outcome"] == "blocked"
    assert blocked["next_run_id"] is None


async def test_review_request_changes_creates_fresh_implementation_round(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "implementation")
    prepare_stage(core_settings, "demo", "review")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="rework:initial",
        external_message_id="initial", content={"text": "change"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    task = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="change safely",
        stages=["implementation", "review"], change_class="minor",
    )
    await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    implementation_worker = str(uuid.uuid4())
    implementation = await repository.claim_run("test", implementation_worker, 60)
    candidate_root = (
        ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root /
        "runs" / str(implementation["run_id"]) / "candidate"
    )
    candidate_file = candidate_root / "project" / "notes.txt"
    candidate_file.parent.mkdir(parents=True, exist_ok=True)
    candidate_file.write_text("candidate\n", encoding="utf-8")
    candidate_hash = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    (candidate_root / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "create",
            "target_uri": "workspace://project/notes.txt",
            "candidate_uri": "workspace://candidate/project/notes.txt",
            "original_sha256": None,
            "candidate_sha256": candidate_hash,
            "size": len(candidate_file.read_bytes()),
        }],
    }), encoding="utf-8")
    completed = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({"outcome": "completed", "summary": "candidate", "artifacts": []}),
            10, "implementation-native",
        )]),
    ).process(implementation, implementation_worker)
    review_worker = str(uuid.uuid4())
    review = await repository.claim_run("test", review_worker, 60)
    assert str(review["run_id"]) == completed["next_run_id"]
    rework = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "request_changes", "summary": "fix regression",
                "target_stage": "implementation",
                "issues": [{"issue_id": "review-1", "evidence": [],
                            "required_fix": "preserve existing behavior"}],
                "artifacts": [],
            }),
            10, "review-native",
        )]),
    ).process(review, review_worker)
    next_run = await repository.get_run(rework["next_run_id"])
    assert next_run["stage"] == "implementation"
    assert next_run["parent_run_id"] == review["run_id"]
    assert next_run["request_json"]["stage_round"] == 2
    assert next_run["request_json"]["review_feedback"]["issues"][0]["issue_id"] == "review-1"


async def test_requirements_only_review_rework_returns_to_requirements(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    prepare_stage(core_settings, "demo", "review")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="design:initial",
        external_message_id="design-initial", content={"text": "design only"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("manager", manager_worker, 60)
    await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="produce requirements",
        stages=["requirements", "review"], change_class="major",
    )
    await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    requirements_worker = str(uuid.uuid4())
    requirements = await repository.claim_run("requirements", requirements_worker, 60)
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_approval", "summary": "requirements ready",
                "document_markdown": "# Requirements", "approval_question": "Approve?",
                "artifacts": [],
            }),
            10, "requirements-native",
        )]),
    ).process(requirements, requirements_worker)
    approved = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:requirements", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="design:approve",
        external_message_id="design-approve", content={"decision": "approve"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert approved["run"]["stage"] == "review"
    review_worker = str(uuid.uuid4())
    review = await repository.claim_run("review", review_worker, 60)
    rework = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "request_changes", "summary": "clarify boundary",
                "target_stage": "requirements",
                "issues": [{
                    "issue_id": "requirements-1", "evidence": [],
                    "required_fix": "clarify the execution boundary",
                }],
                "artifacts": [],
            }),
            10, "review-native",
        )]),
    ).process(review, review_worker)
    next_run = await repository.get_run(rework["next_run_id"])
    assert next_run["stage"] == "requirements"
    assert next_run["agent_key"] == "project:demo:requirements"
    assert next_run["request_json"]["stage_round"] == 2


async def test_approved_skill_review_queues_deterministic_publish(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "implementation")
    prepare_stage(core_settings, "demo", "review")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="skill:initial",
        external_message_id="initial", content={"text": "create a skill"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    task = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="create the skill",
        stages=["implementation", "review"], change_class="minor",
        task_metadata={
            "task_kind": "skill_change",
            "capability_change": {
                "target_skill": "new-skill", "change_mode": "create",
                "baseline": {"bundle_hash": None},
            },
        },
    )
    await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    implementation_worker = str(uuid.uuid4())
    implementation = await repository.claim_run("test", implementation_worker, 60)
    candidate_root = (
        ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root /
        "runs" / str(implementation["run_id"]) / "candidate"
    )
    candidate_file = candidate_root / "project" / ".agents" / "skills" / "new-skill" / "SKILL.md"
    candidate_file.parent.mkdir(parents=True, exist_ok=True)
    candidate_content = """---
name: new-skill
description: Create one test capability.
---

# New Skill

## 任务目标

Produce one verified test result.
"""
    candidate_file.write_text(candidate_content, encoding="utf-8")
    candidate_hash = hashlib.sha256(candidate_file.read_bytes()).hexdigest()
    (candidate_root / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "create",
            "target_uri": "workspace://project/.agents/skills/new-skill/SKILL.md",
            "candidate_uri": "workspace://candidate/project/.agents/skills/new-skill/SKILL.md",
            "original_sha256": None, "candidate_sha256": candidate_hash,
            "size": len(candidate_file.read_bytes()),
        }],
    }), encoding="utf-8")
    implementation_result = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({"outcome": "completed", "summary": "candidate", "artifacts": []}),
            10, "implementation-native",
        )]),
    ).process(implementation, implementation_worker)
    review_worker = str(uuid.uuid4())
    review = await repository.claim_run("test", review_worker, 60)
    assert str(review["run_id"]) == implementation_result["next_run_id"]
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_approval", "summary": "review passed",
                "document_markdown": "# Review\n\nVERDICT: APPROVE",
                "approval_question": "Approve publishing this Skill?", "artifacts": [],
            }),
            10, "review-native",
        )]),
    ).process(review, review_worker)
    approved = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:review", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="skill:approval",
        external_message_id="approval", content={"decision": "approve"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert approved["run"]["run_type"] == "publish"
    assert approved["run"]["status"] == "queued"
    assert approved["run"]["request_json"]["candidate_manifest_uri"].endswith(
        "/candidate/manifest.json"
    )
    publisher_worker = str(uuid.uuid4())
    publish = await repository.claim_run("test", publisher_worker, 60)
    assert str(publish["run_id"]) == str(approved["run"]["run_id"])
    publication, publication_artifacts = CapabilityBundleService(core_settings).publish(
        project_id="demo", publish_run_id=str(publish["run_id"]),
        request=dict(publish["request_json"]),
    )
    receipt = await repository.finish_publish_run(
        str(publish["run_id"]), publisher_worker, str(publish["claim_token"]),
        result=publication, artifact_refs=publication_artifacts,
    )
    finished = await repository.get_run(str(publish["run_id"]))
    assert finished["status"] == "succeeded"
    assert finished["result_json"]["outcome"] == "published"
    assert receipt["reply_message_id"]
    implementation_done = await repository.get_run(str(implementation["run_id"]))
    validation = next(
        item for item in implementation_done["artifact_refs"]
        if item.get("kind") == "validation_report"
    )
    assert validation["status"] == "passed"
    active_skill = (
        ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root /
        ".agents" / "skills" / "new-skill" / "SKILL.md"
    )
    assert active_skill.read_text(encoding="utf-8") == candidate_content


async def test_expired_run_is_quarantined_not_replayed(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="lease:initial",
        external_message_id="initial", content={"text": "hello"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET lease_until=now()-interval '1 second' WHERE run_id=%s",
            (run["run_id"],),
        )
        await connection.commit()
    quarantined = await repository.quarantine_expired_runs()
    assert len(quarantined) == 1
    assert quarantined[0]["status"] == "reconciling"
    assert await repository.claim_run("test", str(uuid.uuid4()), 60) is None
    assert await repository.reconcile_run(
        str(run["run_id"]), "retry", "trace inspected; safe to retry"
    ) == "queued"
    assert await repository.claim_run("test", str(uuid.uuid4()), 60) is not None


async def test_reconciliation_card_retries_without_an_llm_run(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="reconcile:initial",
        external_message_id="initial", content={"text": "work"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.runs SET status='reconciling',worker_id=NULL,
                worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                error_json=%s WHERE run_id=%s
            """,
            (Jsonb({"type": "LeaseExpired", "message": "worker stopped"}), run["run_id"]),
        )
        await connection.commit()
    service = ReconciliationService(core_settings, repository)
    queued = await service.notify_due()
    assert len(queued) == 1
    card = await repository.get_message(queued[0])
    assert card["message_type"] == "reconciliation"
    assert [item["id"] for item in card["content_json"]["actions"]] == [
        "retry", "fail", "cancel",
    ]
    assert await service.notify_due() == []
    result = await service.resolve_card(
        card_message_id=str(card["message_id"]), external_event_id="interaction-1",
        actor_user_id="user-1", action="retry",
    )
    assert result["status"] == "queued"
    duplicate = await service.resolve_card(
        card_message_id=str(card["message_id"]), external_event_id="interaction-1",
        actor_user_id="user-1", action="retry",
    )
    assert duplicate["duplicate"] is True
    assert (await repository.get_run(str(run["run_id"])))["status"] == "queued"


async def test_reconciliation_uses_trace_file_changes_and_never_recommends_retry(
    core_settings,
):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="effects:initial",
        external_message_id="initial", content={"text": "work"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    trace_relative = Path("runs/agent-traces/invocations/effects-test")
    trace_root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root / trace_relative
    trace_root.mkdir(parents=True)
    (trace_root / "stdout.jsonl").write_text(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "file_change", "status": "completed",
            "changes": [{"path": "/workspace/strategy.py", "kind": "update"}],
        },
    }) + "\n", encoding="utf-8")
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.runs SET status='reconciling',worker_id=NULL,
                worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                executor_json=%s,error_json=%s WHERE run_id=%s
            """,
            (
                Jsonb({"trace_uri": f"vault://demo/{trace_relative.as_posix()}/"}),
                Jsonb({"type": "TimeoutError", "message": "timed out"}),
                run["run_id"],
            ),
        )
        await connection.commit()
    service = ReconciliationService(core_settings, repository)
    diagnostic = service.diagnose(await repository.get_run(str(run["run_id"])))
    assert diagnostic["recommended_action"] == "fail"
    assert diagnostic["trace_file_changes"] == [
        {"path": "/workspace/strategy.py", "kind": "update"}
    ]
    assert "文件变化记录：1" in diagnostic["text"]


async def test_reconciliation_resumes_existing_candidate_instead_of_blind_retry(
    core_settings,
):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="candidate:initial",
        external_message_id="initial", content={"text": "work"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    vault = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root
    candidate = vault / "runs" / str(run["run_id"]) / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "manifest.json").write_text(json.dumps({
        "schema_version": "candidate-manifest.v2",
        "files": [{
            "operation": "update", "target_uri": "workspace://project/a.py",
            "candidate_uri": "workspace://candidate/project/a.py",
        }],
    }), encoding="utf-8")
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET status='reconciling',worker_id=NULL,"
            "worker_instance_id=NULL,claim_token=NULL,lease_until=NULL WHERE run_id=%s",
            (run["run_id"],),
        )
        await connection.commit()
    diagnostic = ReconciliationService(core_settings, repository).diagnose(
        await repository.get_run(str(run["run_id"]))
    )
    assert diagnostic["recommended_action"] == "resume"
    assert [item["id"] for item in diagnostic["actions"]][0] == "resume"
    assert diagnostic["candidate_changes"][0]["target_uri"] == "workspace://project/a.py"


async def test_reconciliation_card_recovers_completed_chat_trace(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="recover:initial",
        external_message_id="initial", content={"text": "work"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    trace_relative = Path("runs/agent-traces/invocations/recover-test")
    trace_root = ProjectRuntimeManager(core_settings.data_root).ensure("demo").vault_root / trace_relative
    trace_root.mkdir(parents=True)
    (trace_root / "stdout.jsonl").write_text("\n".join([
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "recovered answer"},
        }),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]) + "\n", encoding="utf-8")
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            UPDATE assistant.runs SET status='reconciling',worker_id=NULL,
                worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,
                executor_json=%s,error_json=%s WHERE run_id=%s
            """,
            (
                Jsonb({"trace_uri": f"vault://demo/{trace_relative.as_posix()}/"}),
                Jsonb({"type": "RuntimeError", "message": "final commit failed"}),
                run["run_id"],
            ),
        )
        await connection.commit()
    service = ReconciliationService(core_settings, repository)
    card_id = (await service.notify_due())[0]
    card = await repository.get_message(card_id)
    assert card["content_json"]["recommended_action"] == "recover"
    result = await service.resolve_card(
        card_message_id=card_id, external_event_id="interaction-recover",
        actor_user_id="user-1", action="recover",
    )
    assert result["status"] == "succeeded"
    recovered = await repository.get_run(str(run["run_id"]))
    assert recovered["result_json"]["summary"] == "recovered answer"
    assert (await repository.reconciliation_health())["reconciling_count"] == 0


async def test_reconciliation_wait_defers_next_card(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="wait:initial",
        external_message_id="initial", content={"text": "work"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET status='reconciling',worker_id=NULL,"
            "worker_instance_id=NULL,claim_token=NULL,lease_until=NULL WHERE run_id=%s",
            (run["run_id"],),
        )
        await connection.commit()
    card = await repository.queue_reconciliation_card(
        run_id=str(run["run_id"]), diagnostic={
            "text": "still running", "actions": [
                {"id": "wait", "label": "继续等待", "style": "secondary"},
                {"id": "cancel", "label": "终止并取消", "style": "danger"},
            ],
            "process_alive": True,"recommended_action": "wait",
        },
    )
    result = await repository.resolve_reconciliation_card(
        card_message_id=str(card["message_id"]), external_event_id="interaction-wait",
        actor_user_id="user-1", action="wait",
    )
    assert result["status"] == "reconciling"
    assert await repository.list_reconciling_runs_due() == []


async def test_scoped_handoff_is_idempotent(core_settings):
    prepare_agent(core_settings, "system")
    prepare_agent(core_settings, "target")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="system", agent_key="project:system:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="handoff:initial",
        external_message_id="initial", content={"text": "route me"},
    )
    worker = str(uuid.uuid4())
    source = await repository.claim_run("test", worker, 60)
    first = await repository.handoff_from_claim(
        source_run_id=str(source["run_id"]), worker_instance_id=worker,
        claim_token=str(source["claim_token"]), target_project_id="target",
        handoff_summary="Route this request.",
    )
    second = await repository.handoff_from_claim(
        source_run_id=str(source["run_id"]), worker_instance_id=worker,
        claim_token=str(source["claim_token"]), target_project_id="target",
        handoff_summary="Route this request.",
    )
    assert first["message"]["message_id"] == second["message"]["message_id"]
    assert first["run"]["run_id"] == second["run"]["run_id"]
    assert first["route_binding"]["message_id"] == second["route_binding"]["message_id"]
    assert await repository.resolve_route_binding("feishu", "chat-1", "user-1") == (
        "target", "project:target:manager"
    )
    assert await repository.resolve_route_binding("feishu", "chat-1", "another-user") is None
    routed = await ingest_event(
        core_settings,
        {
            "header": {"event_id": "handoff:follow-up"},
            "event": {
                "sender": {"sender_id": {"open_id": "user-1"}},
                "message": {
                    "chat_id": "chat-1", "message_id": "follow-up",
                    "content": json.dumps({"text": "continue here"}),
                },
            },
        },
        "app-1",
    )
    assert routed["message"]["project_id"] == "target"
    assert routed["run"]["agent_key"] == "project:target:manager"
    assert len(await repository.list_runs("target")) == 2
    target_worker = str(uuid.uuid4())
    target_run = await repository.claim_run("test", target_worker, 60)
    assert target_run["project_id"] == "target"
    returned = await repository.handoff_from_claim(
        source_run_id=str(target_run["run_id"]), worker_instance_id=target_worker,
        claim_token=str(target_run["claim_token"]), target_project_id="system",
        handoff_summary="Return to the Butler.",
    )
    assert returned["run"]["agent_key"] == "system-butler"
    assert await repository.resolve_route_binding("feishu", "chat-1", "user-1") == (
        "system", "system-butler"
    )


async def test_project_creation_requires_approved_scoped_run(core_settings):
    settings = core_settings.model_copy(update={
        "workspace_root": core_settings.data_root,
    })
    repository = CoreRepository(settings)
    await repository.ingest_message(
        project_id="system", agent_key="system-butler", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="project:initial",
        external_message_id="initial", content={"text": "create a project"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    service = ProjectDirectoryService(settings, repository)
    source = settings.workspace_root / "external" / "project-source"
    source.mkdir(parents=True)
    (settings.data_root / "config").mkdir(parents=True, exist_ok=True)
    (settings.data_root / "config" / "source-discovery.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "source-discovery.v1",
            "roots": [{
                "discovery_id": "test-workspace",
                "path": str(settings.workspace_root),
                "display_path": "TEST",
                "description": "test workspace",
            }],
        }, sort_keys=False),
        encoding="utf-8",
    )
    proposal = service.propose(
        str(run["run_id"]), "new-project", "A safe test Project",
        initial_goal="Deliver the first useful result",
        boundaries=["Only this Project Vault"], non_goals=["No external publish"],
        workspace_roots=[{
            "discovery_id": "test-workspace",
            "relative_path": "external/project-source",
            "root_key": "project-source",
            "purpose": "Project source",
        }],
    )
    with pytest.raises(PermissionError):
        await service.create_from_approved_run(
            run_id=str(run["run_id"]), worker_instance_id=worker,
            claim_token=str(run["claim_token"]), proposal_uri=proposal["uri"],
            expected_sha256=proposal["sha256"],
        )
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",
            (Jsonb({"answer": {"answers": {
                proposal["approval_question_id"]: "批准"
            }}}), run["run_id"]),
        )
        await connection.commit()
    created = await service.create_from_approved_run(
        run_id=str(run["run_id"]), worker_instance_id=worker,
        claim_token=str(run["claim_token"]), proposal_uri=proposal["uri"],
        expected_sha256=proposal["sha256"],
    )
    assert created["status"] == "active"
    assert created["initial_goal"] == "Deliver the first useful result"
    assert len(created["file_grant_ids"]) == 1
    root = ProjectRuntimeManager(settings.data_root).ensure("new-project").vault_root
    assert (root / "project.yaml").is_file()
    source_map = yaml.safe_load((root / "source-map.yaml").read_text(encoding="utf-8"))
    assert source_map["schema_version"] == "project-source-map.v1"
    assert source_map["roots"]["project-source"]["entrypoints"] == []
    assert source_map["roots"]["project-source"]["mode"] == "live"
    assert yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))[
        "task_protocol"
    ] == "single-agent.v2"
    assert (root / "agents" / "pm" / "config.yaml").is_file()
    assert (root / "project-bootstrap.json").is_file()
    assert {
        item.name for item in (root / "agents" / "pm" / "profiles").iterdir()
    } == {
        "requirements.yaml", "build.yaml", "audit.yaml",
        "operate.yaml", "repair.yaml",
    }
    assert json.loads(
        (root / "runtime" / "credentials.json").read_text(encoding="utf-8")
    )["schema_version"] == "project-credentials.v3"
    assert {item.name for item in (root / "agents").iterdir()} == {
        "pm", "supervisor", "operator", "child",
    }
    created_project = yaml.safe_load(
        (root / "project.yaml").read_text(encoding="utf-8")
    )
    assert created_project["supervisor"]["model"] == "gpt-5.6-sol"
    assert created_project["supervisor"]["models"]["intent_panel"]["model"] == (
        "gpt-5.6-luna"
    )
    assert created_project["base_pack"] == {
        "version": "assistant-base-pack.v1",
        "semantic_map_schema": "1.0",
    }
    assert (root / "knowledge" / "map" / "index.yaml").is_file()
    assert (root / "knowledge" / "map" / "overview.md").is_file()
    for agent_id in ("pm", "supervisor", "operator", "child"):
        assert (root / "agents" / agent_id / "MAP.md").is_file()
    assert created_project["network"] == {"enabled": True, "unrestricted": True}
    assert created_project["external_actions"]["approval_required"] is False
    assert (root / ".agents" / "skills" / "assistant-requirements" / "SKILL.md").is_file()
    expected_fields = {
        "schema_version", "agent_id", "agent_key", "kind", "status",
        "display_name", "aliases", "role", "scope_profile", "harness",
        "skills", "mcp_servers", "entrypoints", "triggers", "tools",
    }
    manager_config = yaml.safe_load(
        (root / "agents" / "pm" / "config.yaml").read_text(encoding="utf-8")
    )
    assert set(manager_config) == expected_fields
    assert {
        "list_recent_history", "list_source_discovery_roots", "inspect_source_path",
    } <= set(manager_config["tools"])
    assert "start_project_task" not in manager_config["tools"]
    assert "handoff_to_agent" not in manager_config["tools"]
    assert "propose_source_scope" not in manager_config["tools"]
    assert "activate_source_scope" not in manager_config["tools"]
    assert {item["name"] for item in manager_config["skills"]} == {
            "assistant-task-continuity", "assistant-project-agent",
            "assistant-subagent-orchestration",
            "assistant-requirements", "assistant-release-audit",
        "assistant-history-retrieval",
    }
    assert {"write_candidate_file", "delete_candidate_file"} <= set(
        manager_config["tools"]
    )
    assert "list_project_credentials" in manager_config["tools"]
    assert "get_project_credential" in manager_config["tools"]
    assert "request_project_credential" in manager_config["tools"]
    assert "delete_project_credential" in manager_config["tools"]
    assert {item["project_id"] for item in service.list()} == {"new-project"}
    assert service.list()[0]["proposal_sha256"] == proposal["sha256"]
    assert service.list()[0]["routing"]["typical_requests"] == [
        "Deliver the first useful result"
    ]
    assert len(service.list()[0]["routing_hash"]) == 64
    repeated = await service.create_from_approved_run(
        run_id=str(run["run_id"]), worker_instance_id=worker,
        claim_token=str(run["claim_token"]), proposal_uri=proposal["uri"],
        expected_sha256=proposal["sha256"],
    )
    assert repeated["idempotent"] is True
    with pytest.raises(ValueError, match="already registered"):
        service.propose("different-run", "new-project", "Different purpose")
    ProjectRuntimeManager(core_settings.data_root).ensure("orphan-vault")
    with pytest.raises(ValueError, match="already exists"):
        service.propose("different-run", "orphan-vault", "Do not adopt")


async def test_source_scope_is_metadata_only_approval_gated_and_idempotent(
    core_settings,
) -> None:
    settings = core_settings.model_copy(update={
        "workspace_root": core_settings.data_root,
    })
    prepare_bindable_project(settings, "demo")
    source = settings.workspace_root / "external" / "vnpy"
    source.mkdir(parents=True)
    (source / "good.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "token.py").write_text("TOKEN_KIND = 'syntax'\n", encoding="utf-8")
    (source / "README.md").write_text("safe docs\n", encoding="utf-8")
    (source / ".env").write_text("DO_NOT_RETURN=secret\n", encoding="utf-8")
    (source / "Secrets.yaml").write_text("PASSWORD: DO_NOT_RETURN\n", encoding="utf-8")
    (source / "API_KEY.JSON").write_text("DO_NOT_RETURN\n", encoding="utf-8")
    disabled_git = source / ".git.disabled-previous"
    disabled_git.mkdir()
    (disabled_git / "config").write_text("DO_NOT_RETURN_GIT\n", encoding="utf-8")
    catalog = settings.data_root / "config" / "source-discovery.yaml"
    catalog.write_text(yaml.safe_dump({
        "schema_version": "source-discovery.v1",
        "roots": [{
            "discovery_id": "test-workspace",
            "path": str(settings.workspace_root),
            "display_path": "TEST",
            "description": "test source discovery",
            "excludes": ["vaults/**"],
        }],
    }, sort_keys=False), encoding="utf-8")
    settings.service_credentials_path.parent.mkdir(parents=True, exist_ok=True)
    settings.service_credentials_path.write_text(json.dumps({
        "services": {"feishu": {"owner_open_ids": ["user-1"]}}
    }), encoding="utf-8")
    repository = CoreRepository(settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="scope:request",
        external_message_id="scope-request", content={"text": "inspect external"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    service = SourceScopeService(settings, repository)

    inspection = service.inspect(
        "test-workspace", "external/vnpy", max_depth=2, limit=20,
    )
    assert inspection["metadata_only"] is True
    assert inspection["file_count"] == 6
    assert inspection["excluded_entry_count"] >= 1
    assert inspection["scanned_entry_count"] >= 6
    assert {item["relative_path"] for item in inspection["entries"]} >= {
        ".env", "Secrets.yaml", "API_KEY.JSON",
    }
    assert any(item["relative_path"] == "token.py" for item in inspection["entries"])
    assert "DO_NOT_RETURN" not in json.dumps(inspection)
    with pytest.raises(ValueError, match="stay below"):
        service.inspect("test-workspace", "../outside")
    unfiltered = service.propose(
        current_project_id="demo", run_id=str(run["run_id"]),
        target_project_id="demo", reason="authorize the parent directory",
        bindings=[{
            "root_key": "missing-includes",
            "discovery_id": "test-workspace",
            "relative_path": "external/vnpy",
        }],
    )
    assert unfiltered["bindings"][0]["includes"] == []
    assert unfiltered["bindings"][0]["excludes"] == []
    assert unfiltered["bindings"][0]["write_mode"] == "direct"

    proposal = service.propose(
        current_project_id="demo", run_id=str(run["run_id"]),
        target_project_id="demo", reason="read framework interfaces",
        authorization_mode="explicit_approval",
        bindings=[{
            "root_key": "vnpy-source",
            "discovery_id": "test-workspace",
            "relative_path": "external/vnpy",
            "purpose": "read framework source",
            "includes": ["*.py", "**/*.py", "*.md"],
            "write_mode": "gated",
        }],
    )
    repeated_proposal = service.propose(
        current_project_id="demo", run_id=str(run["run_id"]),
        target_project_id="demo", reason="read framework interfaces",
        authorization_mode="explicit_approval",
        bindings=[{
            "root_key": "vnpy-source",
            "discovery_id": "test-workspace",
            "relative_path": "external/vnpy",
            "purpose": "read framework source",
            "includes": ["*.py", "**/*.py", "*.md"],
            "write_mode": "gated",
        }],
    )
    assert repeated_proposal["idempotent"] is True
    assert repeated_proposal["proposal_sha256"] == proposal["proposal_sha256"]
    with pytest.raises(PermissionError, match="only for its own Project"):
        service.propose(
            current_project_id="demo", run_id=str(run["run_id"]),
            target_project_id="other", reason="must not cross Projects",
            bindings=[{
                "root_key": "other-source",
                "discovery_id": "test-workspace",
                "relative_path": "external/vnpy",
            }],
        )
    with pytest.raises(PermissionError, match="does not approve"):
        await service.activate(
            current_project_id="demo", run_id=str(run["run_id"]),
            worker_instance_id=worker, claim_token=str(run["claim_token"]),
            proposal_uri=proposal["proposal_uri"],
            expected_sha256=proposal["proposal_sha256"],
        )
    answer_message_id = str(uuid.uuid4())
    async with repository.database.connection() as connection:
        await connection.execute(
            """
            INSERT INTO assistant.messages(
                message_id,project_id,channel,chat_id,user_id,direction,
                message_type,content_json
            ) VALUES (%s,'demo','feishu','chat-1','user-1','inbound','answer',%s)
            """,
            (answer_message_id, Jsonb({"text": "批准扩权"})),
        )
        await connection.execute(
            "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",
            (Jsonb({"answer": {"answers": {
                proposal["approval_question_id"]: "批准扩权"
            }}, "answer_message_id": answer_message_id}), run["run_id"]),
        )
        await connection.commit()
    original_source = source.with_name("vnpy-before-link-change")
    outside_source = settings.workspace_root.parent / "outside-source"
    outside_source.mkdir(exist_ok=True)
    source.rename(original_source)
    source.symlink_to(outside_source, target_is_directory=True)
    with pytest.raises(ValueError, match="escaped"):
        await service.activate(
            current_project_id="demo", run_id=str(run["run_id"]),
            worker_instance_id=worker, claim_token=str(run["claim_token"]),
            proposal_uri=proposal["proposal_uri"],
            expected_sha256=proposal["proposal_sha256"],
        )
    source.unlink()
    original_source.rename(source)
    activated = await service.activate(
        current_project_id="demo", run_id=str(run["run_id"]),
        worker_instance_id=worker, claim_token=str(run["claim_token"]),
        proposal_uri=proposal["proposal_uri"],
        expected_sha256=proposal["proposal_sha256"],
    )
    assert activated["status"] == "active"
    assert activated["effective_next_run"] is True
    assert activated["idempotent"] is False
    assert activated["receipt_uri"].startswith(
        "vault://demo/runtime/source-scope-receipts/"
    )
    repeated = await service.activate(
        current_project_id="demo", run_id=str(run["run_id"]),
        worker_instance_id=worker, claim_token=str(run["claim_token"]),
        proposal_uri=proposal["proposal_uri"],
        expected_sha256=proposal["proposal_sha256"],
    )
    assert repeated["idempotent"] is True

    scope = WorkspaceScopeService(settings)
    roots = scope.resolve("demo", str(run["run_id"]), None)
    assert {item.key for item in roots} == {"project", "run_work", "vnpy-source"}
    external_root = next(item for item in roots if item.key == "vnpy-source")
    assert external_root.access == "write"
    assert external_root.includes == ()
    assert external_root.excludes == ()
    assert external_root.recursive is True
    assert external_root.publish_allowed is True
    _, readable, _ = scope.resolve_uri(roots, "workspace://vnpy-source/good.py")
    assert readable == source / "good.py"
    _, env_file, _ = scope.resolve_uri(roots, "workspace://vnpy-source/.env")
    assert env_file == source / ".env"
    _, secret_file, _ = scope.resolve_uri(roots, "workspace://vnpy-source/Secrets.yaml")
    assert secret_file == source / "Secrets.yaml"
    (source / "alias.py").symlink_to(source / ".env")
    _, alias_file, _ = scope.resolve_uri(roots, "workspace://vnpy-source/alias.py")
    assert alias_file == source / ".env"
    revoked = await repository.revoke_project_grant_from_claim(
        source_run_id=str(run["run_id"]), worker_instance_id=worker,
        claim_token=str(run["claim_token"]), grant_id=activated["grant_ids"][0],
    )
    assert revoked["status"] == "revoked"
    assert {item.key for item in scope.resolve("demo", str(run["run_id"]), None)} == {"project", "run_work"}


async def test_recursive_parent_grant_supersedes_child_grants(core_settings) -> None:
    settings = core_settings.model_copy(update={
        "workspace_root": core_settings.data_root,
    })
    prepare_bindable_project(settings, "demo")
    parent = settings.data_root / "external"
    child = parent / "trade8" / "strategies"
    child.mkdir(parents=True)
    (settings.data_root / "config" / "source-discovery.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "source-discovery.v1",
            "roots": [{
                "discovery_id": "test-workspace",
                "path": str(settings.data_root),
                "display_path": "TEST",
                "description": "test workspace",
            }],
        }, sort_keys=False),
        encoding="utf-8",
    )
    repository = CoreRepository(settings)
    service = SourceScopeService(settings, repository)
    async with repository.database.connection() as connection:
        child_result = await service.apply_full_directory_grants(
            connection=connection, target_project_id="demo",
            bindings=[{
                "root_key": "strategies", "discovery_id": "test-workspace",
                "relative_path": "external/trade8/strategies",
                "purpose": "strategies",
            }],
            source_binding={"authorization_mode": "test"},
            source_message_id=None, approval_message_id=None,
        )
        await connection.commit()
    async with repository.database.connection() as connection:
        parent_result = await service.apply_full_directory_grants(
            connection=connection, target_project_id="demo",
            bindings=[{
                "root_key": "external", "discovery_id": "test-workspace",
                "relative_path": "external", "purpose": "all external files",
            }],
            source_binding={"authorization_mode": "test"},
            source_message_id=None, approval_message_id=None,
        )
        await connection.commit()
    assert parent_result["superseded_grant_ids"] == child_result["grant_ids"]
    roots = WorkspaceScopeService(settings).resolve("demo", "inspect", None)
    external = [item for item in roots if item.grant_id]
    assert len(external) == 1
    assert external[0].path == parent.resolve()
    assert external[0].includes == ()
    assert external[0].excludes == ()


async def test_source_discovery_is_metadata_only_through_scoped_mcp(core_settings) -> None:
    settings = core_settings.model_copy(update={
        "workspace_root": core_settings.data_root,
    })
    prepare_bindable_project(settings, "demo")
    source = settings.data_root / "external" / "framework"
    source.mkdir(parents=True)
    (source / "interface.py").write_text("SECRET_SOURCE_CONTENT", encoding="utf-8")
    (source / ".env").write_text("SECRET_TOKEN", encoding="utf-8")
    catalog = settings.data_root / "config" / "source-discovery.yaml"
    catalog.write_text(yaml.safe_dump({
        "schema_version": "source-discovery.v1",
        "roots": [{
            "discovery_id": "test-workspace",
            "path": str(settings.data_root),
            "display_path": "TEST",
            "description": "test source root",
        }],
    }, sort_keys=False), encoding="utf-8")
    repository = CoreRepository(settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="api",
        chat_id="chat-1", user_id="user-1", external_event_id="source-mcp:1",
        external_message_id="source-mcp-1", content={"text": "inspect source"},
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_run("test", worker, 60)
    environment = {
        **os.environ,
        "AI_HOME": str(settings.workspace_root),
        "ASSISTANT_DATA_ROOT": str(settings.data_root),
        "ASSISTANT_PROVIDER_SECRETS": str(settings.provider_secrets_path),
        "ASSISTANT_POSTGRES_HOST": settings.postgres_host,
        "ASSISTANT_POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_database,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
        "KERNEL_SCOPE_PROJECT_ID": "demo",
        "KERNEL_SCOPE_RUN_ID": str(claimed["run_id"]),
        "KERNEL_SCOPE_CLAIM_TOKEN": str(claimed["claim_token"]),
        "KERNEL_SCOPE_WORKER_INSTANCE_ID": worker,
        "KERNEL_SCOPE_ALLOWED_TOOLS": (
            "get_authorized_workspace,list_source_discovery_roots,inspect_source_path"
        ),
        "KERNEL_SCOPE_STAGE": "",
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "assistant_kernel.mcp_server.scoped_tools"],
        env=environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            before_handshake = await session.call_tool(
                "inspect_source_path",
                {"discovery_id": "test-workspace", "relative_path": "external/framework"},
            )
            await session.call_tool("get_authorized_workspace", {})
            roots = await session.call_tool("list_source_discovery_roots", {})
            inspected = await session.call_tool(
                "inspect_source_path",
                {"discovery_id": "test-workspace", "relative_path": "external/framework"},
            )
    assert {tool.name for tool in tools.tools} == {
        "get_authorized_workspace", "list_source_discovery_roots", "inspect_source_path",
    }
    assert before_handshake.is_error is False
    before_handshake_payload = json.loads(before_handshake.content[0].text)
    assert before_handshake_payload == {
        "ok": False,
        "tool": "inspect_source_path",
        "error_type": "PermissionError",
        "message": "call get_authorized_workspace before using scoped tools",
        "hint": (
            "权限或 Scope 边界拒绝：核对当前 Run 的授权快照、"
            "grant 账本与用户审批消息后重试。"
        ),
    }
    roots_payload = json.loads(roots.content[0].text)
    inspected_payload = json.loads(inspected.content[0].text)
    assert roots_payload["roots"][0]["discovery_id"] == "test-workspace"
    assert inspected_payload["metadata_only"] is True
    assert any(item["relative_path"] == ".env" for item in inspected_payload["entries"])
    assert "SECRET_SOURCE_CONTENT" not in json.dumps(inspected_payload)
    assert "SECRET_TOKEN" not in json.dumps(inspected_payload)


async def test_explicit_scope_request_resumes_blocked_stage_with_new_run(core_settings) -> None:
    settings = core_settings.model_copy(update={"workspace_root": core_settings.data_root})
    prepare_bindable_project(settings, "demo")
    source = settings.data_root / "external" / "data-tools"
    source.mkdir(parents=True)
    (source / "fetch.py").write_text("print('offline')\n", encoding="utf-8")
    catalog = settings.data_root / "config" / "source-discovery.yaml"
    catalog.write_text(yaml.safe_dump({
        "schema_version": "source-discovery.v1",
        "roots": [{
            "discovery_id": "test-workspace", "path": str(settings.data_root),
            "display_path": "TEST", "description": "test root",
        }],
    }, sort_keys=False), encoding="utf-8")
    settings.service_credentials_path.parent.mkdir(parents=True, exist_ok=True)
    settings.service_credentials_path.write_text(json.dumps({
        "services": {"feishu": {"owner_open_ids": ["owner"]}}
    }), encoding="utf-8")
    repository = CoreRepository(settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="owner", external_event_id="resume:pm",
        external_message_id="resume-pm", content={"text": "authorize TEST/external/data-tools"},
    )
    pm_worker = str(uuid.uuid4())
    pm_run = await repository.claim_run("test", pm_worker, 60)
    stage = await repository.start_task_from_claim(
        source_run_id=str(pm_run["run_id"]), worker_instance_id=pm_worker,
        claim_token=str(pm_run["claim_token"]), goal="inspect data tools",
        stages=["requirements"], change_class="major",
    )
    stage_worker = str(uuid.uuid4())
    assert await repository.claim_run("test", stage_worker, 60) is None
    assert await repository.finish_run(
        str(pm_run["run_id"]), pm_worker, str(pm_run["claim_token"]),
        status="succeeded", result={"handoff": str(stage["run_id"])},
    )
    claimed_stage = await repository.claim_run("test", stage_worker, 60)
    assert str(claimed_stage["run_id"]) == str(stage["run_id"])
    await repository.finalize_agent_blocked(
        run_id=str(stage["run_id"]), worker_instance_id=stage_worker,
        claim_token=str(claimed_stage["claim_token"]),
        summary="needs read scope", artifact_refs=[],
        permission_request={
            "schema_version": "permission-request.v1", "action": "read",
            "reason": "inspect data tools", "resources": [{"rough_path": "external"}],
        },
    )
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="owner", external_event_id="resume:authorize",
        external_message_id="resume-authorize",
        content={"text": "authorize external/data-tools as read-only source"},
    )
    authorization_worker = str(uuid.uuid4())
    authorization_run = await repository.claim_run("test", authorization_worker, 60)
    service = SourceScopeService(settings, repository)
    proposal = service.propose(
        current_project_id="demo", run_id=str(authorization_run["run_id"]),
        target_project_id="demo", reason="inspect data tools",
        resume_blocked_run_id=str(stage["run_id"]),
        bindings=[{
            "root_key": "data-tools", "discovery_id": "test-workspace",
            "relative_path": "external/data-tools", "includes": ["*.py"],
            "write_mode": "gated",
        }],
    )
    activated = await service.activate(
        current_project_id="demo", run_id=str(authorization_run["run_id"]),
        worker_instance_id=authorization_worker,
        claim_token=str(authorization_run["claim_token"]),
        proposal_uri=proposal["proposal_uri"],
        expected_sha256=proposal["proposal_sha256"],
    )
    resumed_id = activated["resumed_stage_run_id"]
    assert resumed_id
    blocked = await repository.get_run(str(stage["run_id"]))
    resumed = await repository.get_run(resumed_id)
    assert blocked["status"] == "cancelled"
    assert blocked["result_json"]["superseded_by_run_id"] == resumed_id
    assert str(resumed["parent_run_id"]) == str(stage["run_id"])
    assert resumed["status"] == "queued"
    assert resumed["request_json"]["permission_resume"]["permission_receipt_uri"]


def test_workspace_scope_snapshot_is_immutable_and_rejects_retargeting(core_settings) -> None:
    settings = core_settings.model_copy(update={
        "workspace_root": core_settings.data_root,
    })
    prepare_bindable_project(settings, "demo")
    first = settings.data_root / "approved-source"
    second = settings.data_root / "later-source"
    outside = settings.data_root.parent / "outside-snapshot-source"
    first.mkdir()
    second.mkdir()
    outside.mkdir()
    scopes = settings.data_root / "config" / "project-scopes.yaml"
    scopes.write_text(yaml.safe_dump({
        "schema_version": "workspace-scope.v1",
        "projects": {"demo": {"roots": [{
            "key": "approved", "path": str(first), "description": "approved root",
            "includes": ["*.py"], "excludes": [".env"],
        }] }},
    }, sort_keys=False), encoding="utf-8")
    service = WorkspaceScopeService(settings)
    uri, digest = service.snapshot("demo", "scope-run", None)
    scopes.write_text(yaml.safe_dump({
        "schema_version": "workspace-scope.v1",
        "projects": {"demo": {"roots": [{
            "key": "later", "path": str(second), "description": "later root",
        }] }},
    }, sort_keys=False), encoding="utf-8")
    assert [item.key for item in service.resolve("demo", "scope-run", None)] == [
        "project", "later", "run_work",
    ]
    repeated_uri, repeated_digest = service.snapshot("demo", "scope-run", None)
    assert (repeated_uri, repeated_digest) == (uri, digest)
    assert [
        item.key for item in service.load_snapshot(
            "demo", "scope-run", None, uri, digest,
        )
    ] == ["project", "approved", "run_work"]
    first.rename(first.with_name("approved-source-moved"))
    first.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError, match="changed or is unavailable"):
        service.load_snapshot("demo", "scope-run", None, uri, digest)


def test_recursive_glob_matches_direct_and_nested_files() -> None:
    patterns = ("strategies/**/*.py",)
    assert WorkspaceScopeService._matches("strategies/direct.py", patterns)
    assert WorkspaceScopeService._matches("strategies/nested/child.py", patterns)
    assert SourceScopeService._matches("strategies/direct.py", patterns)
    assert SourceScopeService._matches("strategies/nested/child.py", patterns)


async def test_real_question_answer_shape_can_create_and_handoff_project(core_settings):
    root = ProjectRuntimeManager(core_settings.data_root).ensure("system").vault_root
    (root / "agent-config.yaml").write_text(
        "agent_key: system-butler\nreasoning_effort: medium\ntimeout_seconds: 60\n"
        "sandbox: read-only\nskills: []\ntools: []\n",
        encoding="utf-8",
    )
    (root / "role.md").write_text("Butler", encoding="utf-8")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="system", agent_key="system-butler", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="project:e2e",
        external_message_id="initial", content={"text": "create a project"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    service = ProjectDirectoryService(core_settings, repository)
    proposal = service.propose(
        str(run["run_id"]), "first-business", "First business Project",
        initial_goal="Start the first task", boundaries=["Private Vault"],
    )
    waiting = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "needs_input", "summary": "proposal ready",
                "questions": [{
                    "question_id": proposal["approval_question_id"],
                    "question_text": "Approve this exact Project proposal?",
                    "required": True,
                }],
            }),
            10, "butler-native",
        )]),
    ).process(run, worker)
    system_plan = json.loads((root / "runs" / str(run["run_id"]) /
                              "resolved" / "execution-plan.json").read_text(encoding="utf-8"))
    assert system_plan["bindings"]["stage_permissions"]["network"] is True
    assert system_plan["bindings"]["native_permission_profile"]
    answered = await repository.ingest_message(
        project_id="system", agent_key="system-butler", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="project:e2e:approve",
        external_message_id="approval", content={"text": "批准"},
        message_type="answer", reply_to_message_id=waiting["question_message_id"],
    )
    assert answered["resumed"] is True
    resumed_worker = str(uuid.uuid4())
    resumed = await repository.claim_run("test", resumed_worker, 60)
    assert resumed["request_json"]["answer"] == {
        "answers": {proposal["approval_question_id"]: "批准"}
    }
    created = await service.create_and_handoff_from_approved_run(
        run_id=str(resumed["run_id"]), worker_instance_id=resumed_worker,
        claim_token=str(resumed["claim_token"]), proposal_uri=proposal["uri"],
        expected_sha256=proposal["sha256"], handoff_summary="Start the first task",
    )
    assert created["pm_run_id"]
    assert created["pm_run_status"] == "queued"
    pm_run = await repository.get_run(created["pm_run_id"])
    assert pm_run["agent_key"] == "project:first-business:manager"
    repeated = await service.create_and_handoff_from_approved_run(
        run_id=str(resumed["run_id"]), worker_instance_id=resumed_worker,
        claim_token=str(resumed["claim_token"]), proposal_uri=proposal["uri"],
        expected_sha256=proposal["sha256"], handoff_summary="Start the first task",
    )
    assert repeated["idempotent"] is True
    assert repeated["pm_run_id"] == created["pm_run_id"]


@pytest.mark.asyncio
async def test_approved_discord_project_creation_provisions_and_moves_pm_run(
    core_settings, monkeypatch,
) -> None:
    prepare_discord_config(core_settings, users=[], owners=["300"], auto_provision=True)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="system", agent_key="system-butler", channel="discord",
        chat_id="999", user_id="300", external_event_id="project:discord",
        external_message_id="initial", content={
            "text": "create a Discord-first project",
            "discord": {"guild_id": "100", "channel_id": "999"},
        },
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    service = ProjectDirectoryService(core_settings, repository)
    proposal = service.propose(
        str(run["run_id"]), "discord-first", "Discord-first Project",
        initial_goal="Start in the generated channel",
    )
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET request_json=request_json || %s WHERE run_id=%s",
            (Jsonb({"answer": {"answers": {
                proposal["approval_question_id"]: "批准"
            }}}), run["run_id"]),
        )
        await connection.commit()

    async def fake_provision(provisioner, **kwargs):
        await provisioner.repository.bind_chat_project(
            channel="discord", chat_id="555", project_id=kwargs["project_id"],
            agent_key=f"project:{kwargs['project_id']}:manager",
            bound_by_user_id=kwargs["bound_by_user_id"],
            source_message_id=kwargs["source_message_id"],
            project_routing_hash=kwargs["project_routing_hash"],
            idempotency_key="test:discord-first:555",
        )
        return {
            "project_id": kwargs["project_id"], "guild_id": kwargs["guild_id"],
            "channel_id": "555", "created": True,
            "binding_changed": True, "recovered": False,
        }

    async def fake_interfaces(provisioner, **kwargs):
        return {
            "project_id": kwargs["project_id"], "guild_id": kwargs["guild_id"],
            "category_id": "500", "category_created": True,
            "pm_channel_id": "555", "supervisor_channel_id": "556",
            "supervisor_created": True,
        }

    monkeypatch.setattr(DiscordProjectProvisioner, "provision", fake_provision)
    monkeypatch.setattr(
        DiscordProjectProvisioner, "provision_supervisor_interface", fake_interfaces
    )
    created = await service.create_and_handoff_from_approved_run(
        run_id=str(run["run_id"]), worker_instance_id=worker,
        claim_token=str(run["claim_token"]), proposal_uri=proposal["uri"],
        expected_sha256=proposal["sha256"],
    )
    assert created["discord_provisioning"]["channel_id"] == "555"
    assert created["discord_provisioning"]["interfaces"]["supervisor_channel_id"] == "556"
    pm_run = await repository.get_run(created["pm_run_id"])
    trigger = await repository.get_message(str(pm_run["trigger_message_id"]))
    assert trigger["channel"] == "discord"
    assert trigger["chat_id"] == "555"
    assert (await repository.resolve_project_chat_binding(
        "discord", "discord-first"
    ))["chat_id"] == "555"
    async with repository.database.connection() as connection:
        handoff_row = await (
            await connection.execute(
                """
                SELECT created_at FROM assistant.messages
                WHERE direction='inbound' AND message_type='handoff'
                  AND project_id='discord-first'
                ORDER BY created_at DESC LIMIT 1
                """
            )
        ).fetchone()
        receipt_rows = list(await (
            await connection.execute(
                """
                SELECT created_at FROM assistant.messages
                WHERE idempotency_key LIKE 'discord-project-%:discord-first:555'
                ORDER BY created_at
                """
            )
        ).fetchall())
    assert len(receipt_rows) == 2
    assert all(item["created_at"] >= handoff_row["created_at"] for item in receipt_rows)


@pytest.mark.asyncio
async def test_existing_project_handoff_ensures_discord_channel_and_is_idempotent(
    core_settings, monkeypatch,
) -> None:
    prepare_discord_config(core_settings, users=[], owners=["300"], auto_provision=True)
    prepare_bindable_project(core_settings, "target")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="system", agent_key="system-butler", channel="discord",
        chat_id="999", user_id="300", external_event_id="existing:source",
        external_message_id="source", content={
            "text": "continue target",
            "discord": {"guild_id": "100", "channel_id": "999"},
        },
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)

    async def fake_provision(provisioner, **kwargs):
        bound = await provisioner.repository.bind_chat_project(
            channel="discord", chat_id="555", project_id=kwargs["project_id"],
            agent_key=f"project:{kwargs['project_id']}:manager",
            bound_by_user_id=kwargs["bound_by_user_id"],
            source_message_id=kwargs["source_message_id"],
            project_routing_hash=kwargs["project_routing_hash"],
            idempotency_key="test:existing-target:555",
        )
        return {
            "project_id": kwargs["project_id"], "guild_id": kwargs["guild_id"],
            "channel_id": "555", "created": bool(bound["changed"]),
            "binding_changed": bool(bound["changed"]),
            "recovered": not bool(bound["changed"]),
        }

    async def fake_interfaces(provisioner, **kwargs):
        return {
            "project_id": kwargs["project_id"], "guild_id": kwargs["guild_id"],
            "category_id": "500", "category_created": False,
            "pm_channel_id": "555", "supervisor_channel_id": "556",
            "supervisor_created": False,
        }

    monkeypatch.setattr(DiscordProjectProvisioner, "provision", fake_provision)
    monkeypatch.setattr(
        DiscordProjectProvisioner, "provision_supervisor_interface", fake_interfaces
    )
    service = ProjectDirectoryService(core_settings, repository)
    first = await service.handoff_to_existing_project_from_claim(
        run_id=str(run["run_id"]), worker_instance_id=worker,
        claim_token=str(run["claim_token"]), target_project_id="target",
        handoff_summary="Continue the existing Project.",
    )
    second = await service.handoff_to_existing_project_from_claim(
        run_id=str(run["run_id"]), worker_instance_id=worker,
        claim_token=str(run["claim_token"]), target_project_id="target",
        handoff_summary="Continue the existing Project.",
    )

    assert first["discord_provisioning"]["channel_id"] == "555"
    assert second["run"]["run_id"] == first["run"]["run_id"]
    target_run = await repository.get_run(str(first["run"]["run_id"]))
    trigger = await repository.get_message(str(target_run["trigger_message_id"]))
    assert trigger["project_id"] == "target"
    assert trigger["chat_id"] == "555"
    assert first["source_receipt"]["chat_id"] == "999"
    assert "<#555>" in first["source_receipt"]["content_json"]["text"]
    assert len(await repository.list_runs("target")) == 1


async def test_major_stage_blocked_is_persisted_and_can_be_unblocked(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="blocked:initial",
        external_message_id="initial", content={"text": "analyze"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    task = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="blocked analysis",
        stages=["requirements"], change_class="major",
    )
    await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    stage_worker = str(uuid.uuid4())
    stage_run = await repository.claim_run("test", stage_worker, 60)
    result = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "blocked", "summary": "missing external dependency",
                "permission_request": {
                    "schema_version": "permission-request.v1", "action": "read",
                    "reason": "inspect dependency",
                    "resources": [{"rough_path": "external/dependency"}],
                },
            }),
            10, "requirements-native",
        )]),
    ).process(stage_run, stage_worker)
    assert result["outcome"] == "blocked"
    assert result["permission_pm_run_id"]
    permission_pm = await repository.get_run(result["permission_pm_run_id"])
    assert permission_pm["agent_key"] == "project:demo:manager"
    assert str(permission_pm["parent_run_id"]) == str(task["run_id"])
    blocked = await repository.get_run(str(task["run_id"]))
    assert blocked["status"] == "blocked"
    assert blocked["result_json"]["outcome"] == "blocked"
    assert blocked["result_json"]["permission_request"]["action"] == "read"
    assert blocked["executor_json"]["stage_permissions"] == {
        "project_workspace_grant": "project_only",
        "workspace_file_access": "read_execute",
        "formal_write": False,
        "direct_workspace_write": False,
        "run_work_write": True,
        "network": True,
            "account_connect": True,
            "trade": True,
    }
    assert await repository.unblock_run(
        str(task["run_id"]), "dependency is now available"
    ) == "queued"


async def test_recovered_chat_completion_follows_prior_blocked_reply(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="discord",
        chat_id="chat-1", user_id="user-1", external_event_id="recovery:initial",
        external_message_id="initial", content={"text": "continue"},
    )
    first_worker = str(uuid.uuid4())
    first_run = await repository.claim_run("test", first_worker, 60)
    first = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({"outcome": "blocked", "summary": "temporary blocker"}),
            10, "recovery-native",
        )]),
    ).process(first_run, first_worker)
    assert first["outcome"] == "blocked"
    blocked_reply = await repository.get_message(first["reply_message_id"])
    assert await repository.unblock_run(
        str(first_run["run_id"]), "blocker fixed"
    ) == "queued"
    second_worker = str(uuid.uuid4())
    second_run = await repository.claim_run("test", second_worker, 60)
    second = await RunProcessor(
        core_settings, repository, FakeHarness([(
            "recovered result", 12, "recovery-native",
        )]),
    ).process(second_run, second_worker)
    assert second["outcome"] == "completed"
    recovered_reply = await repository.get_message(second["reply_message_id"])
    assert str(recovered_reply["reply_to_message_id"]) == str(blocked_reply["message_id"])
    assert recovered_reply["content_json"]["text"] == "recovered result"


async def test_legacy_network_request_does_not_create_an_authorization_run(core_settings):
    prepare_agent(core_settings)
    prepare_stage(core_settings, "demo", "requirements")
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="network:request",
        external_message_id="network-request", content={"text": "inspect remote data"},
    )
    manager_worker = str(uuid.uuid4())
    manager = await repository.claim_run("test", manager_worker, 60)
    task = await repository.start_task_from_claim(
        source_run_id=str(manager["run_id"]), worker_instance_id=manager_worker,
        claim_token=str(manager["claim_token"]), goal="inspect remote data",
        stages=["requirements"], change_class="major",
    )
    await repository.finish_run(
        str(manager["run_id"]), manager_worker, str(manager["claim_token"]),
        status="succeeded",
    )
    stage_worker = str(uuid.uuid4())
    stage_run = await repository.claim_run("test", stage_worker, 60)
    result = await RunProcessor(
        core_settings, repository, FakeHarness([(
            json.dumps({
                "outcome": "blocked", "summary": "need market data",
                "permission_request": {
                    "schema_version": "permission-request.v1", "action": "network",
                    "reason": "historical bars", "resources": [
                        {"rough_path": "https://data.alpaca.markets/v2/stocks/bars"}
                    ],
                },
            }),
            10, "requirements-network",
        )]),
    ).process(stage_run, stage_worker)
    assert result["outcome"] == "blocked"
    assert result["permission_pm_run_id"] is None
    blocked = await repository.get_run(str(task["run_id"]))
    assert blocked["result_json"]["permission_request"] is None
    assert "不使用独立动作授权" in blocked["result_json"]["summary"]


async def test_rotation_creates_parent_linked_session(core_settings):
    prepare_agent(core_settings)
    settings = core_settings.model_copy(
        update={"context_soft_ratio": 0.5, "context_hard_ratio": 0.75, "default_context_window_tokens": 100}
    )
    repository = CoreRepository(settings)
    harness = FakeHarness([("first", 60, "native-1"), ("second", 10, "native-2")])
    processor = RunProcessor(settings, repository, harness)

    first_session_id = None
    first_run_id = None
    for event_id, text in (("one", "first"), ("two", "second")):
        await repository.ingest_message(
            project_id="demo",
            agent_key="project:demo:manager",
            channel="feishu",
            chat_id="chat-1",
            user_id="user-1",
            external_event_id=f"app:{event_id}",
            external_message_id=event_id,
            content={"text": text},
        )
        worker = str(uuid.uuid4())
        run = await repository.claim_run("test", worker, 60)
        await processor.process(run, worker)
        if event_id == "one":
            first_run_id = str(run["run_id"])
            first_session = await repository.current_session(
                "demo", "project:demo:manager", "feishu", "chat-1"
            )
            first_session_id = str(first_session["session_id"])
            assert first_session["status"] == "active"
            assert first_session["handoff_checkpoint_json"] == {}
            archived = await repository.get_run(first_run_id)
            history = archived["executor_json"]["usage_history"]
            assert len(history) == 1
            assert history[0]["input_tokens"] == 60
            assert history[0]["context_window_tokens"] == 100
            assert history[0]["context_ratio"] == 0.6

    current = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    assert current["native_session_id"] == "native-2"
    assert str(current["parent_session_id"]) == first_session_id
    parent = await repository.get_session(str(current["parent_session_id"]))
    assert parent["status"] == "sealed"
    assert parent["handoff_checkpoint_json"]["schema_version"] == "session-handoff.v3"
    assert parent["handoff_checkpoint_json"]["lineage"]["from_session_id"] == first_session_id
    assert harness.requests[1].physical_session_id is None


async def test_waiting_turn_records_usage_and_answer_starts_fresh_session(core_settings):
    prepare_agent(core_settings)
    settings = core_settings.model_copy(
        update={"context_soft_ratio": 0.5, "context_hard_ratio": 0.75,
                "default_context_window_tokens": 100}
    )
    repository = CoreRepository(settings)
    inbound = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="wait-rotate:initial",
        external_message_id="initial", content={"text": "start"},
    )
    response = json.dumps({
        "outcome": "needs_input", "summary": "need answer",
        "questions": [{
            "question_id": "q1", "question_text": "Continue?", "required": True,
        }],
    })
    harness = FakeHarness([(response, 60, "native-1"), ("done", 10, "native-2")])
    processor = RunProcessor(settings, repository, harness)
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    waiting = await processor.process(run, worker)
    waiting_run = await repository.get_run(str(run["run_id"]))
    old_session = await repository.get_session(str(waiting_run["session_id"]))
    assert old_session["status"] == "active"
    assert old_session["handoff_checkpoint_json"] == {}
    assert waiting_run["executor_json"]["usage_history"][0]["input_tokens"] == 60
    assert waiting_run["result_json"]["token_usage"]["context_ratio"] == 0.6
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="wait-rotate:answer",
        external_message_id="answer", content={"text": "yes"}, message_type="answer",
        reply_to_message_id=waiting["question_message_id"],
    )
    next_worker = str(uuid.uuid4())
    resumed = await repository.claim_run("test", next_worker, 60)
    await processor.process(resumed, next_worker)
    current = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    assert current["parent_session_id"] == old_session["session_id"]
    assert current["handoff_checkpoint_json"]["schema_version"] == "session-handoff.v3"
    assert harness.requests[1].physical_session_id is None


async def test_session_preflight_rotates_stale_active_ratio(core_settings):
    settings = core_settings.model_copy(
        update={"context_soft_ratio": 0.5, "context_hard_ratio": 0.75,
                "default_context_window_tokens": 100}
    )
    repository = CoreRepository(settings)
    original = await repository.ensure_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.sessions SET input_tokens=60,context_window_tokens=100,"
            "usage_known=true "
            "WHERE session_id=%s",
            (original["session_id"],),
        )
        await connection.commit()
    checkpoint = {
        "schema_version": "session-handoff.v2",
        "lineage": {"from_session_id": str(original["session_id"])},
        "cumulative_state": {"next_action": "continue"},
        "coverage": {"to_message_id": None},
    }
    checkpoint["content_sha256"] = canonical_sha256(checkpoint)
    rotated = await repository.ensure_session(
        "demo", "project:demo:manager", "feishu", "chat-1",
        rotation_checkpoint=checkpoint,
    )
    assert rotated["session_id"] != original["session_id"]
    assert rotated["parent_session_id"] == original["session_id"]
    assert rotated["handoff_checkpoint_json"]["schema_version"] == "session-handoff.v2"


async def test_session_does_not_rotate_at_exactly_half(core_settings):
    settings = core_settings.model_copy(
        update={"context_soft_ratio": 0.5, "context_hard_ratio": 0.75,
                "default_context_window_tokens": 100}
    )
    repository = CoreRepository(settings)
    original = await repository.ensure_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.sessions SET input_tokens=50,context_window_tokens=100,"
            "usage_known=true "
            "WHERE session_id=%s",
            (original["session_id"],),
        )
        await connection.commit()
    same = await repository.ensure_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    assert same["session_id"] == original["session_id"]


async def test_usage_is_persisted_before_agent_failure(core_settings):
    prepare_agent(core_settings)
    repository = CoreRepository(core_settings)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="failed:initial",
        external_message_id="failed", content={"text": "fail after completion"},
    )
    worker = str(uuid.uuid4())
    run = await repository.claim_run("test", worker, 60)
    processor = RunProcessor(
        core_settings, repository,
        FakeHarness([(json.dumps({"outcome": "failed", "summary": "no"}), 42, "native-f")]),
    )
    with pytest.raises(RuntimeError, match="no"):
        await processor.process(run, worker)
    persisted = await repository.get_run(str(run["run_id"]))
    assert persisted["executor_json"]["usage_history"][0]["input_tokens"] == 42
    session = await repository.get_session(str(persisted["session_id"]))
    assert session["input_tokens"] == 42
    assert session["usage_known"] is True


async def test_unknown_usage_rotates_on_next_launch(core_settings):
    prepare_agent(core_settings)
    settings = core_settings.model_copy(update={"default_context_window_tokens": 100})
    repository = CoreRepository(settings)
    harness = FakeHarness([
        ("first", 0, "native-unknown", False),
        ("second", 10, "native-fresh", True),
    ])
    processor = RunProcessor(settings, repository, harness)
    first_session_id = None
    for index in (1, 2):
        await repository.ingest_message(
            project_id="demo", agent_key="project:demo:manager", channel="feishu",
            chat_id="chat-1", user_id="user-1", external_event_id=f"unknown:{index}",
            external_message_id=f"unknown-{index}", content={"text": str(index)},
        )
        worker = str(uuid.uuid4())
        run = await repository.claim_run("test", worker, 60)
        await processor.process(run, worker)
        current = await repository.current_session(
            "demo", "project:demo:manager", "feishu", "chat-1"
        )
        if index == 1:
            first_session_id = str(current["session_id"])
            assert current["usage_known"] is False
    assert str(current["parent_session_id"]) == first_session_id
    assert harness.requests[1].physical_session_id is None


async def test_second_rotation_carries_previous_checkpoint(core_settings):
    prepare_agent(core_settings)
    settings = core_settings.model_copy(update={
        "context_soft_ratio": 0.5, "context_hard_ratio": 0.75,
        "default_context_window_tokens": 100,
    })
    repository = CoreRepository(settings)
    harness = FakeHarness([
        ("first", 60, "native-1"),
        ("second", 60, "native-2"),
        ("third", 10, "native-3"),
    ])
    processor = RunProcessor(settings, repository, harness)
    run_ids = []
    for index in (1, 2, 3):
        await repository.ingest_message(
            project_id="demo", agent_key="project:demo:manager", channel="feishu",
            chat_id="chat-1", user_id="user-1", external_event_id=f"chain:{index}",
            external_message_id=f"chain-{index}", content={"text": str(index)},
        )
        worker = str(uuid.uuid4())
        run = await repository.claim_run("test", worker, 60)
        run_ids.append(str(run["run_id"]))
        await processor.process(run, worker)
    current = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    parent = await repository.get_session(str(current["parent_session_id"]))
    checkpoint = parent["handoff_checkpoint_json"]
    carried = {
        str(item.get("run_id")) for item in checkpoint["cumulative_state"]["completed"]
    }
    assert set(run_ids[:2]) <= carried
    assert checkpoint["based_on_checkpoint_sha256"]
    source = checkpoint["coverage"]["source_bundle"]
    relative = source["uri"].split("/", 3)[-1]
    source_file = ProjectRuntimeManager(settings.data_root).resolve_vault_file("demo", relative)
    assert source_file.is_file()
    assert source["sha256"] == hashlib.sha256(source_file.read_bytes()).hexdigest()


async def test_rotation_uses_valid_model_summary(core_settings, monkeypatch):
    prepare_agent(core_settings)
    core_settings.provider_secrets_path.write_text(json.dumps({
        "default_provider": "test",
        "providers": {
            "test": {
                "enabled": True,
                "api_key": "test-only",
                "openai_compatible_base_url": "https://invalid.example",
                "chat_models": {"summary-model": {}},
            }
        },
    }), encoding="utf-8")

    captured = {}

    async def fake_chat(self, prompt, model, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt")
        return SimpleNamespace(
            provider="test", model=model, request_id="summary-request",
            text=json.dumps({
                "goal": "preserve continuity",
                "confirmed_facts": ["fact-one"],
                "decisions": ["decision-one"],
                "constraints": ["constraint-one"],
                "next_action": "continue safely",
            }),
        )

    monkeypatch.setattr(
        "assistant_kernel.services.run_processor.CatalogChatProvider.chat", fake_chat
    )
    settings = core_settings.model_copy(update={
        "context_soft_ratio": 0.5, "context_hard_ratio": 0.75,
        "default_context_window_tokens": 100,
    })
    repository = CoreRepository(settings)
    harness = FakeHarness([("first", 60, "native-1"), ("second", 10, "native-2")])
    processor = RunProcessor(settings, repository, harness)
    for index in (1, 2):
        await repository.ingest_message(
            project_id="demo", agent_key="project:demo:manager", channel="feishu",
            chat_id="chat-1", user_id="user-1", external_event_id=f"model:{index}",
            external_message_id=f"model-{index}", content={"text": str(index)},
        )
        worker = str(uuid.uuid4())
        run = await repository.claim_run("test", worker, 60)
        await processor.process(run, worker)
    current = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    checkpoint = current["handoff_checkpoint_json"]
    assert checkpoint["quality"]["summary_mode"] == "model"
    assert checkpoint["quality"]["request_id"] == "summary-request"
    assert checkpoint["cumulative_state"]["confirmed_facts"] == ["fact-one"]
    assert "assistant-session-rotation-handoff" in captured["system_prompt"]


async def test_role_change_rotates_native_session_with_handoff(core_settings):
    prepare_agent(core_settings)
    settings = core_settings.model_copy(update={
        "context_soft_ratio": 0.9,
        "context_hard_ratio": 0.95,
        "default_context_window_tokens": 100,
    })
    repository = CoreRepository(settings)
    harness = FakeHarness([("first", 10, "native-1"), ("second", 10, "native-2")])
    processor = RunProcessor(settings, repository, harness)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="config:one",
        external_message_id="config-one", content={"text": "first"},
    )
    worker = str(uuid.uuid4())
    first_run = await repository.claim_run("test", worker, 60)
    await processor.process(first_run, worker)
    first_session = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )

    role_path = ProjectRuntimeManager(settings.data_root).ensure("demo").vault_root / "role.md"
    role_path.write_text(role_path.read_text(encoding="utf-8") + "\nChanged role.\n", encoding="utf-8")
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="config:two",
        external_message_id="config-two", content={"text": "second"},
    )
    second_worker = str(uuid.uuid4())
    second_run = await repository.claim_run("test", second_worker, 60)
    await processor.process(second_run, second_worker)
    current = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )

    assert current["session_id"] != first_session["session_id"]
    assert current["parent_session_id"] == first_session["session_id"]
    assert current["handoff_checkpoint_json"]["reason"] == "config_changed"


async def test_unknown_legacy_session_compatibility_rotates_before_resume(core_settings):
    prepare_agent(core_settings)
    settings = core_settings.model_copy(update={
        "context_soft_ratio": 0.9,
        "context_hard_ratio": 0.95,
        "default_context_window_tokens": 100,
    })
    repository = CoreRepository(settings)
    harness = FakeHarness([("first", 10, "native-1"), ("second", 10, "native-2")])
    processor = RunProcessor(settings, repository, harness)
    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="legacy-config:one",
        external_message_id="legacy-config-one", content={"text": "first"},
    )
    first_worker = str(uuid.uuid4())
    first_run = await repository.claim_run("test", first_worker, 60)
    await processor.process(first_run, first_worker)
    first_session = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET executor_json=executor_json - 'session_compatibility_hash' "
            "WHERE session_id=%s",
            (first_session["session_id"],),
        )
        await connection.commit()

    await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="legacy-config:two",
        external_message_id="legacy-config-two", content={"text": "second"},
    )
    second_worker = str(uuid.uuid4())
    second_run = await repository.claim_run("test", second_worker, 60)
    await processor.process(second_run, second_worker)
    current = await repository.current_session(
        "demo", "project:demo:manager", "feishu", "chat-1"
    )

    assert current["session_id"] != first_session["session_id"]
    assert current["parent_session_id"] == first_session["session_id"]
    assert current["handoff_checkpoint_json"]["reason"] == "config_compatibility_unknown"
    assert harness.requests[1].physical_session_id is None


async def test_expired_delivery_lease_is_reclaimed(core_settings):
    repository = CoreRepository(core_settings)
    message = await repository.create_outbound(
        project_id="demo",
        session_id=None,
        run_id=None,
        channel="feishu",
        chat_id="chat-1",
        user_id="user-1",
        message_type="reply",
        content={"text": "hello"},
        reply_to_message_id=None,
        idempotency_key="delivery-reclaim",
    )
    first_worker = str(uuid.uuid4())
    assert await repository.claim_delivery("sender", first_worker, 60, "feishu")
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.messages SET lease_until=now()-interval '1 second' WHERE message_id=%s",
            (message["message_id"],),
        )
        await connection.commit()
    second_worker = str(uuid.uuid4())
    reclaimed = await repository.claim_delivery("sender", second_worker, 60, "feishu")
    assert str(reclaimed["message_id"]) == str(message["message_id"])
    assert str(reclaimed["worker_instance_id"]) == second_worker


async def test_feishu_slash_switches_to_dynamic_project_agent_without_llm_run(core_settings):
    root = ProjectRuntimeManager(core_settings.data_root).ensure("slash-demo").vault_root
    (root / "project.yaml").write_text(yaml.safe_dump({
        "schema_version": "project.v2",
        "project_id": "slash-demo",
        "layout_version": 2,
        "status": "active",
        "default_agent": "pm",
    }), encoding="utf-8")
    for agent_id, agent_key, aliases, slash_enabled in (
        ("pm", "project:slash-demo:manager", ["manager"], True),
        ("analyst", "project:slash-demo:analyst", ["analysis"], True),
        ("implementation", "project:slash-demo:implementation", [], False),
    ):
        agent_root = root / "agents" / agent_id
        agent_root.mkdir(parents=True)
        (agent_root / "config.yaml").write_text(yaml.safe_dump({
            "schema_version": "agent-config.v2",
            "agent_id": agent_id,
            "agent_key": agent_key,
            "kind": "manager" if agent_id == "pm" else "business",
            "status": "active",
            "display_name": agent_id.title(),
            "aliases": aliases,
            "role": "role.md",
            "skills": [],
            "scope_profile": "current-project",
            "mcp_servers": {},
            "entrypoints": {
                "pm_handoff": True,
                "feishu_slash": slash_enabled,
            },
            "triggers": ["user_message"],
            "harness": {"sandbox": "read-only", "timeout_seconds": 60},
        }, sort_keys=False), encoding="utf-8")
        (agent_root / "role.md").write_text(f"# {agent_id}\n", encoding="utf-8")

    event = {
        "header": {"event_id": "slash:switch"},
        "kernel": {
            "project_id": "slash-demo",
            "agent_key": "project:slash-demo:manager",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1",
                "message_id": "slash-message",
                "content": json.dumps({"text": "/agent analysis"}),
            },
        },
    }
    first = await ingest_event(
        core_settings, event, "app-1", allow_explicit_route=True,
    )
    duplicate = await ingest_event(
        core_settings, event, "app-1", allow_explicit_route=True,
    )
    repository = CoreRepository(core_settings)

    assert first["run"] is None
    assert first["command"] == "/agent"
    assert duplicate["duplicate"] is True
    assert await repository.resolve_route_binding("feishu", "chat-1", "user-1") == (
        "slash-demo", "project:slash-demo:analyst"
    )

    assert await repository.list_runs("slash-demo") == []

    follow_up = await ingest_event(core_settings, {
        "header": {"event_id": "slash:follow-up"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1",
                "message_id": "follow-up",
                "content": json.dumps({"text": "analyze this"}),
            },
        },
    }, "app-1")
    assert follow_up["run"]["agent_key"] == "project:slash-demo:analyst"

    denied = await ingest_event(core_settings, {
        "header": {"event_id": "slash:denied"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1",
                "message_id": "denied",
                "content": json.dumps({"text": "/agent implementation"}),
            },
        },
    }, "app-1")
    assert "无法切换Agent" in denied["outbound"]["content_json"]["text"]
    assert await repository.resolve_route_binding("feishu", "chat-1", "user-1") == (
        "slash-demo", "project:slash-demo:analyst"
    )

    switched_pm = await ingest_event(core_settings, {
        "header": {"event_id": "slash:pm"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "message_id": "pm",
                "content": json.dumps({"text": "/pm"}),
            },
        },
    }, "app-1")
    assert switched_pm["run"] is None
    assert switched_pm["outbound"]["content_json"]["text"] == (
        "Hi，我是 Project slash-demo 的 Project Manager。\n"
        "现在已进入这个项目，有什么需要？"
    )
    assert await repository.resolve_route_binding("feishu", "chat-1", "user-1") == (
        "slash-demo", "project:slash-demo:manager"
    )

    returned = await ingest_event(core_settings, {
        "header": {"event_id": "slash:butler"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1", "message_id": "butler",
                "content": json.dumps({"text": "/butler"}),
            },
        },
    }, "app-1")
    assert returned["run"] is None
    assert returned["outbound"]["content_json"]["text"] == (
        "Hi，我是你的个人超级助理大管家。\n"
        "现在已回到大管家总入口，有什么需要？"
    )
    assert await repository.resolve_route_binding("feishu", "chat-1", "user-1") == (
        "system", "system-butler"
    )


async def test_platform_binding_is_one_project_to_one_chat(
    core_settings,
) -> None:
    repository = CoreRepository(core_settings)
    first = await repository.bind_chat_project(
        channel="feishu", chat_id="chat-a", project_id="demo",
        agent_key="project:demo:manager", bound_by_user_id="owner",
        source_message_id=str(uuid.uuid4()), project_routing_hash="a" * 64,
        idempotency_key="bind-chat-a",
    )
    assert first["changed"] is True
    assert (await repository.resolve_chat_project_binding("feishu", "chat-a"))["project_id"] == "demo"
    assert (await repository.resolve_project_chat_binding("feishu", "demo"))["chat_id"] == "chat-a"
    with pytest.raises(ValueError, match="already bound to chat chat-a"):
        await repository.bind_chat_project(
            channel="feishu", chat_id="chat-b", project_id="demo",
            agent_key="project:demo:manager", bound_by_user_id="owner",
            source_message_id=str(uuid.uuid4()), project_routing_hash="a" * 64,
            idempotency_key="bind-chat-b-denied",
        )
    with pytest.raises(ValueError, match="already bound"):
        await repository.bind_chat_project(
            channel="feishu", chat_id="chat-a", project_id="other",
            agent_key="project:other:manager", bound_by_user_id="owner",
            source_message_id=str(uuid.uuid4()), project_routing_hash="b" * 64,
            idempotency_key="bind-chat-a-other",
        )

    unbound = await repository.unbind_chat_project(
        channel="feishu", chat_id="chat-a", expected_project_id="demo",
        unbound_by_user_id="owner", source_message_id=str(uuid.uuid4()),
        idempotency_key="unbind-chat-a",
    )
    assert unbound["changed"] is True
    assert await repository.resolve_chat_project_binding("feishu", "chat-a") is None
    second = await repository.bind_chat_project(
        channel="feishu", chat_id="chat-b", project_id="demo",
        agent_key="project:demo:manager", bound_by_user_id="owner",
        source_message_id=str(uuid.uuid4()), project_routing_hash="a" * 64,
        idempotency_key="bind-chat-b",
    )
    assert second["changed"] is True
    assert (await repository.resolve_chat_project_binding("feishu", "chat-b"))["project_id"] == "demo"


async def test_feishu_group_binding_is_available_to_all_users_and_routes_without_llm(
    core_settings,
) -> None:
    credentials = core_settings.data_root / "service-credentials.json"
    credentials.write_text(json.dumps({
        "services": {"feishu": {"owner_open_ids": ["owner"]}}
    }), encoding="utf-8")
    settings = core_settings.model_copy(update={"service_credentials_path": credentials})
    prepare_bindable_project(settings)
    repository = CoreRepository(settings)

    bound_by_other = await ingest_event(settings, {
        "header": {"event_id": "bind:denied"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "other"}},
            "message": {
                "chat_id": "chat-a", "chat_type": "group", "message_id": "denied",
                "content": json.dumps({"text": "/bind_project demo"}),
            },
        },
    }, "app-1")
    assert bound_by_other["run"] is None
    assert bound_by_other["command"] == "/bind_project"
    assert "无权" in bound_by_other["outbound"]["content_json"]["text"]
    assert await repository.resolve_chat_project_binding("feishu", "chat-a") is None

    bound = await ingest_event(settings, {
        "header": {"event_id": "bind:allowed"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat-a", "chat_type": "group", "message_id": "allowed",
                "content": json.dumps({"text": "@_user_1 /bind_project demo"}),
            },
        },
    }, "app-1")
    assert bound["run"] is None
    assert bound["command"] == "/bind_project"
    assert (await repository.resolve_chat_project_binding("feishu", "chat-a"))["project_id"] == "demo"

    routed = await ingest_event(settings, {
        "header": {"event_id": "bind:follow-up"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat-a", "chat_type": "group", "message_id": "follow-up",
                "content": json.dumps({"text": "continue"}),
            },
        },
    }, "app-1")
    assert routed["message"]["project_id"] == "demo"
    assert routed["run"]["agent_key"] == "project:demo:manager"

    blocked_switch = await ingest_event(settings, {
        "header": {"event_id": "bind:blocked-switch"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat-a", "chat_type": "group", "message_id": "switch",
                "content": json.dumps({"text": "/agent review"}),
            },
        },
    }, "app-1")
    assert blocked_switch["run"] is None
    assert "不允许直接切换Agent" in blocked_switch["outbound"]["content_json"]["text"]
    assert (await repository.resolve_chat_project_binding("feishu", "chat-a"))["project_id"] == "demo"

    wrong_unbind = await ingest_event(settings, {
        "header": {"event_id": "bind:wrong-unbind"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat-a", "chat_type": "group", "message_id": "wrong-unbind",
                "content": json.dumps({"text": "/unbind_project other"}),
            },
        },
    }, "app-1")
    assert "解除绑定失败" in wrong_unbind["outbound"]["content_json"]["text"]
    assert (await repository.resolve_chat_project_binding("feishu", "chat-a"))["project_id"] == "demo"

    unbound = await ingest_event(settings, {
        "header": {"event_id": "bind:unbind"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat-a", "chat_type": "group", "message_id": "unbind",
                "content": json.dumps({"text": "/unbind_project demo"}),
            },
        },
    }, "app-1")
    assert unbound["run"] is None
    assert "已解除" in unbound["outbound"]["content_json"]["text"]
    assert await repository.resolve_chat_project_binding("feishu", "chat-a") is None

    unbound_group = await ingest_event(settings, {
        "header": {"event_id": "bind:unbound-group"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "owner"}},
            "message": {
                "chat_id": "chat-b", "chat_type": "group", "message_id": "unbound",
                "content": json.dumps({"text": "hello"}),
            },
        },
    }, "app-1")
    assert unbound_group["run"] is None
    assert unbound_group["unbound_group"] is True


def test_feishu_websocket_event_is_normalized() -> None:
    event = SimpleNamespace(
        header=SimpleNamespace(
            event_id="event-ws", event_type="im.message.receive_v1",
            app_id="app", tenant_key="tenant",
        ),
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="open-user", user_id="user"),
            ),
            message=SimpleNamespace(
                message_id="message-ws", chat_id="chat-ws", parent_id="",
                root_id="", chat_type="group", message_type="text",
                content='{"text":"hello"}',
            ),
        ),
    )
    payload = _websocket_message_payload(event)
    assert payload["header"]["event_id"] == "event-ws"
    assert payload["event"]["message"]["chat_id"] == "chat-ws"
    assert payload["event"]["message"]["chat_type"] == "group"
    assert payload["event"]["sender"]["sender_id"]["open_id"] == "open-user"


async def test_feishu_event_deduplication_and_stable_delivery_uuid(core_settings):
    repository = CoreRepository(core_settings)
    event = {
        "header": {"event_id": "event-1"},
        "kernel": {"project_id": "demo", "agent_key": "project:demo:manager"},
        "event": {
            "sender": {"sender_id": {"open_id": "user-1"}},
            "message": {
                "chat_id": "chat-1",
                "message_id": "incoming-1",
                "content": json.dumps({"text": "hello"}),
            },
        },
    }
    first = await ingest_event(core_settings, event, "app-1", allow_explicit_route=True)
    duplicate = await ingest_event(core_settings, event, "app-1", allow_explicit_route=True)
    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True

    outbound = await repository.create_outbound(
        project_id="demo",
        session_id=None,
        run_id=None,
        channel="feishu",
        chat_id="chat-1",
        user_id="user-1",
        message_type="reply",
        content={"text": "done"},
        reply_to_message_id=str(first["message"]["message_id"]),
        idempotency_key="reply:event-1",
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_delivery("sender", worker, 60, "feishu")
    observed_uuid = None
    observed_path = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_uuid, observed_path
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        body = json.loads(request.content)
        observed_uuid = body["uuid"]
        observed_path = request.url.path
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "sent-1"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    ) as client:
        await FeishuSender(
            core_settings,
            repository,
            {"enabled": True, "app_id": "app-1", "app_secret": "secret"},
            client,
        ).send(claimed, worker)
    assert observed_uuid == str(outbound["message_id"])
    assert observed_path == "/open-apis/im/v1/messages/incoming-1/reply"
    sent = await repository.get_message(str(outbound["message_id"]))
    assert sent["delivery_status"] == "sent"
    assert sent["external_message_id"] == "sent-1"


async def test_feishu_reply_rejection_falls_back_to_chat_message(core_settings):
    repository = CoreRepository(core_settings)
    inbound = await repository.ingest_message(
        project_id="demo", agent_key="project:demo:manager", channel="feishu",
        chat_id="chat-1", user_id="user-1", external_event_id="fallback:inbound",
        external_message_id="withdrawn-message", content={"text": "withdraw later"},
    )
    outbound = await repository.create_outbound(
        project_id="demo", session_id=None, run_id=None, channel="feishu",
        chat_id="chat-1", user_id="user-1", message_type="reply",
        content={"text": "still deliver this"},
        reply_to_message_id=str(inbound["message"]["message_id"]),
        idempotency_key="fallback:outbound",
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_delivery("sender", worker, 60, "feishu")
    observed_paths = []
    observed_uuids = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        observed_paths.append(request.url.path)
        body = json.loads(request.content)
        observed_uuids.append(body["uuid"])
        if request.url.path.endswith("/reply"):
            return httpx.Response(400, json={"code": 230001, "msg": "message recalled"})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "fallback-sent"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    ) as client:
        await FeishuSender(
            core_settings, repository,
            {"enabled": True, "app_id": "app-1", "app_secret": "secret"}, client,
        ).send(claimed, worker)
    assert observed_paths == [
        "/open-apis/im/v1/messages/withdrawn-message/reply",
        "/open-apis/im/v1/messages",
    ]
    assert observed_uuids == [str(outbound["message_id"])] * 2
    sent = await repository.get_message(str(outbound["message_id"]))
    assert sent["delivery_status"] == "sent"
    assert sent["external_message_id"] == "fallback-sent"
    assert sent["content_json"]["delivery"]["mode"] == "chat_fallback"
    assert sent["content_json"]["delivery"]["reply_failure"]["code"] == 230001


async def test_feishu_sender_uses_interactive_payload_for_approval(core_settings):
    repository = CoreRepository(core_settings)
    approval = await repository.create_outbound(
        project_id="demo", session_id=None, run_id=None, channel="feishu",
        chat_id="chat-1", user_id="user-1", message_type="approval",
        content={"text": "Approve the requirements"}, reply_to_message_id=None,
        idempotency_key="approval:card",
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_delivery("sender", worker, 60, "feishu")
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        body = json.loads(request.content)
        observed.update(body)
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "card-sent"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    ) as client:
        await FeishuSender(
            core_settings, repository,
            {"enabled": True, "app_id": "app-1", "app_secret": "secret"}, client,
        ).send(claimed, worker)
    assert observed["msg_type"] == "interactive"
    card = json.loads(observed["content"])
    assert card["header"]["title"]["content"] == "需要批准"
    assert (await repository.get_message(str(approval["message_id"])))[
        "delivery_status"
    ] == "sent"


async def test_feishu_question_card_falls_back_to_option_text(core_settings):
    repository = CoreRepository(core_settings)
    question = await repository.create_outbound(
        project_id="demo", session_id=None, run_id=None, channel="feishu",
        chat_id="chat-1", user_id="user-1", message_type="question",
        content={
            "text": "Which path?\n\n1. Path A\n2. Path B",
            "question_id": "path", "interaction_seq": 1,
            "options": [
                {"id": "a", "label": "Path A", "description": ""},
                {"id": "b", "label": "Path B", "description": ""},
            ],
        },
        reply_to_message_id=None, idempotency_key="question:card-fallback",
    )
    worker = str(uuid.uuid4())
    claimed = await repository.claim_delivery("sender", worker, 60, "feishu")
    sent_types = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        body = json.loads(request.content)
        sent_types.append(body["msg_type"])
        if body["msg_type"] == "interactive":
            return httpx.Response(200, json={"code": 230001, "msg": "unsupported card"})
        assert "Path A" in json.loads(body["content"])["text"]
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "text-sent"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    ) as client:
        await FeishuSender(
            core_settings, repository,
            {"enabled": True, "app_id": "app-1", "app_secret": "secret"}, client,
        ).send(claimed, worker)
    assert sent_types == ["interactive", "text"]
    stored = await repository.get_message(str(question["message_id"]))
    assert stored["delivery_status"] == "sent"
    assert stored["content_json"]["delivery"]["mode"] == "question_text_fallback"


async def _force_reconciling(repository: CoreRepository, run_id: str) -> None:
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET status='reconciling',worker_id=NULL,"
            "worker_instance_id=NULL,claim_token=NULL,lease_until=NULL,"
            "process_pid=NULL,process_start_ticks=NULL,updated_at=now() "
            "WHERE run_id=%s",
            (run_id,),
        )
        await connection.commit()


async def test_auto_requeue_reconciled_uses_bounded_backoff(core_settings):
    repository = CoreRepository(core_settings)
    prepare_agent(core_settings, "recovery-demo")
    run = await repository.create_run(
        project_id="recovery-demo", agent_key="project:recovery-demo:python",
        run_type="python", request={"source_uri": "vault://recovery-demo/x.py"},
        idempotency_key="recovery-backoff-1",
    )
    await _force_reconciling(repository, str(run["run_id"]))
    status = await repository.auto_requeue_reconciled(
        run_id=str(run["run_id"]), reason="tier test",
    )
    assert status == "queued"
    row = await repository.get_run(str(run["run_id"]))
    assert row["error_json"]["type"] == "AutoRequeued"
    # Exhausted attempts dead-letter instead of looping forever.
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET attempt=max_attempts,status='reconciling' "
            "WHERE run_id=%s", (str(run["run_id"]),),
        )
        await connection.commit()
    status = await repository.auto_requeue_reconciled(
        run_id=str(run["run_id"]), reason="tier test exhausted",
    )
    assert status == "dead_letter"
    assert await repository.auto_requeue_reconciled(
        run_id=str(uuid.uuid4()), reason="already resolved",
    ) == "not_reconciling"


async def test_recovery_tier1_auto_requeues_supervisor_consultation(core_settings):
    repository = CoreRepository(core_settings)
    prepare_agent(core_settings, "recovery-demo")
    message = await repository.ingest_message(
        project_id="recovery-demo", agent_key="project:recovery-demo:supervisor",
        channel="discord", chat_id="sup", user_id="u1",
        external_event_id="recovery-sup", external_message_id="recovery-sup",
        content={"text": "consult"},
    )
    run = await repository.create_run(
        project_id="recovery-demo",
        agent_key="project:recovery-demo:supervisor",
        run_type="chat",
        trigger_message_id=str(message["message"]["message_id"]),
        request={
            "interaction_kind": "supervisor_consultation",
            "task_id": "t", "trigger": "user_consultation",
        },
        idempotency_key="recovery-sup-1",
    )
    await _force_reconciling(repository, str(run["run_id"]))
    decision = await RecoveryService(
        core_settings, repository
    ).classify_and_recover(await repository.get_run(str(run["run_id"])))
    assert decision["tier"] == 1
    assert decision["status"] == "queued"
    assert (await repository.get_run(str(run["run_id"])))["status"] == "queued"


async def test_recovery_keeps_all_non_supervisor_runs_on_manual_card(
    core_settings,
):
    repository = CoreRepository(core_settings)
    prepare_agent(core_settings, "recovery-demo")
    clean = await repository.ingest_message(
        project_id="recovery-demo", agent_key="project:recovery-demo:manager",
        channel="discord", chat_id="pm", user_id="u1",
        external_event_id="recovery-clean", external_message_id="recovery-clean",
        content={"text": "clean chat"},
    )
    clean_run = clean["run"]
    dirty = await repository.ingest_message(
        project_id="recovery-demo", agent_key="project:recovery-demo:manager",
        channel="discord", chat_id="pm-2", user_id="u1",
        external_event_id="recovery-dirty", external_message_id="recovery-dirty",
        content={"text": "dirty chat"},
    )
    dirty_run = dirty["run"]
    python_run = await repository.create_run(
        project_id="recovery-demo", agent_key="project:recovery-demo:python",
        run_type="python", request={"source_uri": "vault://recovery-demo/x.py"},
        idempotency_key="recovery-python-1",
    )
    for target in (clean_run, dirty_run, python_run):
        await _force_reconciling(repository, str(target["run_id"]))
    # The dirty run declares recorded file effects through the executor.
    async with repository.database.connection() as connection:
        await connection.execute(
            "UPDATE assistant.runs SET executor_json=executor_json || "
            "'{\"file_effects\":{\"changed_entry_count\":3}}'::jsonb "
            "WHERE run_id=%s",
            (str(dirty_run["run_id"]),),
        )
        await connection.commit()

    service = RecoveryService(core_settings, repository)
    clean_decision = await service.classify_and_recover(
        await repository.get_run(str(clean_run["run_id"]))
    )
    dirty_decision = await service.classify_and_recover(
        await repository.get_run(str(dirty_run["run_id"]))
    )
    python_decision = await service.classify_and_recover(
        await repository.get_run(str(python_run["run_id"]))
    )
    assert clean_decision == {
        "run_id": str(clean_run["run_id"]), "tier": 3,
        "status": "left_for_card",
    }
    assert dirty_decision == {
        "run_id": str(dirty_run["run_id"]), "tier": 3, "status": "left_for_card",
    }
    assert python_decision["tier"] == 3
    statuses = {
        str(item["run_id"]): str(item["status"])
        for item in await repository.list_runs("recovery-demo", limit=10)
    }
    assert statuses[str(clean_run["run_id"])] == "reconciling"
    assert statuses[str(dirty_run["run_id"])] == "reconciling"
    assert statuses[str(python_run["run_id"])] == "reconciling"
