"""Gateway configuration: one explicit file mapping project channels (config/core-discord.yaml).

    bot_token_env: CORE_DISCORD_BOT_TOKEN      # name of the env var holding the bot token
    guild_id: "123"
    allowed_user_ids: ["456"]
    projects:
      kernel-v12-validation:
        pm_channel_id: "789"
        supervisor_channel_id: "790"
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ProjectChannels:
    project_id: str
    pm_channel_id: str
    supervisor_channel_id: str


@dataclass(frozen=True)
class GatewayConfig:
    guild_id: str
    allowed_user_ids: frozenset[str]
    projects: dict[str, ProjectChannels] = field(default_factory=dict)
    bot_token_env: str = "CORE_DISCORD_BOT_TOKEN"
    # shared_bot: reuse the legacy bot. Slash commands are then NOT registered (that would replace the
    # legacy bot's commands); /status, /pending and /cancel work as plain text in the core channels.
    shared_bot: bool = False

    def route(self, channel_id: str) -> tuple[str, str] | None:
        """(project_id, role) for a channel, or None when the channel belongs to no project."""
        for project in self.projects.values():
            if channel_id == project.pm_channel_id:
                return project.project_id, "pm"
            if channel_id == project.supervisor_channel_id:
                return project.project_id, "supervisor"
        return None

    def allowed(self, user_id: str) -> bool:
        return not self.allowed_user_ids or user_id in self.allowed_user_ids


def bot_token(settings, config: GatewayConfig, *, shared_fallback: bool) -> str | None:
    """The bot token from ``config.bot_token_env``; with ``shared_fallback`` also the shared bot's source
    (``DISCORD_BOT_TOKEN`` or ``services.discord.bot_token`` in the service credentials file)."""
    token = os.environ.get(config.bot_token_env)
    if token or not shared_fallback:
        return token or None
    if os.environ.get("DISCORD_BOT_TOKEN"):
        return os.environ["DISCORD_BOT_TOKEN"]
    path = settings.service_credentials_path
    if not path.is_file():
        return None
    services = json.loads(path.read_text(encoding="utf-8-sig")).get("services") or {}
    return (services.get("discord") or {}).get("bot_token") or None


def load_gateway_config(path: Path) -> GatewayConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    projects = {
        str(pid): ProjectChannels(str(pid), str(p["pm_channel_id"]), str(p["supervisor_channel_id"]))
        for pid, p in (data.get("projects") or {}).items()
    }
    return GatewayConfig(guild_id=str(data["guild_id"]),
                         allowed_user_ids=frozenset(str(u) for u in data.get("allowed_user_ids") or []),
                         projects=projects, bot_token_env=str(data.get("bot_token_env") or "CORE_DISCORD_BOT_TOKEN"),
                         shared_bot=bool(data.get("shared_bot", False)))
