"""The dashboard (ROUND6): a local web page served by kernel-api, in English; its first page is the Discord settings.

The page itself and its interface text are public; every other `/api/v1` call needs the dashboard token from
`<data_root>/config/dashboard-token` (created on first start, mode 600) as `Authorization: Bearer <token>`.
The host publishes kernel-api on 127.0.0.1 only. The bot token is written to the service credentials file and is
never sent back, only its last four characters.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from assistant_kernel.core import i18n
from assistant_kernel.core.gateway import setup
from assistant_kernel.core.gateway.config import (bot_token, load_gateway_config, save_bot_token,
                                                  save_gateway_settings, saved_bot_token)

PAGE = Path(__file__).with_name("static") / "dashboard.html"
TOKEN_FILE = Path("config") / "dashboard-token"
DISCORD_ID = re.compile(r"^\d{5,25}$")


def token_path(settings) -> Path:
    return Path(settings.data_root) / TOKEN_FILE


def ensure_token(settings) -> str:
    """The dashboard token, created on first use."""
    path = token_path(settings)
    if path.is_file() and path.read_text("utf-8").strip():
        return path.read_text("utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(24)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value + "\n", "utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return value


class TokenIn(BaseModel):
    token: str | None = None


class DiscordIn(BaseModel):
    token: str | None = None
    guild_id: str
    allowed_user_ids: list[str] = Field(default_factory=list)


def gateway_config_path(settings) -> Path:
    return Path(os.environ.get("CORE_DISCORD_CONFIG") or Path(settings.data_root) / "config" / "core-discord.yaml")


def token_state(settings, config) -> dict[str, Any]:
    """Which token the gateway uses and where it comes from (an environment variable wins over the saved one)."""
    from_env = os.environ.get(config.bot_token_env) or (config.shared_bot and os.environ.get("DISCORD_BOT_TOKEN"))
    current = bot_token(settings, config, shared_fallback=config.shared_bot)
    return {"saved": setup.mask(current), "source": "env" if from_env else ("file" if current else None),
            "file": setup.mask(saved_bot_token(settings))}


def build_router(settings) -> APIRouter:
    router = APIRouter()
    expected = ensure_token(settings)

    def authorized(authorization: str = Header(default="")) -> None:
        given = authorization.removeprefix("Bearer ").strip()
        if not given or not hmac.compare_digest(given, expected):
            raise HTTPException(status_code=401, detail="dashboard_token")

    def failure(exc: setup.DiscordSetupError) -> HTTPException:
        return HTTPException(status_code=400, detail={"code": exc.code, "detail": exc.detail})

    def current_token(given: str | None) -> str:
        token = (given or "").strip() or bot_token(settings, load_gateway_config(gateway_config_path(settings)),
                                                   shared_fallback=True)
        if not token:
            raise HTTPException(status_code=400, detail={"code": "no_token", "detail": ""})
        return token

    @router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def page() -> str:
        return PAGE.read_text("utf-8")

    @router.get("/api/v1/texts")
    async def texts() -> dict[str, Any]:
        """The page's interface text (public; English)."""
        return {"texts": {k[len("dashboard."):]: v for k, v in i18n.pack().items() if k.startswith("dashboard.")}}

    @router.get("/api/v1/auth", dependencies=[Depends(authorized)])
    async def check_auth() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/api/v1/discord", dependencies=[Depends(authorized)])
    async def get_discord() -> dict[str, Any]:
        config = load_gateway_config(gateway_config_path(settings))
        return {"guild_id": config.guild_id, "allowed_user_ids": sorted(config.allowed_user_ids),
                "shared_bot": config.shared_bot, "token": token_state(settings, config),
                "projects": [{"project_id": p.project_id, "pm_channel_id": p.pm_channel_id,
                              "supervisor_channel_id": p.supervisor_channel_id}
                             for p in sorted(config.projects.values(), key=lambda p: p.project_id)]}

    @router.post("/api/v1/discord/inspect", dependencies=[Depends(authorized)])
    async def inspect(body: TokenIn) -> dict[str, Any]:
        try:
            return await setup.inspect(current_token(body.token))
        except setup.DiscordSetupError as exc:
            raise failure(exc) from exc

    @router.post("/api/v1/discord/intent", dependencies=[Depends(authorized)])
    async def intent(body: TokenIn) -> dict[str, bool]:
        try:
            return {"message_content": await setup.enable_message_content(current_token(body.token))}
        except setup.DiscordSetupError as exc:
            raise failure(exc) from exc

    @router.put("/api/v1/discord", dependencies=[Depends(authorized)])
    async def put_discord(body: DiscordIn) -> dict[str, Any]:
        users = [u.strip() for u in body.allowed_user_ids if u.strip()]
        if not DISCORD_ID.match(body.guild_id.strip()) or not all(DISCORD_ID.match(u) for u in users):
            raise HTTPException(status_code=400, detail={"code": "bad_id", "detail": ""})
        token = current_token(body.token)
        try:
            found = await setup.inspect(token)
        except setup.DiscordSetupError as exc:
            raise failure(exc) from exc
        if body.guild_id.strip() not in {g["id"] for g in found["guilds"]}:
            raise HTTPException(status_code=400, detail={"code": "bot_not_in_guild", "detail": ""})
        if (body.token or "").strip():
            save_bot_token(settings, token)
        path = gateway_config_path(settings)
        save_gateway_settings(path, guild_id=body.guild_id.strip(), allowed_user_ids=users)
        config = load_gateway_config(path)
        return {"saved": True, "token": token_state(settings, config),
                "env_overrides": bool((body.token or "").strip()) and token_state(settings, config)["source"] == "env"}

    return router
