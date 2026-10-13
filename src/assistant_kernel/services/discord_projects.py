from __future__ import annotations

from typing import Any

import httpx

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.discord_interfaces import DiscordInterfaceRegistry


PROJECT_TOPIC_PREFIX = "assistant-kernel-project:"
INTERFACE_TOPIC_PREFIX = "assistant-kernel-interface:"


def topic_has_marker(topic: str | None, marker: str) -> bool:
    """Project IDs sharing a prefix must never share a channel binding."""
    return marker in {line.strip() for line in str(topic or '').splitlines()}


class DiscordProjectProvisioner:
    """Idempotently materialize one Kernel Project as one Discord text channel."""

    def __init__(
        self, repository: CoreRepository, config: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.client = client

    @staticmethod
    def marker(project_id: str) -> str:
        return f"{PROJECT_TOPIC_PREFIX}{project_id}"

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None,
    ) -> Any:
        owned = self.client is None
        client = self.client or httpx.AsyncClient(
            base_url=str(self.config.get("api_base_url") or "https://discord.com/api/v10"),
            timeout=20,
            headers={
                "Authorization": f"Bot {self.config['bot_token']}",
                "User-Agent": "AssistantKernel/discord-project-provisioner",
            },
        )
        try:
            response = await client.request(method, path, json=json)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        finally:
            if owned:
                await client.aclose()

    async def _channel_or_none(self, channel_id: str) -> dict[str, Any] | None:
        try:
            channel = await self._request("GET", f"/channels/{channel_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        if not isinstance(channel, dict):
            raise RuntimeError("Discord returned an invalid channel object")
        return channel

    async def provision(
        self, *, project_id: str, display_name: str, purpose: str,
        guild_id: str, bound_by_user_id: str, source_message_id: str,
        project_routing_hash: str,
    ) -> dict[str, Any]:
        if not self.config.get("enabled"):
            raise RuntimeError("Discord is not enabled")
        if not self.config.get("auto_provision_projects"):
            raise RuntimeError("Discord Project auto-provisioning is disabled")
        if guild_id not in set(self.config.get("guild_ids") or set()):
            raise PermissionError("the source Discord Guild is not configured")
        if not str(self.config.get("bot_token") or ""):
            raise RuntimeError("Discord bot_token is required for channel provisioning")

        existing_binding = await self.repository.resolve_project_chat_binding(
            "discord", project_id
        )
        if existing_binding is not None:
            existing_channel_id = str(existing_binding["chat_id"])
            existing_channel = await self._channel_or_none(existing_channel_id)
            if existing_channel is not None:
                actual_guild_id = str(existing_channel.get("guild_id") or guild_id)
                if actual_guild_id != guild_id:
                    raise RuntimeError(
                        "the bound Discord channel belongs to another Guild"
                    )
                return {
                    "project_id": project_id,
                    "guild_id": guild_id,
                    "channel_id": existing_channel_id,
                    "created": False,
                    "binding_changed": False,
                    "recovered": True,
                }
            await self.repository.unbind_chat_project(
                channel="discord",
                chat_id=existing_channel_id,
                expected_project_id=project_id,
                unbound_by_user_id="system:discord-channel-reconcile",
                source_message_id=str(existing_binding["message_id"]),
                idempotency_key=(
                    f"discord-deleted-channel-unbind:{project_id}:"
                    f"{existing_channel_id}"
                ),
            )

        marker = self.marker(project_id)
        channels = await self._request("GET", f"/guilds/{guild_id}/channels")
        matches = [
            dict(item) for item in (channels or [])
            if isinstance(item, dict) and topic_has_marker(item.get("topic"), marker)
        ]
        if len(matches) > 1:
            raise RuntimeError(
                f"multiple Discord channels claim Project {project_id}; manual repair is required"
            )
        created = False
        if matches:
            channel = matches[0]
        else:
            topic = f"{display_name}: {purpose}".strip()[:900]
            topic = f"{topic}\n{marker}" if topic else marker
            payload: dict[str, Any] = {
                "name": project_id,
                "type": 0,
                "topic": topic[:1024],
            }
            category_id = str(self.config.get("project_category_id") or "").strip()
            if category_id:
                payload["parent_id"] = category_id
            channel = await self._request(
                "POST", f"/guilds/{guild_id}/channels", json=payload
            )
            created = True

        channel_id = str((channel or {}).get("id") or "")
        if not channel_id:
            raise RuntimeError("Discord did not return a channel ID")
        bound = await self.repository.bind_chat_project(
            channel="discord",
            chat_id=channel_id,
            project_id=project_id,
            agent_key=f"project:{project_id}:manager",
            bound_by_user_id=bound_by_user_id,
            source_message_id=source_message_id,
            project_routing_hash=project_routing_hash,
            idempotency_key=f"discord-project-channel:{project_id}:{channel_id}",
        )
        return {
            "project_id": project_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "created": created,
            "binding_changed": bool(bound["changed"]),
            "recovered": bool(matches),
        }

    async def provision_supervisor_interface(
        self, *, project_id: str, guild_id: str,
    ) -> dict[str, Any]:
        """Group one Project PM channel and its stateless Supervisor interface."""
        if not self.config.get("enabled") or not str(self.config.get("bot_token") or ""):
            raise RuntimeError("Discord bot configuration is unavailable")
        if guild_id not in set(self.config.get("guild_ids") or set()):
            raise PermissionError("the Discord Guild is not configured")
        binding = await self.repository.resolve_project_chat_binding("discord", project_id)
        if binding is None:
            raise RuntimeError("Project does not have a bound Discord PM channel")
        pm_channel_id = str(binding["chat_id"])
        pm_channel = await self._channel_or_none(pm_channel_id)
        if pm_channel is None or str(pm_channel.get("guild_id") or "") != guild_id:
            raise RuntimeError("bound Discord PM channel is missing or belongs to another Guild")

        channels = await self._request("GET", f"/guilds/{guild_id}/channels")
        category_name = f"PROJECT · {project_id}"[:100]
        categories = [
            dict(item) for item in (channels or [])
            if isinstance(item, dict) and int(item.get("type", -1)) == 4
            and str(item.get("name") or "").casefold() == category_name.casefold()
        ]
        if len(categories) > 1:
            raise RuntimeError("multiple Discord categories match this Project")
        category_created = not categories
        category = (
            categories[0] if categories else
            await self._request("POST", f"/guilds/{guild_id}/channels", json={
                "name": category_name, "type": 4,
            })
        )
        category_id = str((category or {}).get("id") or "")
        if not category_id:
            raise RuntimeError("Discord did not return a category ID")
        if (
            str(pm_channel.get("parent_id") or "") != category_id
            or str(pm_channel.get("name") or "") != "pm"
        ):
            await self._request(
                "PATCH", f"/channels/{pm_channel_id}",
                json={"parent_id": category_id, "name": "pm"},
            )

        marker = f"{INTERFACE_TOPIC_PREFIX}supervisor:{project_id}"
        supervisors = [
            dict(item) for item in (channels or [])
            if isinstance(item, dict) and int(item.get("type", -1)) == 0
            and topic_has_marker(item.get("topic"), marker)
        ]
        if len(supervisors) > 1:
            raise RuntimeError("multiple Discord Supervisor channels claim this Project")
        supervisor_created = not supervisors
        if supervisors:
            supervisor = supervisors[0]
            if (
                str(supervisor.get("parent_id") or "") != category_id
                or str(supervisor.get("name") or "") != "supervisor"
            ):
                supervisor = await self._request(
                    "PATCH", f"/channels/{supervisor['id']}",
                    json={"parent_id": category_id, "name": "supervisor"},
                )
        else:
            supervisor = await self._request(
                "POST", f"/guilds/{guild_id}/channels", json={
                    "name": "supervisor",
                    "type": 0,
                    "parent_id": category_id,
                    "topic": (
                        "Goal Supervisor interface. 普通消息自动进入无状态Supervisor；"
                        "也可使用 /stage、/prompt、/why。\n" + marker
                    )[:1024],
                },
            )
        supervisor_id = str((supervisor or {}).get("id") or "")
        if not supervisor_id:
            raise RuntimeError("Discord did not return a Supervisor channel ID")
        DiscordInterfaceRegistry(self.repository.settings).upsert(
            channel_id=supervisor_id, guild_id=guild_id,
            project_id=project_id, role="supervisor",
        )
        return {
            "project_id": project_id, "guild_id": guild_id,
            "category_id": category_id, "category_created": category_created,
            "pm_channel_id": pm_channel_id,
            "supervisor_channel_id": supervisor_id,
            "supervisor_created": supervisor_created,
        }
