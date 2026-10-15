"""Discord wiring for the core gateway: a thin layer over GatewayService (all logic lives there)."""
from __future__ import annotations

import asyncio
import io
import logging
import os
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

from assistant_kernel.config import KernelSettings
from assistant_kernel.core.gateway.config import GatewayConfig, load_gateway_config
from assistant_kernel.core.gateway.service import GatewayService, Reply
from assistant_kernel.core.kernel import Kernel

log = logging.getLogger(__name__)
STYLES = {"success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger,
          "primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary}
MODAL_ACTIONS = ("answer:", "revise")


class CoreButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=r"core:(?P<item>[0-9a-f-]{36}):(?P<action>[a-z_]+(?::\d+)?)"):
    """A card button that is routed by its custom_id, so it survives gateway restarts."""

    def __init__(self, item_id: str, action: str, label: str = "…", style: str = "secondary"):
        super().__init__(discord.ui.Button(label=label[:80], style=STYLES.get(style, STYLES["secondary"]),
                                           custom_id=f"core:{item_id}:{action}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["item"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_button(interaction, self.item.custom_id)


class CoreModal(discord.ui.Modal):
    def __init__(self, client: "CoreDiscordClient", spec: dict[str, Any]):
        super().__init__(title=spec["title"][:45], custom_id=spec["custom_id"][:100], timeout=1800)
        self.client_ref, self.inputs = client, {}
        for field in spec["fields"][:5]:
            text_input = discord.ui.TextInput(
                label=field["label"][:45] or "回答", required=field["required"], default=field.get("default") or None,
                placeholder=(field.get("placeholder") or None),
                style=discord.TextStyle.paragraph if field.get("paragraph") or len(field.get("long_text", "")) > 45
                else discord.TextStyle.short, max_length=4000)
            self.inputs[field["id"]] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = await self.client_ref.service.on_modal(
            channel_id=str(interaction.channel_id), user_id=str(interaction.user.id),
            interaction_id=str(interaction.id), custom_id=self.custom_id,
            values={key: str(item.value or "") for key, item in self.inputs.items()})
        await interaction.followup.send(reply.text or "已记录。", ephemeral=True)


class DiscordTransport:
    def __init__(self, client: "CoreDiscordClient", guild_id: str):
        self.client, self.guild_id = client, guild_id

    async def _channel(self, channel_id: str):
        return self.client.get_channel(int(channel_id)) or await self.client.fetch_channel(int(channel_id))

    @staticmethod
    def _view(buttons) -> discord.ui.View | None:
        if not buttons:
            return None
        view = discord.ui.View(timeout=None)
        for b in buttons:
            _, item_id, action = b["custom_id"].split(":", 2)
            view.add_item(CoreButton(item_id, action, b["label"], b["style"]))
        return view

    async def send(self, channel_id, text, buttons=(), attachment=None) -> str:
        channel = await self._channel(channel_id)
        kwargs: dict[str, Any] = {"content": text, "allowed_mentions": discord.AllowedMentions.none()}
        view = self._view(buttons)
        if view is not None:
            kwargs["view"] = view
        if attachment:
            kwargs["file"] = discord.File(io.BytesIO(attachment.encode("utf-8")), filename="card.md")
        return str((await channel.send(**kwargs)).id)

    async def edit(self, channel_id, message_id, text, buttons=()) -> None:
        channel = await self._channel(channel_id)
        await channel.get_partial_message(int(message_id)).edit(content=text, view=self._view(buttons))

    def link(self, channel_id, message_id) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{channel_id}/{message_id}"


class CoreDiscordClient(discord.Client):
    def __init__(self, kernel: Kernel, config: GatewayConfig, config_path: Path | None = None):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.message_content = True
        super().__init__(intents=intents)
        self.kernel, self.config, self.config_path = kernel, config, config_path
        self.config_mtime = config_path.stat().st_mtime if config_path and config_path.is_file() else None
        self.service = GatewayService(kernel, config, DiscordTransport(self, config.guild_id))
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="status", description="当前项目的任务和卡片状态")
        async def status(interaction: discord.Interaction):
            await interaction.response.send_message(await self.service.status_text(str(interaction.channel_id)),
                                                    ephemeral=True)

        @self.tree.command(name="pending", description="列出待处理的卡片")
        async def pending(interaction: discord.Interaction):
            await interaction.response.send_message(await self.service.pending_text(str(interaction.channel_id)),
                                                    ephemeral=True)

        @self.tree.command(name="cancel", description="取消当前进行中的任务")
        @app_commands.describe(confirm="确认取消")
        async def cancel(interaction: discord.Interaction, confirm: bool):
            if not confirm:
                await interaction.response.send_message("未取消。如需取消请把 confirm 选为 True。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(
                await self.service.cancel_active(str(interaction.channel_id), str(interaction.user.id)), ephemeral=True)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(CoreButton)
        if not self.config.shared_bot:  # syncing would replace a shared bot's legacy slash commands
            guild = discord.Object(id=int(self.config.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        for project_id in self.config.projects:
            await self.kernel.ensure_project(project_id)
        self.loop.create_task(self._pump())

    async def _pump(self) -> None:
        """Deliver outbound messages and keep cards in sync; send reminders once a minute."""
        await self.wait_until_ready()
        ticks = 0
        while not self.is_closed():
            try:
                if ticks % 5 == 0:
                    await self._reload_config()
                await self.service.deliver_outbox()
                await self.service.sync_cards()
                if ticks % 30 == 0:
                    await self.service.remind()
            except Exception:
                log.exception("gateway pump error")
            ticks += 1
            await asyncio.sleep(2)

    async def _reload_config(self) -> None:
        """Pick up channel routing added by project creation (config/core-discord.yaml) without a restart."""
        if not self.config_path or not self.config_path.is_file():
            return
        mtime = self.config_path.stat().st_mtime
        if mtime == self.config_mtime:
            return
        config = load_gateway_config(self.config_path)
        for project_id in set(config.projects) - set(self.config.projects):
            await self.kernel.ensure_project(project_id)
        self.config = self.service.config = config
        self.config_mtime = mtime
        log.info("gateway routing reloaded: %s", ", ".join(sorted(config.projects)))

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or str(message.guild.id) != self.config.guild_id:
            return
        await self.service.on_message(channel_id=str(message.channel.id), user_id=str(message.author.id),
                                      message_id=str(message.id), text=message.content)

    async def handle_button(self, interaction: discord.Interaction, custom_id: str) -> None:
        action = custom_id.split(":", 2)[2]
        if action.startswith(MODAL_ACTIONS):
            reply: Reply = await self.service.on_button(
                channel_id=str(interaction.channel_id), user_id=str(interaction.user.id),
                interaction_id=str(interaction.id), custom_id=custom_id)
            if reply.modal:
                await interaction.response.send_modal(CoreModal(self, reply.modal))
            else:
                await interaction.response.send_message(reply.text or "已记录。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = await self.service.on_button(channel_id=str(interaction.channel_id), user_id=str(interaction.user.id),
                                             interaction_id=str(interaction.id), custom_id=custom_id)
        await interaction.followup.send(reply.text or "已记录。", ephemeral=True)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = KernelSettings.from_environment()
    path = Path(os.environ.get("CORE_DISCORD_CONFIG") or settings.data_root / "config" / "core-discord.yaml")
    config = load_gateway_config(path)
    token = os.environ.get(config.bot_token_env)
    if not token and config.shared_bot:
        from assistant_kernel.services.discord import load_discord_config
        token = load_discord_config(settings).get("bot_token")   # same source as the legacy gateway
    if not token:
        raise SystemExit(f"no bot token: set {config.bot_token_env} or use shared_bot")
    CoreDiscordClient(Kernel(settings), config, path).run(token, log_handler=None)


if __name__ == "__main__":
    run()
