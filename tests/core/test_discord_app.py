"""Offline checks of the Discord layer: buttons route by custom_id, modals carry pre-filled answers."""
from __future__ import annotations

import re

from assistant_kernel.core.gateway.config import GatewayConfig, ProjectChannels
from assistant_kernel.core.gateway.discord_app import CoreButton, CoreDiscordClient, CoreModal, DiscordTransport

ITEM = "0f8c1b2e-1111-4222-8333-944455556666"


async def test_buttons_and_modals_build_offline(kernel):
    client = CoreDiscordClient(kernel, GatewayConfig("1", frozenset(), {"p1": ProjectChannels("p1", "2", "3")}))
    view = DiscordTransport._view([{"custom_id": f"core:{ITEM}:execute", "label": "执行", "style": "success"},
                                   {"custom_id": f"core:{ITEM}:answer:1", "label": "回答 6-7", "style": "secondary"}])
    custom_ids = [child.custom_id for child in view.children]
    assert custom_ids == [f"core:{ITEM}:execute", f"core:{ITEM}:answer:1"]
    assert all(re.fullmatch(CoreButton.__discord_ui_compiled_template__, c) for c in custom_ids)
    modal = CoreModal(client, {"custom_id": f"coremodal:{ITEM}:answer:0", "title": "回答问题",
                               "fields": [{"id": "q1", "label": "给谁看？", "required": True, "default": "团队",
                                           "placeholder": "可选：个人 / 团队", "long_text": "给谁看？"}]})
    assert modal.inputs["q1"].default == "团队"
    assert DiscordTransport(client, "1").link("2", "9") == "https://discord.com/channels/1/2/9"
    await client.close()
