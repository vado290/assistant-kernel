from __future__ import annotations
from assistant_kernel.services.kernel_management import management_route
from assistant_kernel.services.agent_files import unbound_route

import asyncio
import contextlib
import io
import json
import logging
import math
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal

import aiohttp
import discord
from discord import app_commands
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.agent_files import RouteResolver
from assistant_kernel.services.channel_health import (
    ChannelHealthReporter, now_iso,
)
from assistant_kernel.services.project_credentials import ProjectCredentialsStore
from assistant_kernel.services.discord_interfaces import DiscordInterfaceRegistry
from assistant_kernel.services.reconciliation import ReconciliationService
from assistant_kernel.services.questionnaire import is_questionnaire, render_questionnaire
from assistant_kernel.services.feishu import (
    _handle_slash_command,
    _resolve_project_pm,
    parse_slash_command,
    render_text,
)


KERNEL_UI_PREFIX = "Kernel: "
DISCORD_INTERACTION_FRESH_SECONDS = 30.0
DISCORD_INGRESS_RETRY_DELAYS = (0.0, 0.25, 1.0)


@dataclass(frozen=True)
class InteractionAckResult:
    status: Literal["ok", "expired", "network_uncertain", "failed"]
    interaction_age_ms: int
    defer_duration_ms: int
    fallback_allowed: bool


def _interaction_age_ms(interaction: discord.Interaction) -> int:
    created_at = getattr(interaction, "created_at", None)
    if not isinstance(created_at, datetime):
        return 0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(int((datetime.now(timezone.utc) - created_at).total_seconds() * 1000), 0)


async def ingest_discord_event_with_retry(
    settings: KernelSettings, payload: dict[str, Any], *,
    health: ChannelHealthReporter | None = None,
    before_resume: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Retry only transient ingress infrastructure failures with one event id."""
    transient = (
        OperationalError, InterfaceError, PoolTimeout,
        OSError, asyncio.TimeoutError,
    )
    for attempt, delay in enumerate(DISCORD_INGRESS_RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            result = await ingest_discord_event(
                settings, payload, before_resume=before_resume,
            )
            if health is not None:
                health.update(
                    last_ingress_commit_at=now_iso(),
                    last_ingress_error_type=None,
                    last_ingress_attempts=attempt,
                )
            return result
        except (PermissionError, ValueError):
            raise
        except transient as exc:
            if health is not None:
                health.update(
                    last_ingress_error_type=type(exc).__name__,
                    last_ingress_error_at=now_iso(),
                    last_ingress_attempts=attempt,
                )
            if attempt >= len(DISCORD_INGRESS_RETRY_DELAYS):
                raise
    raise RuntimeError("Discord ingress retry loop ended unexpectedly")


def kernel_ui_text(value: str) -> str:
    text = str(value or "").strip()
    return text if text.startswith(KERNEL_UI_PREFIX) else KERNEL_UI_PREFIX + text


def render_discord_text(message: dict[str, Any]) -> str:
    if is_questionnaire(dict(message.get("content_json") or {})):
        return render_questionnaire(message["content_json"])
    text = render_text(message)
    content = dict(message.get("content_json") or {})
    is_kernel = bool(
        content.get("origin") == "kernel"
        or str(message.get("message_type") or "") in {
            "reconciliation", "kernel_notice",
        }
        or (
            message.get("run_id") is None
            and str(message.get("message_type") or "") == "reply"
            and content.get("command")
        )
    )
    return kernel_ui_text(text) if is_kernel else text


def _id_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        items: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = (value,)
    return {str(item).strip() for item in items if str(item).strip()}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_discord_config(settings: KernelSettings) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if settings.service_credentials_path.is_file():
        payload = json.loads(
            settings.service_credentials_path.read_text(encoding="utf-8-sig")
        )
        data = dict((payload.get("services") or {}).get("discord") or {})

    environment = {
        "bot_token": os.getenv("DISCORD_BOT_TOKEN"),
        "application_id": os.getenv("DISCORD_APPLICATION_ID"),
        "home_channel_id": os.getenv("DISCORD_HOME_CHANNEL_ID"),
        "guild_ids": os.getenv("DISCORD_GUILD_IDS") or os.getenv("DISCORD_GUILD_ID"),
        "allowed_channel_ids": os.getenv("DISCORD_ALLOWED_CHANNEL_IDS"),
        "allowed_user_ids": (
            os.getenv("DISCORD_ALLOWED_USER_IDS")
            or os.getenv("DISCORD_ALLOWED_USER_ID")
        ),
        "owner_user_ids": os.getenv("DISCORD_OWNER_USER_IDS"),
        "archived_channel_ids": os.getenv("DISCORD_ARCHIVED_CHANNEL_IDS"),
        "project_category_id": os.getenv("DISCORD_PROJECT_CATEGORY_ID"),
        "api_base_url": os.getenv("DISCORD_API_BASE_URL"),
    }
    for key, value in environment.items():
        if value is not None and value != "":
            data[key] = value
    data["enabled"] = _env_bool("DISCORD_ENABLED", bool(data.get("enabled")))
    data["allow_dm"] = _env_bool("DISCORD_ALLOW_DM", bool(data.get("allow_dm")))
    data["message_content"] = _env_bool(
        "DISCORD_MESSAGE_CONTENT", bool(data.get("message_content", True))
    )
    data["auto_provision_projects"] = _env_bool(
        "DISCORD_AUTO_PROVISION_PROJECTS",
        bool(data.get("auto_provision_projects", False)),
    )
    data["home_channel_id"] = str(data.get("home_channel_id") or "") or None
    data["project_category_id"] = (
        str(data.get("project_category_id") or "") or None
    )
    for key in (
        "guild_ids", "allowed_channel_ids", "allowed_user_ids", "owner_user_ids",
        "archived_channel_ids",
    ):
        data[key] = _id_set(data.get(key))
    return data


def core_channel_ids(settings: KernelSettings) -> frozenset[str]:
    """Channels handed to the rebuilt core (config/core-discord.yaml); the legacy gateway ignores them."""
    path = settings.data_root / "config" / "core-discord.yaml"
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return frozenset()
    return frozenset(str(project.get(key)) for project in (data.get("projects") or {}).values()
                     for key in ("pm_channel_id", "supervisor_channel_id") if project.get(key))


def _member_snapshot(user: Any) -> dict[str, Any]:
    roles = getattr(user, "roles", None) or []
    return {
        "user_id": str(getattr(user, "id", "") or ""),
        "display_name": str(
            getattr(user, "display_name", "")
            or getattr(user, "global_name", "")
            or getattr(user, "name", "")
            or ""
        ),
        "role_ids": [
            str(getattr(role, "id", "")) for role in roles
            if str(getattr(role, "id", ""))
        ],
    }


def validate_discord_config(config: dict[str, Any]) -> None:
    if not config.get("enabled"):
        return
    if not str(config.get("bot_token") or ""):
        raise ValueError("Discord bot_token is required when the channel is enabled")
    if not config.get("guild_ids"):
        raise ValueError("Discord guild_ids must be non-empty")
    if config.get("allow_dm") and not config.get("owner_user_ids"):
        raise ValueError("Discord owner_user_ids must be non-empty when DMs are enabled")


def discord_origin_rejection(
    config: dict[str, Any], *, guild_id: str | None, channel_id: str,
    parent_channel_id: str | None, user_id: str,
) -> str | None:
    if guild_id is None:
        if not config.get("allow_dm"):
            return "dm_disabled"
        return (
            None
            if user_id in set(config.get("owner_user_ids") or set())
            else "dm_user_not_owner"
        )
    if guild_id not in set(config.get("guild_ids") or set()):
        return "guild_not_allowed"
    return None


def _external_event_id(config: dict[str, Any], event_id: str) -> str:
    application_id = str(config.get("application_id") or "default")
    return f"{application_id}:{event_id}"


async def _start_supervisor_consultation(
    *, settings: KernelSettings, repository: CoreRepository,
    project_id: str, channel: str, chat_id: str,
    user_id: str | None, external_event_id: str, external_message_id: str,
    text: str, trigger: str,
    reply_to_message_id: str | None = None, task_id: str | None = None,
    consultation_id: str | None = None,
) -> dict[str, Any]:
    """v6.1: queue a Supervisor consultation Run instead of an inline call."""
    agent_key = f"project:{project_id}:supervisor"
    if task_id is None and consultation_id is None:
        current = await repository.current_project_task(project_id)
        task_id = str(current["run_id"]) if current is not None else ""
    recorded = await repository.ingest_message(
        project_id=project_id, agent_key=agent_key,
        channel=channel, chat_id=chat_id, user_id=user_id,
        external_event_id=external_event_id,
        external_message_id=external_message_id,
        content={"text": text, "discord": {"channel_id": chat_id}},
        message_type="text",
        reply_to_message_id=reply_to_message_id,
        auto_reply_to_unambiguous=False,
    )
    if bool(recorded["duplicate"]):
        # The original delivery of this Discord event already advanced the
        # dialogue head and queued its consultation.  Replaying it must not
        # advance the epoch a second time or create a differently-shaped Run.
        return {
            "message": recorded["message"], "run": None, "outbound": None,
            "duplicate": True,
        }
    consultation_id = None if task_id else consultation_id or f"consult-{recorded['message']['message_id']}"
    source_context_epoch = await repository.advance_supervisor_context_epoch(
        task_id,
        reason="new_supervisor_message",
        source_id=str(recorded["message"]["message_id"]),
    ) if task_id else 0
    run = await repository.create_run(
        project_id=project_id, agent_key=agent_key, run_type="chat",
        trigger_message_id=str(recorded["message"]["message_id"]),
        request={
            "interaction_kind": "supervisor_consultation",
            "schema_version": "supervisor-consultation.v3",
            "task_id": task_id or None, "consultation_id": consultation_id,
            "read_only_consultation": not bool(task_id), "trigger": trigger, "turn_number": 0,
            "source_context_epoch": int(source_context_epoch),
            "source_turn_number": 0,
            "turn_handoff_ref": None,
            "turn_handoff_sha256": None,
            "source_message_id": str(recorded["message"]["message_id"]),
            "interaction_head": str(recorded["message"]["message_id"]),
            "channel": channel, "chat_id": chat_id, "user_id": user_id,
            "reply_to_message_id": reply_to_message_id,
            "source_actor_type": "user",
            "source_agent_key": None,
            "source_run_id": None,
            "source_interface_role": "supervisor",
            "source_channel": channel,
            "source_chat_id": chat_id,
        },
        idempotency_key=f"supervisor-consult:discord:{external_event_id}",
    )
    ack = await repository.create_outbound(
        project_id=project_id, session_id=None, run_id=str(run["run_id"]),
        channel=channel, chat_id=chat_id, user_id=user_id,
        message_type="reply",
        content={
            "text": "已收到，Supervisor正在评估当前项目；本次为只读咨询。" if not task_id else
                    "已收到，Supervisor正在基于当前Task状态评估，稍后更新状态卡。",
            "origin": "kernel", "context_visibility": "ui_only",
        },
        reply_to_message_id=str(recorded["message"]["message_id"]),
        idempotency_key=f"supervisor-ack:discord:{external_event_id}",
    )
    return {
        "message": recorded["message"], "run": run, "outbound": ack,
        "duplicate": bool(recorded["duplicate"]),
    }


async def ingest_discord_event(
    settings: KernelSettings, payload: dict[str, Any], *,
    allow_explicit_route: bool = False,
    before_resume: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    config = load_discord_config(settings)
    event_id = str(payload.get("event_id") or payload.get("interaction_id") or "")
    if not event_id:
        raise ValueError("Discord event_id is required")
    if payload.get("author_bot"):
        return {"ignored": True, "reason": "bot_sender"}

    guild_id = str(payload.get("guild_id") or "") or None
    channel_id = str(payload.get("channel_id") or "")
    parent_channel_id = str(payload.get("parent_channel_id") or "") or None
    is_thread = bool(payload.get("is_thread") and parent_channel_id)
    binding_chat_id = parent_channel_id if is_thread else channel_id
    user_id = str(payload.get("user_id") or "")
    if not channel_id or not user_id:
        raise ValueError("Discord channel_id and user_id are required")
    rejection = discord_origin_rejection(
        config,
        guild_id=guild_id,
        channel_id=channel_id,
        parent_channel_id=parent_channel_id,
        user_id=user_id,
    )
    if rejection:
        raise PermissionError(f"Discord origin rejected: {rejection}")

    text = str(payload.get("content") or "").strip()
    external_message_id = str(payload.get("message_id") or "") or None
    reply_external_id = str(payload.get("reply_message_id") or "") or None
    chat_type = "private" if guild_id is None else "group"
    slash_candidate = parse_slash_command(text)
    repository = CoreRepository(settings)
    reply_to_message_id = None
    message_type = "text"
    original = None
    group_binding = None
    project_interface = (
        DiscordInterfaceRegistry(settings).resolve(binding_chat_id)
        if chat_type == "group" else None
    )
    archived = binding_chat_id in set(config.get("archived_channel_ids") or set())

    if reply_external_id and slash_candidate is None:
        async with repository.database.connection() as connection:
            original = await (
                await connection.execute(
                    """
                    SELECT m.message_id,m.message_type,m.project_id,m.session_id,m.run_id,
                           m.content_json,m.chat_id,m.user_id,r.agent_key
                    FROM assistant.messages m
                    LEFT JOIN assistant.runs r ON r.run_id=m.run_id
                    WHERE m.channel='discord' AND m.external_message_id=%s
                      AND m.direction='outbound' AND m.chat_id=%s
                      AND (m.user_id IS NULL OR m.user_id=%s)
                    ORDER BY sent_at DESC NULLS LAST LIMIT 1
                    """,
                    (reply_external_id, channel_id, user_id),
                )
            ).fetchone()
        if original is None:
            raise ValueError("the replied Discord message is not a known outbound message")
        from assistant_kernel.services.question_groups import QuestionGroupService, projection_ref
        if projection_ref(original.get('content_json') or {}):
            result=await QuestionGroupService(settings,repository).record_text(
                projection={**original,'channel':'discord'},channel='discord',chat_id=channel_id,
                user_id=user_id,external_event_id=_external_event_id(config,event_id),text=text)
            await repository.create_outbound(project_id=str(original['project_id']),session_id=original['session_id'],
                run_id=str(original['run_id']),channel='discord',chat_id=channel_id,user_id=user_id,
                message_type='kernel_notice',content={'text':'输入已保存，交由 Supervisor 澄清问题对应关系。' if result.get('unmapped') else
                    '正式答案已保存；更正与其他要求保留原文，下一轮 Prompt 仍需单独确认。',
                    'context_visibility':'ui_only','origin':'kernel'},reply_to_message_id=None,
                idempotency_key='question-text-receipt:'+event_id)
            return {'message':result['message'],'run':None,'duplicate':result['duplicate'],
                'partial_answer':True,'resumed':False}
        if is_questionnaire(dict(original.get("content_json") or {})):
            result = await repository.record_questionnaire_action(
                message_id=str(original["message_id"]), project_id=str(original["project_id"]),
                channel="discord", chat_id=channel_id, user_id=user_id,
                external_event_id=_external_event_id(config, event_id),
                revision=-1, page=-1, action="reply", value=text,
            )
            await repository.create_outbound(
                project_id=str(original["project_id"]), session_id=original["session_id"],
                run_id=str(original["run_id"]), channel="discord", chat_id=channel_id,
                user_id=user_id, message_type="kernel_notice",
                content={
                    "text": "答案草稿已保存，请刷新原卡片后继续或提交全部答案。",
                    "origin": "kernel", "context_visibility": "ui_only",
                },
                reply_to_message_id=None,
                idempotency_key=f"questionnaire-draft-receipt:{event_id}",
            )
            return {"message": result["message"], "run": None, "duplicate": result["duplicate"],
                    "partial_answer": True, "resumed": False}
        if original["message_type"] in {"question", "approval"}:
            reply_to_message_id = str(original["message_id"])
            message_type = "answer"
        elif (
            str(original["message_type"]) == "kernel_notice"
            and (original["content_json"] or {}).get("interaction_kind")
            == "supervisor_status"
        ):
            # A reply to a Supervisor status card continues the consultation
            # instead of routing to the PM chat.
            reply_to_message_id = str(original["message_id"])
            message_type = "supervisor_reply"
        elif (original.get("content_json") or {}).get("consultation_id") and str(
            original.get("agent_key") or ""
        ).endswith(":supervisor"):
            reply_to_message_id = str(original["message_id"])
            message_type = "supervisor_consultation_reply"

    if original is not None:
        project_id = str(original["project_id"])
        agent_key = str(original["agent_key"] or (
            "system-butler" if project_id == "system"
            else f"project:{project_id}:manager"
        ))
    else:
        explicit = (payload.get("kernel") or {}) if allow_explicit_route else {}
        if chat_type == "group":
            if project_interface is not None:
                interface_project = str(project_interface["project_id"])
                interface_role = str(project_interface["role"])
                group_binding = {
                    "project_id": interface_project,
                    "agent_key": (
                        f"project:{interface_project}:{interface_role}"
                        if interface_role in {"supervisor"}
                        else f"project:{interface_project}:manager"
                    ),
                    "interface_role": interface_role,
                }
                if str(project_interface.get("guild_id") or guild_id) != guild_id:
                    raise PermissionError("Discord Project interface belongs to another Guild")
            else:
                group_binding = (
                    await repository.resolve_chat_project_binding("discord", binding_chat_id)
                )
            if group_binding is None:
                project_id, agent_key = unbound_route(settings)
            elif project_interface is not None:
                project_id = str(group_binding["project_id"])
                agent_key = str(group_binding["agent_key"])
            else:
                project_id = str(group_binding["project_id"])
                try:
                    _, target = _resolve_project_pm(settings, repository, project_id)
                except (KeyError, PermissionError, ValueError):
                    project_id, agent_key = unbound_route(settings)
                    group_binding = None
                else:
                    agent_key = target.agent_key
        elif explicit.get("project_id"):
            project_id, agent_key = await RouteResolver(settings, repository).resolve(
                "discord", channel_id, user_id,
                explicit.get("project_id"), explicit.get("agent_key"),
            )
        else:
            project_id, agent_key = await RouteResolver(settings, repository).resolve(
                "discord", channel_id, user_id,
            )

    if original is None:
        project_id, agent_key = management_route(settings.data_root, (project_id, agent_key))

    from assistant_kernel.services.kernel_management import retired_identity
    if retired_identity(project_id, agent_key):
        project_id, agent_key = "kernel", "project:kernel:manager"
        original = None
        message_type = "text"
        if slash_candidate is None or slash_candidate[0] not in {"/bind_project", "/unbind_project", "/binding", "/help"}:
            slash_candidate = ("/butler", None)

    external_event_id = _external_event_id(config, event_id)
    if archived and (
        slash_candidate is None
        or slash_candidate[0] not in {"/help", "/binding", "/whoami"}
    ):
        recorded = await repository.record_channel_command(
            project_id="kernel", agent_key="project:kernel:manager", channel="discord",
            chat_id=channel_id, user_id=user_id, external_event_id=external_event_id,
            external_message_id=external_message_id,
            content={"text": text, "reason": "archived_channel", "raw": payload},
        )
        outbound = await repository.create_outbound(
            project_id="kernel", session_id=None, run_id=None, channel="discord",
            chat_id=channel_id, user_id=user_id, message_type="reply",
            content={
                "text": "该频道已归档，只能查看历史；请在 chief-of-staff 或新的 Project 频道继续。",
                "origin": "kernel", "context_visibility": "ui_only",
            },
            reply_to_message_id=str(recorded["message"]["message_id"]),
            idempotency_key=f"archived-channel-reply:discord:{external_event_id}",
        )
        return {
            "message": recorded["message"], "run": None, "outbound": outbound,
            "duplicate": bool(recorded["duplicate"]), "archived_channel": True,
        }
    if message_type == "supervisor_consultation_reply" and original is not None and slash_candidate is None:
        return await _start_supervisor_consultation(
            settings=settings, repository=repository, project_id=project_id, channel="discord",
            chat_id=channel_id, user_id=user_id, external_event_id=external_event_id,
            external_message_id=external_message_id, text=text, trigger="user_consultation",
            reply_to_message_id=reply_to_message_id,
            consultation_id=str((original["content_json"] or {}).get("consultation_id")),
        )
    if (
        message_type == "supervisor_reply"
        and original is not None
        and slash_candidate is None
    ):
        card_task_id = str((original["content_json"] or {}).get("task_id") or "")
        return await _start_supervisor_consultation(
            settings=settings, repository=repository,
            project_id=project_id, channel="discord", chat_id=channel_id,
            user_id=user_id, external_event_id=external_event_id,
            external_message_id=external_message_id, text=text,
            trigger="prompt_revision",
            reply_to_message_id=reply_to_message_id,
            task_id=card_task_id or None,
        )
    if (
        project_interface is not None
        and project_interface.get("role") == "supervisor"
        and slash_candidate is None
        and message_type == "text"
    ):
        return await _start_supervisor_consultation(
            settings=settings, repository=repository,
            project_id=project_id, channel="discord", chat_id=channel_id,
            user_id=user_id, external_event_id=external_event_id,
            external_message_id=external_message_id, text=text,
            trigger="user_consultation",
        )
    if slash_candidate is not None and original is None:
        command, argument = slash_candidate
        return await _handle_slash_command(
            settings=settings,
            repository=repository,
            project_id=project_id,
            agent_key=agent_key,
            channel="discord",
            chat_id=channel_id,
            user_id=user_id,
            external_event_id=external_event_id,
            external_message_id=external_message_id,
            text=text,
            raw=payload,
            command=command,
            argument=argument,
            chat_type=chat_type,
            platform_label="Discord",
            chat_label="频道",
            platform_chat_label="Discord频道",
            allow_chat_binding=user_id in set(config.get("owner_user_ids") or set()),
            binding_chat_id=binding_chat_id,
            binding_inherited=is_thread,
            interface_binding=group_binding if project_interface is not None else None,
            binding_block_reason=(
                "该频道是已登记的 Project Supervisor 入口，不能用普通绑定命令改变归属。"
                if project_interface is not None else
                None
            ),
        )

    is_home_conversation = binding_chat_id == config.get("home_channel_id")
    if chat_type == "group" and group_binding is None and original is None and not is_home_conversation:
        recorded = await repository.record_channel_command(
            project_id="kernel", agent_key="project:kernel:manager", channel="discord",
            chat_id=channel_id, user_id=user_id, external_event_id=external_event_id,
            external_message_id=external_message_id,
            content={"text": text, "reason": "unbound_channel", "raw": payload},
        )
        target = "父频道" if is_thread else "当前频道"
        outbound = await repository.create_outbound(
            project_id="kernel", session_id=None, run_id=None, channel="discord",
            chat_id=channel_id, user_id=user_id, message_type="reply",
            content={
                "text": f"{target}尚未绑定Project。请在顶层频道发送 /bind_project <project-id>。",
                "origin": "kernel", "context_visibility": "ui_only",
            },
            reply_to_message_id=str(recorded["message"]["message_id"]),
            idempotency_key=f"unbound-channel-reply:discord:{external_event_id}",
        )
        return {
            "message": recorded["message"], "run": None, "outbound": outbound,
            "duplicate": bool(recorded["duplicate"]), "unbound_group": True,
        }

    attachments = [
        {
            "id": str(item.get("id") or ""),
            "filename": str(item.get("filename") or ""),
            "size": int(item.get("size") or 0),
            "content_type": str(item.get("content_type") or "") or None,
        }
        for item in (payload.get("attachments") or [])
        if isinstance(item, dict)
    ]
    content = {
        "text": text,
        "discord": {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "parent_channel_id": parent_channel_id,
            "channel_name_snapshot": str(payload.get("channel_name") or "") or None,
            "parent_channel_name_snapshot": str(payload.get("parent_channel_name") or "") or None,
            "thread": is_thread,
            "attachments": attachments,
            "member": dict(payload.get("member") or {}),
        },
    }
    result = await repository.ingest_message(
        project_id=project_id,
        agent_key=agent_key,
        channel="discord",
        chat_id=channel_id,
        user_id=user_id,
        external_event_id=external_event_id,
        external_message_id=external_message_id,
        content=content,
        message_type=message_type,
        reply_to_message_id=reply_to_message_id,
        auto_reply_to_unambiguous=bool(
            message_type == "text" and reply_to_message_id is None
        ),
        before_resume=before_resume,
    )
    run = result.get("run")
    is_manager_chat = bool(
        run
        and str(run.get("run_type") or "") == "chat"
        and (
            str(run.get("agent_key") or "").endswith(":manager")
        )
    )
    superseded = [str(item) for item in result.get("superseded_run_ids") or []]
    if superseded and run is not None:
        result["dedupe_notice"] = await repository.create_outbound(
            project_id=project_id, session_id=None,
            run_id=str(run["run_id"]), channel="discord", chat_id=channel_id,
            user_id=user_id, message_type="kernel_notice",
            content={
                "text": (
                    f"检测到完全相同且尚未执行的消息；已保留最新一条，"
                    f"取消 {len(superseded)} 条旧队列 Run。"
                ),
                "origin": "kernel", "context_visibility": "ui_only",
                "superseded_run_ids": superseded,
            },
            reply_to_message_id=str(result["message"]["message_id"]),
            idempotency_key=f"duplicate-superseded:discord:{run['run_id']}",
        )
    if is_manager_chat:
        blocker = await repository.active_execution_blocker(project_id)
        if blocker is not None:
            status = str(blocker.get("status") or "unknown")
            stage = str(blocker.get("stage") or blocker.get("run_type") or "execution")
            notice = await repository.create_outbound(
                project_id=project_id, session_id=None,
                run_id=str(run["run_id"]), channel="discord", chat_id=channel_id,
                user_id=user_id, message_type="kernel_notice",
                content={
                    "text": (
                        f"工程执行平面当前为 {status}（{stage}，"
                        f"Run {str(blocker['run_id'])[:8]}）。"
                        "同一工程主线会等待该任务；独立脚本和运行任务仍可并行。"
                    ),
                    "origin": "kernel", "context_visibility": "ui_only",
                    "blocking_run_id": str(blocker["run_id"]),
                    "blocking_status": status, "blocking_stage": stage,
                },
                reply_to_message_id=str(result["message"]["message_id"]),
                idempotency_key=(
                    f"execution-blocker-notice:discord:{project_id}:"
                    f"{channel_id}:{blocker['run_id']}"
                ),
            )
            result["kernel_notice"] = notice
    return result


def split_discord_text(text: str, limit: int = 1900) -> list[str]:
    value = (text or "(empty response)").strip() or "(empty response)"
    chunks: list[str] = []
    while len(value) > limit:
        cut = value.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = value.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(value[:cut].rstrip())
        value = value[cut:].lstrip()
    chunks.append(value)
    return chunks


class KernelQuestionnaireModal(discord.ui.Modal):
    def __init__(
        self, client: "KernelDiscordClient", message: dict[str, Any], *,
        revision: int, page: int, additional: bool,
    ) -> None:
        super().__init__(title="补充说明" if additional else "自由填写答案", timeout=900)
        self.kernel_client = client
        self.kernel_message = message
        self.revision, self.page = revision, page
        self.action = "additional" if additional else "text"
        body = message["content_json"]
        state = body["questionnaire"]
        default = state.get("additional_text", "") if additional else (
            state.get("answers", {}).get(body["questions"][page]["question_id"], "")
        )
        self.answer = discord.ui.TextInput(
            custom_id="answer", style=discord.TextStyle.paragraph,
            required=not additional, max_length=4000, default=str(default),
        )
        self.add_item(discord.ui.Label(
            text="补充要求或其他说明（可选）" if additional else "自己的答案",
            component=self.answer,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.kernel_client.handle_questionnaire_component(
            interaction, str(self.kernel_message["message_id"]),
            f"q:{self.revision}:{self.page}:{self.action}",
            str(self.answer.value), self.kernel_message,
        )


def build_questionnaire_view(
    message: dict[str, Any], client: discord.Client,
) -> discord.ui.View | None:
    body = message["content_json"]
    state = body["questionnaire"]
    if state.get("status") != "draft":
        return None
    page, revision = int(state["cursor"]), int(state["revision"])
    questions = body["questions"]
    message_id = str(message["message_id"])
    view = discord.ui.View(timeout=None)

    def button(label: str, verb: str, *, row: int, disabled: bool = False, selected: bool = False):
        action = f"q:{revision}:{page}:{verb}"
        item = discord.ui.Button(
            label=label[:80], row=row, disabled=disabled,
            style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
            custom_id=f"ak:v1:{message_id}:{action}",
        )

        async def callback(interaction: discord.Interaction) -> None:
            await client.handle_kernel_component(  # type: ignore[attr-defined]
                interaction, message_id, action, None, message,
            )

        item.callback = callback
        view.add_item(item)

    if page < len(questions):
        question = questions[page]
        for index, option in enumerate(question.get("options") or []):
            recommended = "（推荐）" if option["id"] == question.get("recommended_option_id") else ""
            label = str(option["label"])
            button(label[:80 - len(recommended)] + recommended, f"choice:{index}", row=0,
                   selected=state.get("choices", {}).get(question["question_id"]) == option["id"])
        button("其他 / 自由填写", "write", row=0)
        button("上一问", "previous", row=1, disabled=page == 0)
        button("下一问" if page + 1 < len(questions) else "查看并提交", "next", row=1)
    else:
        button("返回修改", "previous", row=0)
        button("自由补充说明", "supplement", row=0)
        button("提交全部答案", "submit", row=0)
    button("刷新", "refresh", row=1)
    return view


class KernelCredentialModal(discord.ui.Modal):
    def __init__(
        self, client: "KernelDiscordClient", message: dict[str, Any],
        fields: list[dict[str, Any]],
    ) -> None:
        super().__init__(title="Project credential", timeout=900)
        self.kernel_client = client
        self.kernel_message = message
        for field in fields[:5]:
            self.add_item(discord.ui.TextInput(
                label=str(field.get("label") or field.get("name") or "Value")[:45],
                custom_id=str(field.get("name") or "value")[:100],
                required=bool(field.get("required", True)),
                style=discord.TextStyle.short,
            ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = {
            str(item.custom_id): str(item.value)
            for item in self.children if isinstance(item, discord.ui.TextInput)
        }
        await self.kernel_client.handle_credential_submit(
            interaction, self.kernel_message, values,
        )


def build_discord_view(
    message: dict[str, Any], client: discord.Client,
) -> discord.ui.View | None:
    if not hasattr(client, "handle_kernel_component"):
        return None
    content = dict(message.get("content_json") or {})
    if is_questionnaire(content):
        return build_questionnaire_view(message, client)
    message_id = str(message.get("message_id") or "")
    message_type = str(message.get("message_type") or "")
    view = discord.ui.View(timeout=None)

    def add_button(
        label: str, style: discord.ButtonStyle, action: str, *, disabled: bool = False,
    ) -> None:
        button = discord.ui.Button(
            label=label[:80], style=style,
            custom_id=f"ak:v1:{message_id}:{action}"[:100],
            disabled=disabled,
        )

        async def callback(interaction: discord.Interaction) -> None:
            await client.handle_kernel_component(  # type: ignore[attr-defined]
                interaction, message_id, action, None, message,
            )

        button.callback = callback
        view.add_item(button)

    if message_type == "approval":
        add_button("Approve", discord.ButtonStyle.success, "approve")
        add_button("Request changes", discord.ButtonStyle.secondary, "revise")
        add_button("Reject", discord.ButtonStyle.danger, "reject")
        return view

    if content.get('interaction_kind')=='prompt_confirmation':
        opened=content.get('action_status')=='open'
        digest=str((content.get('package_ref') or {}).get('sha256') or '')[:12]
        for item in content.get('actions') or []:
            add_button(str(item['label']),discord.ButtonStyle.success if item['id']=='confirm' else discord.ButtonStyle.secondary,
                'prompt:'+str(item['id'])+':'+digest,disabled=not opened)
        return view if view.children else None

    if content.get("interaction_kind") == "reconcile_run":
        styles = {
            "success": discord.ButtonStyle.success,
            "secondary": discord.ButtonStyle.secondary,
            "danger": discord.ButtonStyle.danger,
            "primary": discord.ButtonStyle.primary,
        }
        for item in content.get("actions") or []:
            if not isinstance(item, dict) or not item.get("id") or not item.get("label"):
                continue
            add_button(
                str(item["label"]),
                styles.get(str(item.get("style") or "primary"), discord.ButtonStyle.primary),
                "reconcile:" + str(item["id"]),
            )
        return view if view.children else None

    if content.get("interaction_kind") == "supervisor_status":
        styles = {
            "success": discord.ButtonStyle.success,
            "secondary": discord.ButtonStyle.secondary,
            "danger": discord.ButtonStyle.danger,
            "primary": discord.ButtonStyle.primary,
        }
        card_open = str(content.get("action_status") or "open") == "open"
        for item in content.get("actions") or []:
            if not isinstance(item, dict) or not item.get("id") or not item.get("label"):
                continue
            add_button(
                str(item["label"]),
                styles.get(str(item.get("style") or "primary"), discord.ButtonStyle.primary),
                "supervisor:" + str(item["id"]),
                disabled=bool(item.get("disabled")) or not card_open,
            )
        return view if view.children else None

    if content.get("interaction_kind") == "supervision_recovery":
        for item in content.get("actions") or []:
            if isinstance(item, dict) and item.get("id") in {"retry", "wait", "cancel"}:
                add_button(str(item.get("label") or item["id"]), discord.ButtonStyle.secondary,
                           "supervision-recovery:" + str(item["id"]),
                           disabled=bool(item.get("disabled")) or content.get("action_status", "waiting") != "waiting")
        return view if view.children else None

    if message_type == "question":
        input_schema = dict(content.get("input_schema") or {})
        fields = [
            dict(item) for item in input_schema.get("fields") or []
            if isinstance(item, dict)
        ]
        if input_schema.get("kind") == "project_credential" and 1 <= len(fields) <= 5:
            add_button("Enter credential", discord.ButtonStyle.primary, "credential")
            return view
        options = [
            dict(item) for item in content.get("options") or []
            if isinstance(item, dict)
        ]
        if 2 <= len(options) <= 3 and all(item.get("id") and item.get("label") for item in options):
            for option in options:
                recommended = "（推荐）" if option["id"] == content.get("recommended_option_id") else ""
                add_button(
                    str(option["label"])[:80 - len(recommended)] + recommended, discord.ButtonStyle.primary,
                    "choice:" + str(option["id"]),
                )
            return view

    resume_options = [
        dict(item) for item in content.get("resume_options") or []
        if isinstance(item, dict)
    ]
    if content.get("interaction_kind") == "resume_select" and resume_options:
        select = discord.ui.Select(
            placeholder="Select a Run to resume",
            min_values=1, max_values=1,
            custom_id=f"ak:v1:{message_id}:resume"[:100],
            options=[
                discord.SelectOption(
                    label=str(item.get("label") or item.get("id") or "Run")[:100],
                    value=str(item.get("id") or "")[:100],
                    description=str(item.get("description") or "")[:100] or None,
                )
                for item in resume_options[:25]
                if item.get("id")
            ],
        )

        async def select_callback(interaction: discord.Interaction) -> None:
            await client.handle_kernel_component(  # type: ignore[attr-defined]
                interaction, message_id, "resume", select.values[0], message,
            )

        select.callback = select_callback
        view.add_item(select)
        return view
    return None


class DiscordSender:
    def __init__(
        self, settings: KernelSettings, repository: CoreRepository,
        client: discord.Client,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.reconciliations = ReconciliationService(settings, repository)
        self.client = client

    async def send(self, message: dict[str, Any], worker_instance_id: str) -> None:
        if not await self.repository.confirm_delivery_current(
            str(message["message_id"]), worker_instance_id
        ):
            return
        channel_id = int(str(message["chat_id"]))
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError("Discord destination is not messageable")

        content = dict(message.get('content_json') or {})
        display_message = {**message,'message_id':content['target_message_id']} if content.get('target_message_id') else message
        text = render_discord_text(display_message)
        chunks = split_discord_text(text)
        view = build_discord_view(display_message, self.client)
        update_external_id = str(content.get("update_external_message_id") or "")
        if content.get("interaction_kind") == "supervisor_status" and not content.get(
            "ui_update_only"
        ):
            # Every actionable revision is a new physical Discord message.
            # Only explicit UI-only terminal/supersede projections may edit an
            # older card, which prevents delayed edits from replacing a newer
            # decision card.
            update_external_id = ""
        if update_external_id and len(chunks) == 1 and hasattr(channel, "get_partial_message"):
            try:
                existing = channel.get_partial_message(int(update_external_id))
                await existing.edit(content=chunks[0], view=view)
                accepted = await self.repository.acknowledge_delivery(
                    str(message["message_id"]), worker_instance_id, update_external_id,
                    {"mode": "edit", "message_ids": [update_external_id],
                     "chunk_count": 1, "msg_type": "interactive" if view else "text"},
                )
                if accepted:
                    return
            except (ValueError, discord.HTTPException, discord.NotFound):
                if content.get('target_message_id'): raise
                pass
        if content.get('target_message_id'):
            raise RuntimeError('Question projection edit could not be delivered; retain the Outbox retry')
        reply_external_id = await self.repository.delivery_reply_external_id(
            str(message["message_id"])
        )
        sent_ids: list[str] = []
        reference = None
        if reply_external_id and hasattr(channel, "get_partial_message"):
            reference = channel.get_partial_message(int(reply_external_id))
        for index, chunk in enumerate(chunks):
            kwargs: dict[str, Any] = {
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if index == 0 and reference is not None:
                kwargs.update({"reference": reference, "mention_author": False})
            if index == 0 and view is not None:
                kwargs["view"] = view
            document_markdown = content.get("document_markdown") or content.get("report_markdown")
            if index == 0 and isinstance(document_markdown, str) and document_markdown:
                kwargs["file"] = discord.File(
                    io.BytesIO(document_markdown.encode("utf-8")),
                    filename=f"artifact-{message['message_id']}.md",
                )
            try:
                sent = await channel.send(chunk, **kwargs)
            except discord.HTTPException:
                if "reference" not in kwargs:
                    raise
                kwargs.pop("reference", None)
                kwargs.pop("mention_author", None)
                if "file" in kwargs:
                    kwargs["file"] = discord.File(io.BytesIO(document_markdown.encode("utf-8")),
                        filename=f"artifact-{message['message_id']}.md")
                sent = await channel.send(chunk, **kwargs)
            sent_ids.append(str(sent.id))
        accepted = await self.repository.acknowledge_delivery(
            str(message["message_id"]), worker_instance_id, sent_ids[0],
            {
                "mode": "reply" if reference is not None else "channel",
                "message_ids": sent_ids,
                "chunk_count": len(sent_ids),
                "msg_type": "interactive" if view is not None else "text",
            },
        )
        if not accepted:
            raise RuntimeError("delivery lease was lost before acknowledgment")
        health = getattr(self.client, "health", None)
        if health is not None:
            health.update(last_delivery_at=now_iso(), last_error_type=None)


class KernelDiscordClient(discord.Client):
    def __init__(
        self, settings: KernelSettings, config: dict[str, Any],
        repository: CoreRepository, health: ChannelHealthReporter | None = None,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.dm_messages = bool(config.get("allow_dm"))
        intents.message_content = bool(config.get("message_content", True))
        super().__init__(intents=intents)
        self.settings = settings
        self.config = config
        self.repository = repository
        self.health = health or ChannelHealthReporter(settings, "discord")
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    async def _ack_interaction(
        self, interaction: discord.Interaction,
    ) -> InteractionAckResult:
        age_ms = _interaction_age_ms(interaction)
        started = time.monotonic()
        status: Literal["ok", "expired", "network_uncertain", "failed"] = "ok"
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound as exc:
                status = "expired" if int(getattr(exc, "code", 0) or 0) == 10062 else "failed"
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
                status = "network_uncertain"
            except discord.HTTPException:
                status = "failed"
        duration_ms = max(int((time.monotonic() - started) * 1000), 0)
        fallback_allowed = bool(
            status in {"expired", "network_uncertain"}
            and age_ms <= int(DISCORD_INTERACTION_FRESH_SECONDS * 1000)
        )
        self.health.update(
            last_interaction_at=now_iso(),
            last_interaction_ack_status=status,
            last_interaction_ack_ms=duration_ms,
            last_interaction_age_ms=age_ms,
        )
        if status != "ok":
            logging.warning(
                "Discord interaction acknowledgment status=%s age_ms=%s duration_ms=%s fallback=%s",
                status, age_ms, duration_ms, fallback_allowed,
            )
        return InteractionAckResult(
            status=status, interaction_age_ms=age_ms,
            defer_duration_ms=duration_ms,
            fallback_allowed=fallback_allowed,
        )

    async def _queue_component_retry_notice(
        self, interaction: discord.Interaction, message_id: str,
    ) -> None:
        try:
            message = await self._component_message(interaction, message_id)
            await self.repository.create_outbound(
                project_id=str(message["project_id"]),
                session_id=(
                    str(message["session_id"]) if message.get("session_id") else None
                ),
                run_id=str(message["run_id"]) if message.get("run_id") else None,
                channel="discord", chat_id=str(message["chat_id"]),
                user_id=str(interaction.user.id), message_type="kernel_notice",
                content={
                    "text": "本次Discord操作未执行，因为交互确认已过期或连接状态不确定；请重新点击原卡片。",
                    "origin": "kernel", "context_visibility": "ui_only",
                    "interaction_kind": "discord_interaction_not_executed",
                    "source_component_message_id": message_id,
                },
                reply_to_message_id=message_id,
                idempotency_key=f"discord-interaction-not-executed:{interaction.id}",
            )
        except Exception:
            logging.exception(
                "Discord expired component notice could not be queued message_id=%s",
                message_id,
            )

    def _register_commands(self) -> None:
        async def dispatch(
            interaction: discord.Interaction, command: str,
            argument: str | None = None,
        ) -> None:
            ack = await self._ack_interaction(interaction)
            if ack.status != "ok" and not ack.fallback_allowed:
                logging.warning(
                    "Discord command not ingested after failed acknowledgment command=%s age_ms=%s",
                    command, ack.interaction_age_ms,
                )
                return
            channel = interaction.channel
            parent_id = getattr(channel, "parent_id", None)
            payload = {
                "event_id": str(interaction.id),
                "interaction_id": str(interaction.id),
                "guild_id": str(interaction.guild_id or "") or None,
                "channel_id": str(interaction.channel_id or ""),
                "parent_channel_id": str(parent_id or "") or None,
                "channel_name": str(getattr(channel, "name", "") or "") or None,
                "parent_channel_name": str(
                    getattr(getattr(channel, "parent", None), "name", "") or ""
                ) or None,
                "user_id": str(interaction.user.id),
                "member": _member_snapshot(interaction.user),
                "author_bot": bool(interaction.user.bot),
                "content": f"/{command}" + (f" {argument}" if argument else ""),
                "is_thread": isinstance(channel, discord.Thread),
                "attachments": [],
            }
            try:
                await ingest_discord_event_with_retry(
                    self.settings, payload, health=self.health,
                )
            except PermissionError:
                logging.warning(
                    "Discord interaction rejected guild_id=%s channel_id=%s user_id=%s",
                    interaction.guild_id, interaction.channel_id, interaction.user.id,
                )
                if ack.status == "ok":
                    with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                        await interaction.followup.send(
                            kernel_ui_text("当前用户或频道未获授权。"), ephemeral=True,
                        )
            except Exception:
                logging.exception("Discord interaction ingestion failed")
                if ack.status == "ok":
                    with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                        await interaction.followup.send(
                            kernel_ui_text("指令接收失败，请稍后重试。"), ephemeral=True,
                        )
            else:
                if ack.status == "ok":
                    with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                        await interaction.followup.send(
                            kernel_ui_text("已接收，结果将在当前会话回复。"), ephemeral=True,
                        )

        async def new_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "new")

        async def goal_command(
            interaction: discord.Interaction, goal: str | None = None,
        ) -> None:
            await dispatch(interaction, "goal", goal)

        async def resume_command(
            interaction: discord.Interaction, run_id: str | None = None,
        ) -> None:
            await dispatch(interaction, "resume", run_id)

        async def pending_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "pending")

        async def status_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "status")

        async def kernel_status_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "kernel-status")

        async def audit_command(
            interaction: discord.Interaction,
            mode: Literal["quick", "full"] = "full",
        ) -> None:
            await dispatch(interaction, "audit", mode)

        async def supervisor_command(
            interaction: discord.Interaction, question: str | None = None,
        ) -> None:
            await dispatch(interaction, "supervisor", question)

        async def prompt_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "prompt")

        async def stage_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "stage")

        async def why_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "why")

        async def bind_project_command(
            interaction: discord.Interaction, project_id: str,
        ) -> None:
            await dispatch(interaction, "bind_project", project_id)

        async def unbind_project_command(
            interaction: discord.Interaction, project_id: str,
        ) -> None:
            await dispatch(interaction, "unbind_project", project_id)

        async def binding_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "binding")

        async def whoami_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "whoami")

        async def help_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "help")

        async def pm_command(interaction: discord.Interaction) -> None:
            await dispatch(interaction, "pm")

        commands = (
            (new_command, "new", "开始新的 Kernel 对话"),
            (goal_command, "goal", "查看或更新当前对话目标"),
            (resume_command, "resume", "恢复等待用户输入的 Run"),
            (pending_command, "pending", "查看当前会话待处理任务"),
            (status_command, "status", "查看当前 Session 和上下文余额"),
            (kernel_status_command, "kernel-status", "查看 Kernel 和最近 Run 状态"),
            (audit_command, "audit", "只读审计当前 Project 的改装闭环"),
            (supervisor_command, "supervisor", "咨询当前Project的Goal Supervisor"),
            (prompt_command, "prompt", "查看Supervisor当前Action Plan"),
            (stage_command, "stage", "查看并解释当前Task阶段"),
            (why_command, "why", "解释当前流程决定或阻塞原因"),
            (bind_project_command, "bind_project", "将当前频道绑定到 Project"),
            (unbind_project_command, "unbind_project", "解除当前频道的 Project 绑定"),
            (binding_command, "binding", "查看当前频道绑定"),
            (whoami_command, "whoami", "查看当前 Project 和 Agent"),
            (help_command, "help", "查看可用指令"),
            (pm_command, "pm", "进入当前 Project Manager"),
        )
        for callback, name, description in commands:
            self.tree.command(name=name, description=description)(callback)

    async def setup_hook(self) -> None:
        for message in await self.repository.list_active_discord_component_messages():
            view = build_discord_view(message, self)
            external_id = str(message.get("external_message_id") or "")
            if view is not None and external_id.isdigit():
                self.add_view(view, message_id=int(external_id))
        for guild_id in sorted(self.config.get("guild_ids") or set()):
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logging.info(
                "Discord application commands synced guild_id=%s count=%s",
                guild_id, len(synced),
            )

    async def _component_message(
        self, interaction: discord.Interaction, message_id: str,
    ) -> dict[str, Any]:
        message = await self.repository.get_message(message_id)
        if message is None or str(message.get("channel")) != "discord":
            raise PermissionError("component message is unknown")
        if str(message.get("chat_id")) != str(interaction.channel_id or ""):
            raise PermissionError("component belongs to another Discord conversation")
        if message.get("user_id") is not None and str(message.get("user_id")) != str(interaction.user.id):
            content = dict(message.get("content_json") or {})
            owner_override = (
                content.get("interaction_kind") == "reconcile_run"
                and str(interaction.user.id) in set(self.config.get("owner_user_ids") or set())
            )
            if not owner_override:
                raise PermissionError("component belongs to another user")
        if interaction.message is not None and str(message.get("external_message_id") or "") != str(interaction.message.id):
            raise PermissionError("component external message differs")
        return message

    async def _refresh_questionnaire_card(
        self, interaction: discord.Interaction, message_id: str,
    ) -> None:
        current = await self._component_message(interaction, message_id)
        if not is_questionnaire(dict(current.get("content_json") or {})):
            raise ValueError("not a questionnaire")
        view = build_discord_view(current, self)
        text = render_discord_text(current)
        if view is not None:
            try:
                await self.repository.validate_current_waiting_message(
                    message_id=message_id, project_id=str(current["project_id"]),
                    channel="discord", chat_id=str(current["chat_id"]),
                    user_id=str(interaction.user.id),
                )
            except PermissionError:
                view = None
                text = "这组问题已失效，请使用当前卡片或 /resume 恢复待答任务。"
        target = interaction.message
        if target is None and interaction.channel is not None:
            external_id = str(current.get("external_message_id") or "")
            if external_id.isdigit() and hasattr(interaction.channel, "get_partial_message"):
                target = interaction.channel.get_partial_message(int(external_id))
        if target is not None:
            await target.edit(
                content=text, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def handle_questionnaire_component(
        self, interaction: discord.Interaction, message_id: str, action: str,
        value: str | None, message_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Navigation and modals share durable state; they never create an LLM Run."""
        try:
            parts = action.split(":")
            if len(parts) not in {4, 5} or parts[0] != "q":
                raise ValueError("invalid questionnaire action")
            revision, page, verb = int(parts[1]), int(parts[2]), parts[3]
            if verb == "choice":
                if len(parts) != 5:
                    raise ValueError("invalid questionnaire choice")
                value = parts[4]
            elif len(parts) != 4:
                raise ValueError("invalid questionnaire action")
            if verb in {"write", "supplement"}:
                # No database wait before the modal acknowledgement deadline.
                snapshot = dict(message_snapshot or {})
                if (
                    str(snapshot.get("message_id")) != message_id
                    or str(snapshot.get("chat_id")) != str(interaction.channel_id)
                    or str(snapshot.get("user_id")) != str(interaction.user.id)
                ):
                    raise PermissionError("form belongs to another user or conversation")
                body = snapshot.get("content_json") or {}
                state = body.get("questionnaire") or {}
                if (
                    not is_questionnaire(body) or state.get("status") != "draft"
                    or int(state.get("revision", -1)) != revision
                    or int(state.get("cursor", -1)) != page
                    or (verb == "supplement") != (page == len(body["questions"]))
                ):
                    raise ValueError("form is no longer current")
                await interaction.response.send_modal(KernelQuestionnaireModal(
                    self, snapshot, revision=revision, page=page,
                    additional=verb == "supplement",
                ))
                return
            ack = await self._ack_interaction(interaction)
            if ack.status != "ok":
                await self._queue_component_retry_notice(interaction, message_id)
                return
            current = await self._component_message(interaction, message_id)
            if verb != "refresh":
                for attempt, delay in enumerate(DISCORD_INGRESS_RETRY_DELAYS, start=1):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        result = await self.repository.record_questionnaire_action(
                            message_id=message_id, project_id=str(current["project_id"]),
                            channel="discord", chat_id=str(current["chat_id"]),
                            user_id=str(interaction.user.id),
                            external_event_id=_external_event_id(self.config, str(interaction.id)),
                            revision=revision, page=page, action=verb, value=value,
                        )
                        break
                    except (OperationalError, InterfaceError, PoolTimeout, OSError, asyncio.TimeoutError):
                        if attempt == len(DISCORD_INGRESS_RETRY_DELAYS):
                            raise
                self.health.update(last_ingress_commit_at=now_iso(), last_ingress_error_type=None)
                if result.get('accepted_into_supervision'):
                    with contextlib.suppress(discord.HTTPException,aiohttp.ClientError):
                        await interaction.followup.send('正式答案已保存，交由 Supervisor 评估；下一轮具体执行包仍需确认。',ephemeral=True)
                if result.get("resumed"):
                    with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                        await interaction.followup.send("答案已提交，任务将继续处理。", ephemeral=True)
            await self._refresh_questionnaire_card(interaction, message_id)
        except (PermissionError, ValueError) as exc:
            # Reject stale pages without removing a valid current questionnaire.
            with contextlib.suppress(Exception):
                await self._refresh_questionnaire_card(interaction, message_id)
            with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                if interaction.response.is_done():
                    await interaction.followup.send(str(exc), ephemeral=True)
                else:
                    await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception:
            logging.exception("Discord questionnaire interaction failed")
            with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                text = "卡片暂时无法更新。已保存的答案不会丢失，可点击刷新后重试。"
                if interaction.response.is_done():
                    await interaction.followup.send(text, ephemeral=True)
                else:
                    await interaction.response.send_message(text, ephemeral=True)

    async def handle_kernel_component(
        self, interaction: discord.Interaction, message_id: str,
        action: str, value: str | None,
        message_snapshot: dict[str, Any] | None = None,
    ) -> None:
        if action.startswith("q:"):
            await self.handle_questionnaire_component(
                interaction, message_id, action, value, message_snapshot,
            )
            return
        try:
            if action == "credential":
                message = dict(message_snapshot or {})
                if (
                    message.get("user_id") is not None
                    and str(message.get("user_id")) != str(interaction.user.id)
                ):
                    raise PermissionError("credential component belongs to another user")
                content = dict(message.get("content_json") or {})
                schema = dict(content.get("input_schema") or {})
                fields = [
                    dict(item) for item in schema.get("fields") or []
                    if isinstance(item, dict)
                ]
                if schema.get("kind") != "project_credential" or not 1 <= len(fields) <= 5:
                    raise ValueError("credential form is unavailable")
                try:
                    await interaction.response.send_modal(
                        KernelCredentialModal(self, message, fields)
                    )
                except (
                    discord.NotFound, discord.HTTPException,
                    aiohttp.ClientError, OSError, asyncio.TimeoutError,
                ):
                    self.health.update(
                        last_interaction_at=now_iso(),
                        last_interaction_ack_status="expired",
                        last_interaction_age_ms=_interaction_age_ms(interaction),
                    )
                    await self._queue_component_retry_notice(interaction, message_id)
                    return
                self.health.update(
                    last_interaction_at=now_iso(),
                    last_interaction_ack_status="ok",
                    last_interaction_ack_ms=0,
                    last_interaction_age_ms=_interaction_age_ms(interaction),
                )
                return
            ack = await self._ack_interaction(interaction)
            if ack.status != "ok":
                await self._queue_component_retry_notice(interaction, message_id)
                return
            message = await self._component_message(interaction, message_id)
            if action.startswith('prompt:'):
                from assistant_kernel.services.prompt_protocol import PromptProtocol
                parts=action.split(':');body=message['content_json'];package=body.get('package_ref') or {}
                if len(parts)!=3 or parts[2]!=str(package.get('sha256') or '')[:12]:
                    raise ValueError('Prompt action refers to a different package version')
                result=await PromptProtocol(self.settings,self.repository).record_confirmation(
                    display_message_id=message_id,project_id=str(message['project_id']),channel='discord',
                    chat_id=str(interaction.channel_id),user_id=str(interaction.user.id),
                    external_event_id=_external_event_id(self.config,str(interaction.id)),
                    action=parts[1],expected_package_sha256=package['sha256'])
                response='已确认这份执行包，等待 PM 领取。' if result.get('confirmation_ref') else '已记录，当前执行包不会派发。'
                if interaction.message is not None: await interaction.message.edit(view=None)
            elif action.startswith("reconcile:"):
                selected_action = action.split(":", 1)[1]
                result = await self.reconciliations.resolve_card(
                    card_message_id=message_id,
                    external_event_id=_external_event_id(self.config, str(interaction.id)),
                    actor_user_id=str(interaction.user.id),
                    action=selected_action,
                )
                response = (
                    f"Reconciliation action `{result['action']}` recorded; "
                    f"Run status is `{result['status']}`."
                )
            elif action.startswith("supervision-recovery:"):
                result = await self.repository.record_supervision_recovery(
                    card_message_id=message_id,
                    external_event_id=_external_event_id(self.config, str(interaction.id)),
                    actor_user_id=str(interaction.user.id), action=action.split(":", 1)[1],
                )
                response = f"监督恢复动作已记录：{result.get('action') or action.split(':', 1)[1]}。"
            elif action.startswith("supervisor:"):
                selected_action = action.split(":", 1)[1]
                if selected_action in {"continue", "reject"}:
                    response = (
                        "This legacy Supervisor card is no longer actionable. "
                        "Send a new message to Supervisor to continue."
                    )
                    if interaction.message is not None:
                        await interaction.message.edit(view=None)
                    await interaction.followup.send(
                        kernel_ui_text(response), ephemeral=True,
                    )
                    return
                if selected_action not in {"adopt", "dismiss", "revise"}:
                    raise ValueError("unsupported Supervisor card action")
                result = await self.repository.record_supervisor_action(
                    card_message_id=message_id,
                    external_event_id=_external_event_id(
                        self.config, str(interaction.id)
                    ),
                    actor_user_id=str(interaction.user.id),
                    action=selected_action,
                )
                from assistant_kernel.services.decision_records import (
                    DecisionRecordService,
                )
                project_id = str(message["project_id"])
                task_id = str(result["task_id"])
                decision_text = {
                    "adopt": "用户确认当前Supervisor Action Plan。",
                    "dismiss": "用户取消当前Supervisor Action Plan。",
                    "revise": "用户请求修改当前Supervisor Action Plan；尚未提供具体修改内容。",
                }[selected_action]
                if not result.get("duplicate"):
                    DecisionRecordService(self.settings).append(
                        project_id, task_id,
                        kind=str(result.get("kind") or "correction"),
                        text=decision_text,
                        source_message_ids=[message_id, str(result["message_id"])],
                        record_id=str(result["decision_record_id"]),
                        scope={
                            "supervisor_decision_id": result.get(
                                "supervisor_decision_id"
                            ),
                            "target_ref": result.get("target_ref"),
                        },
                    )
                response = {
                    "adopt": "Action Plan confirmed; the PM Task may continue.",
                    "dismiss": "Action Plan cancelled without changing the PM Task.",
                    "revise": "Revision requested; reply to the card with details.",
                }[selected_action]
            elif action.startswith("choice:"):
                content = dict(message.get("content_json") or {})
                result = await self.repository.record_question_choice(
                    project_id=str(message["project_id"]), channel="discord",
                    chat_id=str(message["chat_id"]), user_id=str(interaction.user.id),
                    external_event_id=_external_event_id(self.config, str(interaction.id)),
                    question_message_id=message_id, run_id=str(message["run_id"]),
                    question_id=str(content.get("question_id") or ""),
                    interaction_seq=int(content.get("interaction_seq") or -1),
                    choice_id=action.split(":", 1)[1],
                )
                status = str(result.get("choice_status") or "recorded")
                response = {
                    "recorded": "Choice recorded.",
                    "already_answered": "This question was already answered.",
                    "expired": "This question expired; use /resume if it is still recoverable.",
                }.get(status, "Choice received.")
            elif action in {"approve", "revise", "reject"}:
                result = await ingest_discord_event(self.settings, {
                    "event_id": str(interaction.id),
                    "interaction_id": str(interaction.id),
                    "guild_id": str(interaction.guild_id or "") or None,
                    "channel_id": str(interaction.channel_id or ""),
                    "parent_channel_id": str(getattr(interaction.channel, "parent_id", "") or "") or None,
                    "user_id": str(interaction.user.id), "author_bot": False,
                    "member": _member_snapshot(interaction.user),
                    "content": action, "reply_message_id": str(interaction.message.id),
                    "is_thread": isinstance(interaction.channel, discord.Thread),
                    "attachments": [],
                })
                response = "Decision recorded."
            elif action == "resume" and value:
                await ingest_discord_event(self.settings, {
                    "event_id": str(interaction.id), "interaction_id": str(interaction.id),
                    "guild_id": str(interaction.guild_id or "") or None,
                    "channel_id": str(interaction.channel_id or ""),
                    "parent_channel_id": str(getattr(interaction.channel, "parent_id", "") or "") or None,
                    "user_id": str(interaction.user.id), "author_bot": False,
                    "member": _member_snapshot(interaction.user),
                    "content": f"/resume {value}", "is_thread": isinstance(interaction.channel, discord.Thread),
                    "attachments": [],
                })
                response = "Run selected for resume."
            else:
                raise ValueError("unsupported Discord component action")
            if interaction.message is not None:
                await interaction.message.edit(view=None)
            with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                await interaction.followup.send(kernel_ui_text(response), ephemeral=True)
        except Exception as exc:
            logging.exception("Discord component handling failed")
            text = "This action could not be completed. It may be stale or unauthorized."
            if interaction.message is not None:
                with contextlib.suppress(discord.HTTPException):
                    await interaction.message.edit(view=None)
            with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                if interaction.response.is_done():
                    await interaction.followup.send(kernel_ui_text(text), ephemeral=True)
                else:
                    await interaction.response.send_message(kernel_ui_text(text), ephemeral=True)

    async def handle_credential_submit(
        self, interaction: discord.Interaction, message: dict[str, Any],
        values: dict[str, str],
    ) -> None:
        try:
            ack = await self._ack_interaction(interaction)
            if ack.status != "ok":
                await self._queue_component_retry_notice(
                    interaction, str(message["message_id"])
                )
                return
            current = await self._component_message(interaction, str(message["message_id"]))
            await self.repository.validate_current_waiting_message(
                message_id=str(current["message_id"]),
                project_id=str(current["project_id"]), channel="discord",
                chat_id=str(current["chat_id"]), user_id=str(interaction.user.id),
            )
            content = dict(current.get("content_json") or {})
            schema = dict(content.get("input_schema") or {})
            request_id = str(schema.get("request_id") or "")
            credential_key = str(schema.get("credential_key") or "")
            credential_store = ProjectCredentialsStore(self.settings.data_root)
            credential_result: dict[str, Any] = {}

            def save_credential() -> dict[str, Any]:
                result = credential_store.complete_request(
                    project_id=str(current["project_id"]), request_id=request_id,
                    fields=values,
                    external_event_id=_external_event_id(self.config, str(interaction.id)),
                )
                credential_result.update(result)
                return result

            await ingest_discord_event_with_retry(self.settings, {
                "event_id": f"credential-answer:{interaction.id}",
                "guild_id": str(interaction.guild_id or "") or None,
                "channel_id": str(interaction.channel_id or ""),
                "parent_channel_id": str(getattr(interaction.channel, "parent_id", "") or "") or None,
                "user_id": str(interaction.user.id), "author_bot": False,
                "member": _member_snapshot(interaction.user),
                "content": f"Project credential saved: {credential_key}",
                "reply_message_id": str(current["external_message_id"]),
                "is_thread": isinstance(interaction.channel, discord.Thread),
                "attachments": [],
            }, health=self.health, before_resume=save_credential)
            channel = interaction.channel
            if channel is not None and hasattr(channel, "get_partial_message"):
                await channel.get_partial_message(int(str(current["external_message_id"]))).edit(view=None)
            with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                await interaction.followup.send(
                    kernel_ui_text(
                        f"Credential `{credential_result.get('credential_key') or credential_key}` saved; the waiting Run will continue."
                    ),
                    ephemeral=True,
                )
        except Exception:
            logging.exception("Discord credential form failed")
            text = "Credential submission failed or expired."
            with contextlib.suppress(discord.HTTPException, aiohttp.ClientError):
                if interaction.response.is_done():
                    await interaction.followup.send(kernel_ui_text(text), ephemeral=True)
                else:
                    await interaction.response.send_message(kernel_ui_text(text), ephemeral=True)

    async def on_ready(self) -> None:
        self.health.update(
            connected=True, last_ready_at=now_iso(), last_error_type=None,
            guild_count=len(self.guilds),
        )
        logging.info(
            "Discord gateway ready bot_user_id=%s guild_count=%s",
            getattr(self.user, "id", None), len(self.guilds),
        )

    async def on_disconnect(self) -> None:
        self.health.update(last_disconnect_at=now_iso())
        self.health.reconnecting()

    async def on_resumed(self) -> None:
        self.health.update(
            connected=True, last_ready_at=now_iso(), last_resume_at=now_iso(),
            last_error_type=None,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or (self.user is not None and message.author.id == self.user.id):
            return
        if str(message.channel.id) in core_channel_ids(self.settings):
            return  # this channel belongs to the rebuilt core's gateway (config/core-discord.yaml)
        self.health.update(last_event_at=now_iso())
        parent_id = getattr(message.channel, "parent_id", None)
        payload = {
            "event_id": str(message.id),
            "message_id": str(message.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "channel_id": str(message.channel.id),
            "parent_channel_id": str(parent_id) if parent_id else None,
            "channel_name": str(getattr(message.channel, "name", "") or "") or None,
            "parent_channel_name": str(
                getattr(getattr(message.channel, "parent", None), "name", "") or ""
            ) or None,
            "user_id": str(message.author.id),
            "member": _member_snapshot(message.author),
            "author_bot": bool(message.author.bot),
            "content": message.content or "",
            "reply_message_id": (
                str(message.reference.message_id)
                if message.reference and message.reference.message_id else None
            ),
            "is_thread": isinstance(message.channel, discord.Thread),
            "attachments": [
                {
                    "id": str(item.id),
                    "filename": item.filename,
                    "size": item.size,
                    "content_type": item.content_type,
                }
                for item in message.attachments
            ],
        }
        try:
            await ingest_discord_event_with_retry(
                self.settings, payload, health=self.health,
            )
        except PermissionError:
            logging.warning(
                "Discord message rejected guild_id=%s channel_id=%s user_id=%s",
                payload["guild_id"], payload["channel_id"], payload["user_id"],
            )
        except Exception:
            logging.exception(
                "Discord message ingestion failed event_id=%s", payload["event_id"]
            )


async def discord_delivery_loop(
    settings: KernelSettings, client: discord.Client,
) -> None:
    await client.wait_until_ready()
    repository = CoreRepository(settings)
    sender = DiscordSender(settings, repository, client)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:discord"
    worker_instance_id = str(uuid.uuid4())
    while not client.is_closed():
        try:
            message = await repository.claim_delivery(
                worker_id, worker_instance_id, 90, channel="discord"
            )
            if message is None:
                await asyncio.sleep(settings.worker_poll_seconds)
                continue
            try:
                await sender.send(message, worker_instance_id)
            except Exception as exc:
                delivery_status = await repository.fail_delivery(
                    str(message["message_id"]), worker_instance_id, str(exc)
                )
                health = getattr(client, "health", None)
                if health is not None:
                    health.update(
                        last_delivery_error_at=now_iso(),
                        last_delivery_error_type=type(exc).__name__,
                        last_delivery_failure_status=delivery_status,
                    )
                logging.exception("Discord delivery failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Discord delivery loop failed")
            await asyncio.sleep(settings.worker_poll_seconds)


async def discord_health_pulse(client: KernelDiscordClient) -> None:
    interval = 10.0
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    while True:
        await asyncio.sleep(interval)
        observed = loop.time()
        lag = max(observed - expected, 0.0)
        expected = observed + interval
        latency = float(getattr(client, "latency", math.inf))
        client.health.update(
            process_running=True,
            last_process_heartbeat_at=now_iso(),
            gateway_latency_seconds=(
                round(latency, 3) if math.isfinite(latency) else None
            ),
            event_loop_lag_seconds=round(lag, 3),
        )


async def discord_worker_loop() -> None:
    from assistant_kernel.services.config_consumers import startup_settings
    settings = startup_settings(KernelSettings.from_environment(),'discord')
    health = ChannelHealthReporter(settings, "discord")
    while True:
        config = load_discord_config(settings)
        if not config.get("enabled"):
            health.update(connected=False, enabled=False)
            await asyncio.sleep(max(settings.worker_poll_seconds, 5.0))
            continue
        try:
            validate_discord_config(config)
            repository = CoreRepository(settings)
            health.update(enabled=True, last_error_type=None)
            client = KernelDiscordClient(settings, config, repository, health)
            delivery = asyncio.create_task(discord_delivery_loop(settings, client))
            pulse = asyncio.create_task(discord_health_pulse(client))
            try:
                await client.start(str(config["bot_token"]), reconnect=True)
            finally:
                delivery.cancel()
                pulse.cancel()
                await asyncio.gather(delivery, pulse, return_exceptions=True)
                if not client.is_closed():
                    await client.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            health.update(
                connected=False, last_error_type=type(exc).__name__,
                last_error_at=now_iso(),
            )
            logging.exception("Discord gateway stopped; retrying")
            await asyncio.sleep(5)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(discord_worker_loop())


if __name__ == "__main__":
    run()
