"""Project bootstrap executor (phase 5): creates a new project deterministically from an approved spec.

Every step can be repeated safely: files are written only when absent, Discord channels are found again by
a marker in their topic, and the routing entry and project row are upserts.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from assistant_kernel.core import skills
from assistant_kernel.core.gitdelivery import init_repository
from assistant_kernel.storage import ProjectRuntimeManager

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).parents[1] / "project_template"
DISCORD_API = "https://discord.com/api/v10"


def fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


# Sections of the draft that do not belong in the charter (change reasons live in core.iterations, ROUND4).
CHARTER_ONLY_IN_TASK = ("创建参数", "用户画像观察", "修订记录", "变更记录")
ARCHIVE_TITLE = "背景信息存档"   # collected while creating the project; goes to knowledge/background.md (0.45.6)
ROLE_SECTIONS = {"pm": "本项目对 PM 的特别要求", "supervisor": "本项目对 Supervisor 的特别要求"}


def drop_section(markdown: str, title: str) -> str:
    lines = markdown.splitlines()
    bounds = skills.section_bounds(lines, title)
    return markdown if bounds is None else "\n".join(lines[:bounds[0]] + lines[bounds[2]:])


def render_charter(spec: dict[str, Any], draft: str, created: str, task_id: str) -> str:
    """The new project's knowledge/charter.md from the approved charter draft of the creation task."""
    body = draft
    for title in CHARTER_ONLY_IN_TASK:
        body = drop_section(body, title)
    body = "\n".join(line for line in body.splitlines() if not line.startswith("# ")).strip()   # its own title goes
    header = (f"# {spec['display_name']} · 项目章程\n\n> 项目 ID：`{spec['project_id']}` · 创建于 {created}（任务 {task_id[:8]}）。"
              "PM 和 Supervisor 每一轮都会读本章程；第 1、5、7 节同时用于 kernel 的项目路由。\n"
              "> 各节通过任务修改（修改原因记在项目的变更流水里）。背景信息存档在 `knowledge/background.md`，"
              "由 Kernel 在需求文档获批时追加。\n\n")
    return header + drop_section(body, ARCHIVE_TITLE).strip() + "\n"


def write_project_files(settings, spec: dict[str, Any]) -> list[str]:
    """Create the project's vault from the template and the approved charter. Repeatable: the vault is ours
    (project.yaml is written first and marks it), and everything below is derived deterministically from `spec`."""
    from assistant_kernel.core.executors.common import read_document
    from assistant_kernel.core.vault_sync import append_archive, append_profile, write_brief
    runtime = ProjectRuntimeManager(settings.data_root)
    vault = runtime.describe(spec["project_id"]).vault_root
    existing = vault / "project.yaml"
    ours = existing.is_file() and \
        (yaml.safe_load(existing.read_text("utf-8")) or {}).get("created_by") == "kernel-core-bootstrap"
    if vault.exists() and not ours:
        raise PermissionError(f"a vault for {spec['project_id']} already exists and was not created by this bootstrap")
    runtime.ensure(spec["project_id"])
    created = datetime.now(timezone.utc).date().isoformat()
    values = {"project_id": spec["project_id"], "display_name": spec["display_name"], "purpose": spec["purpose"],
              "created_at": created, "industry": "（见章程）", "organization": "（见章程）", "users": "（见章程）",
              "notes": "（见章程）"}
    written = []
    # project.yaml first: it marks the vault as ours, so an interrupted run can be repeated.
    for source in sorted((p for p in TEMPLATE.rglob("*") if p.is_file()), key=lambda p: p.name != "project.yaml"):
        target = vault / source.relative_to(TEMPLATE)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fill(source.read_text("utf-8"), values), "utf-8", newline="\n")
        written.append(target.relative_to(vault).as_posix())
    config = yaml.safe_load(existing.read_text("utf-8")) or {}
    config.update({key: spec[key] for key in ("capabilities", "delivery", "share_with_kernel") if key in spec})
    if spec.get("delivery") == "git-repo":   # code delivery: the project's own repository inside its vault
        config.setdefault("repository", "repo")
        config.setdefault("main_branch", "main")
        if init_repository(vault / config["repository"], config["main_branch"],
                           f"# {spec['display_name']}\n\n{spec['purpose']}\n"):
            written.append(f"{config['repository']}/")
    existing.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), "utf-8", newline="\n")
    source = spec.get("source_project") or "kernel"   # the creation task's documents live in the creating project
    charter_doc = spec.get("charter")
    draft = read_document(settings, source, charter_doc) if charter_doc else ""
    if draft:
        task_id = str(spec.get("task_id") or "")
        (vault / "knowledge" / "charter.md").write_text(render_charter(spec, draft, created, task_id), "utf-8",
                                                        newline="\n")
        facts = skills.section_text(draft, ARCHIVE_TITLE)
        if facts.strip() and facts.strip() not in ("无", "无。", "（无）"):
            append_archive(vault / "knowledge" / "background.md", f"archive:{charter_doc['sha256'][:12]}",
                           f"{created} · 创建项目时收集", facts)
        observed = skills.section_text(draft, "用户画像观察")
        if observed.strip() and observed.strip() not in ("无", "无。", "（无）"):
            append_profile(vault / "knowledge" / "user-profile.md", spec.get("user_id") or "默认",
                           f"profile:{charter_doc['sha256'][:12]}", f"{created} · 创建项目时的观察", observed)
    for role, title in ROLE_SECTIONS.items():
        note = spec.get(f"{role}_role")
        path = vault / "agents" / role / "role.md"
        if note and path.is_file():
            path.write_text(skills.replace_section(path.read_text("utf-8"), title, note), "utf-8", newline="\n")
    brief = spec.get("brief")
    if brief:
        research = read_document(settings, source, brief)
        if research.strip():
            write_brief(vault / "knowledge" / "domain-brief.md", f"brief:{brief['sha256'][:12]}",
                        f"{created} · 创建项目时的调研", research)
    capabilities = skills.project_config(vault)["capabilities"]
    written += [f"agents/{copy.split('/')[0]}/.agents/skills/{copy.split('/')[1]}"
                for copy in skills.install_templates(vault, capabilities)]
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


def first_commit(settings, spec: dict[str, Any]) -> None:
    """The new project's own repository and its first commit (ROUND5 D13); the remote follows on the next push."""
    from assistant_kernel.core import projectgit
    vault = ProjectRuntimeManager(settings.data_root).describe(spec["project_id"]).vault_root
    try:
        projectgit.ensure_repo(vault)
        projectgit.commit_all(vault, f"创建项目 {spec['display_name']}")
    except (projectgit.GitError, OSError) as exc:   # a project without history still works
        log.warning("first commit of %s failed: %s", spec["project_id"], exc)


async def execute_bootstrap(run: dict[str, Any], ctx) -> dict[str, Any]:
    settings, spec = ctx.services["settings"], run["input"]["spec"]
    files = await asyncio.to_thread(write_project_files, settings, spec)
    await asyncio.to_thread(first_commit, settings, spec)
    creator = ctx.services.get("discord_channels") or channel_creator(settings)
    channels = await creator.ensure(spec["project_id"], spec["display_name"], spec["purpose"])
    await asyncio.to_thread(add_routing, gateway_config_path(settings), spec["project_id"], channels)
    await ctx.kernel.ensure_project(spec["project_id"])
    return {"outcome": "created", "project_id": spec["project_id"], "channels": channels, "files": files,
            "guild_id": getattr(creator, "guild_id", None)}
