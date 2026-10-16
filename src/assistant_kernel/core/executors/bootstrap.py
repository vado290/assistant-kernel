"""Project bootstrap executor (phase 5): creates a new project deterministically from an approved spec.

Every step can be repeated safely: files are written only when absent, Discord channels are found again by
a marker in their topic, and the routing entry and project row are upserts.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from assistant_kernel.core import skills
from assistant_kernel.storage import ProjectRuntimeManager

TEMPLATE = Path(__file__).parents[1] / "project_template"
DISCORD_API = "https://discord.com/api/v10"


def fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def write_project_files(settings, spec: dict[str, Any]) -> list[str]:
    runtime = ProjectRuntimeManager(settings.data_root)
    vault = runtime.describe(spec["project_id"]).vault_root
    existing = vault / "project.yaml"
    ours = existing.is_file() and \
        (yaml.safe_load(existing.read_text("utf-8")) or {}).get("created_by") == "kernel-core-bootstrap"
    if vault.exists() and not ours:
        raise PermissionError(f"a vault for {spec['project_id']} already exists and was not created by this bootstrap")
    runtime.ensure(spec["project_id"])
    values = {"project_id": spec["project_id"], "display_name": spec["display_name"], "purpose": spec["purpose"],
              "created_at": datetime.now(timezone.utc).date().isoformat(), **spec["business_profile"]}
    written = []
    # project.yaml first: it marks the vault as ours, so an interrupted run can be repeated.
    for source in sorted((p for p in TEMPLATE.rglob("*") if p.is_file()), key=lambda p: p.name != "project.yaml"):
        target = vault / source.relative_to(TEMPLATE)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fill(source.read_text("utf-8"), values), "utf-8")
        written.append(target.relative_to(vault).as_posix())
    capabilities = skills.project_config(vault)["capabilities"]
    written += [f".agents/skills/{name}" for name in skills.install_templates(vault, capabilities)]
    return written


class DiscordChannels:
    """Create (or find again) a project's category and #pm / #supervisor channels via the Discord REST API."""

    def __init__(self, token: str, guild_id: str, base_url: str = DISCORD_API):
        self.token, self.guild_id, self.base_url = token, guild_id, base_url

    async def ensure(self, project_id: str, display_name: str, purpose: str) -> dict[str, str]:
        headers = {"Authorization": f"Bot {self.token}"}
        async with httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30) as client:
            existing = (await client.get(f"/guilds/{self.guild_id}/channels")).raise_for_status().json()

            async def find_or_create(marker: str | None, payload: dict[str, Any]) -> str:
                for channel in existing:
                    if (marker and marker in str(channel.get("topic") or "")) or \
                            (not marker and channel.get("type") == 4 and channel.get("name") == payload["name"]):
                        return str(channel["id"])
                created = (await client.post(f"/guilds/{self.guild_id}/channels", json=payload)).raise_for_status().json()
                existing.append(created)
                return str(created["id"])

            category = await find_or_create(None, {"name": f"PROJECT · {project_id}", "type": 4})
            channels = {}
            for role, title in (("pm", "PM：在这里提需求、补充信息"), ("supervisor", "Supervisor：卡片、咨询")):
                marker = f"core-project:{project_id}:{role}"
                channels[role] = await find_or_create(marker, {
                    "name": role, "type": 0, "parent_id": category,
                    "topic": f"{display_name} · {title}\n{purpose[:300]}\n{marker}"})
            return {"category_id": category, "pm_channel_id": channels["pm"],
                    "supervisor_channel_id": channels["supervisor"]}


def add_routing(config_path: Path, project_id: str, channels: dict[str, str]) -> None:
    data = yaml.safe_load(config_path.read_text("utf-8")) or {}
    data.setdefault("projects", {})[project_id] = {"pm_channel_id": channels["pm_channel_id"],
                                                    "supervisor_channel_id": channels["supervisor_channel_id"]}
    temporary = config_path.with_suffix(".yaml.tmp")
    temporary.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), "utf-8")
    temporary.replace(config_path)   # the gateway reloads the file when it changes


def channel_creator(settings) -> DiscordChannels:
    from assistant_kernel.core.gateway.config import bot_token, load_gateway_config
    config = load_gateway_config(gateway_config_path(settings))
    token = bot_token(settings, config, shared_fallback=True)
    if not token:
        raise RuntimeError("no Discord bot token available for creating channels")
    return DiscordChannels(token, config.guild_id)


def gateway_config_path(settings) -> Path:
    return Path(os.environ.get("CORE_DISCORD_CONFIG") or settings.data_root / "config" / "core-discord.yaml")


async def execute_bootstrap(run: dict[str, Any], ctx) -> dict[str, Any]:
    settings, spec = ctx.services["settings"], run["input"]["spec"]
    files = await asyncio.to_thread(write_project_files, settings, spec)
    creator = ctx.services.get("discord_channels") or channel_creator(settings)
    channels = await creator.ensure(spec["project_id"], spec["display_name"], spec["purpose"])
    await asyncio.to_thread(add_routing, gateway_config_path(settings), spec["project_id"], channels)
    await ctx.kernel.ensure_project(spec["project_id"])
    return {"outcome": "created", "project_id": spec["project_id"], "channels": channels, "files": files,
            "guild_id": getattr(creator, "guild_id", None)}
