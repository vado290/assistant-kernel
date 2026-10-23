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
    """The bot token: the env var ``config.bot_token_env``; with ``shared_fallback`` the shared bot's
    ``DISCORD_BOT_TOKEN``; then ``services.discord.bot_token`` in the service credentials file, where the dashboard
    saves it (ROUND6)."""
    token = os.environ.get(config.bot_token_env)
    if token:
        return token
    if shared_fallback and os.environ.get("DISCORD_BOT_TOKEN"):
        return os.environ["DISCORD_BOT_TOKEN"]
    return saved_bot_token(settings)


def saved_bot_token(settings) -> str | None:
    path = settings.service_credentials_path
    if not path.is_file():
        return None
    services = json.loads(path.read_text(encoding="utf-8-sig")).get("services") or {}
    return (services.get("discord") or {}).get("bot_token") or None


def save_bot_token(settings, token: str) -> None:
    """Write the bot token into the service credentials file (created with mode 600; other entries kept)."""
    path = settings.service_credentials_path
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    data.setdefault("services", {}).setdefault("discord", {})["bot_token"] = token
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def load_gateway_config(path: Path) -> GatewayConfig:
    """The routing file; a missing file or guild (fresh installation, ROUND6) gives an empty guild id."""
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
    projects = {
        str(pid): ProjectChannels(str(pid), str(p["pm_channel_id"]), str(p["supervisor_channel_id"]))
        for pid, p in (data.get("projects") or {}).items()
    }
    return GatewayConfig(guild_id=str(data.get("guild_id") or ""),
                         allowed_user_ids=frozenset(str(u) for u in data.get("allowed_user_ids") or []),
                         projects=projects, bot_token_env=str(data.get("bot_token_env") or "CORE_DISCORD_BOT_TOKEN"),
                         shared_bot=bool(data.get("shared_bot", False)))


def save_gateway_settings(path: Path, *, guild_id: str, allowed_user_ids: list[str]) -> None:
    """Set the guild and the allowed users in the routing file; project channels and other keys stay."""
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
    data["guild_id"] = str(guild_id)
    data["allowed_user_ids"] = [str(u) for u in allowed_user_ids]
    data.setdefault("projects", {})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
    temporary.replace(path)   # the gateway notices the change and restarts itself
