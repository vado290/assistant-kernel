"""Discord setup for the dashboard (ROUND6): check a bot token and read what Kernel needs from Discord.

Creating the application and its bot is the user's step in the Discord Developer Portal (Discord has no API for it).
With the bot token Kernel reads the bot, its application (id, owner, intent flags) and the servers it was added to,
builds the invite link, and can switch on the Message Content intent for bots in fewer than 100 servers (Discord
allows only the "limited" intent flags to be changed through the API).
"""
from __future__ import annotations

from typing import Any

import httpx

DISCORD_API = "https://discord.com/api/v10"
PORTAL = "https://discord.com/developers/applications"
MESSAGE_CONTENT = 1 << 18          # application flag GATEWAY_MESSAGE_CONTENT (verified bots)
MESSAGE_CONTENT_LIMITED = 1 << 19  # GATEWAY_MESSAGE_CONTENT_LIMITED (bots in fewer than 100 servers)
# View Channels, Send Messages, Manage Channels (project channels), Embed Links, Attach Files, Read Message History
PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 4) | (1 << 14) | (1 << 15) | (1 << 16)


class DiscordSetupError(Exception):
    """A Discord call failed; ``code`` is a short reason the dashboard translates."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def invite_url(application_id: str) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={application_id}"
            f"&scope=bot%20applications.commands&permissions={PERMISSIONS}")


def mask(token: str | None) -> str:
    return f"…{token[-4:]}" if token else ""


def _check(response: httpx.Response) -> Any:
    if response.status_code == 401:
        raise DiscordSetupError("invalid_token")
    if response.status_code >= 400:
        try:
            message = str(response.json().get("message") or "")
        except ValueError:
            message = response.text[:200]
        raise DiscordSetupError("discord_error", f"{response.status_code} {message}".strip())
    return response.json()


TRANSPORT: httpx.AsyncBaseTransport | None = None   # tests replace the network


def _client(token: str, base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, headers={"Authorization": f"Bot {token}"}, timeout=20,
                             transport=TRANSPORT)


async def inspect(token: str, base_url: str = DISCORD_API) -> dict[str, Any]:
    """What the dashboard shows after "check": bot, application, owner, intent, invite link and servers."""
    token = token.strip()
    if not token:
        raise DiscordSetupError("invalid_token")
    try:
        async with _client(token, base_url) as client:
            bot = _check(await client.get("/users/@me"))
            application = _check(await client.get("/applications/@me"))
            guilds = _check(await client.get("/users/@me/guilds"))
    except httpx.HTTPError as exc:
        raise DiscordSetupError("unreachable", type(exc).__name__) from exc
    flags = int(application.get("flags") or 0)
    team = application.get("team") or {}
    owner = (str(team.get("owner_user_id")) if team else str((application.get("owner") or {}).get("id") or ""))
    return {
        "bot": {"id": str(bot["id"]), "name": str(bot.get("username") or "")},
        "application": {"id": str(application["id"]), "name": str(application.get("name") or ""),
                        "portal_url": f"{PORTAL}/{application['id']}/bot"},
        "owner_id": owner,
        "message_content": bool(flags & (MESSAGE_CONTENT | MESSAGE_CONTENT_LIMITED)),
        "invite_url": invite_url(str(application["id"])),
        "guilds": [{"id": str(g["id"]), "name": str(g.get("name") or "")} for g in guilds],
    }


async def enable_message_content(token: str, base_url: str = DISCORD_API) -> bool:
    """Switch on GATEWAY_MESSAGE_CONTENT_LIMITED; True when the intent is on afterwards."""
    try:
        async with _client(token.strip(), base_url) as client:
            flags = int(_check(await client.get("/applications/@me")).get("flags") or 0)
            if flags & (MESSAGE_CONTENT | MESSAGE_CONTENT_LIMITED):
                return True
            updated = _check(await client.patch("/applications/@me", json={"flags": flags | MESSAGE_CONTENT_LIMITED}))
    except httpx.HTTPError as exc:
        raise DiscordSetupError("unreachable", type(exc).__name__) from exc
    return bool(int(updated.get("flags") or 0) & (MESSAGE_CONTENT | MESSAGE_CONTENT_LIMITED))
