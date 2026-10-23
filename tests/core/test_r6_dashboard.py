"""Round 6 / R6.3: the dashboard's Discord settings and language, against a fake Discord (no network)."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant_kernel.api import dashboard
from assistant_kernel.core.gateway import setup
from assistant_kernel.core.gateway.config import load_gateway_config

TOKEN = "bot.token.abcd"


def fake_discord(state: dict) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bot {TOKEN}":
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        path = request.url.path.removeprefix("/api/v10")
        if path == "/users/@me" and request.method == "GET":
            return httpx.Response(200, json={"id": "111", "username": "kernel-bot"})
        if path == "/applications/@me" and request.method == "GET":
            return httpx.Response(200, json={"id": "222", "name": "Kernel", "flags": state["flags"],
                                             "owner": {"id": "33333"}})
        if path == "/applications/@me" and request.method == "PATCH":
            state["flags"] = json.loads(request.content)["flags"]
            return httpx.Response(200, json={"id": "222", "flags": state["flags"]})
        if path == "/users/@me/guilds":
            return httpx.Response(200, json=[{"id": "44444", "name": "My server"}])
        return httpx.Response(404, json={"message": "not found"})
    return httpx.MockTransport(handle)


@pytest.fixture
def client(tmp_path, monkeypatch):
    state = {"flags": 0}
    monkeypatch.setattr(setup, "TRANSPORT", fake_discord(state))
    monkeypatch.delenv("CORE_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("CORE_DISCORD_CONFIG", str(tmp_path / "config" / "core-discord.yaml"))
    settings = SimpleNamespace(data_root=tmp_path, service_credentials_path=tmp_path / "config" / "service-credentials.json")
    app = FastAPI()
    app.include_router(dashboard.build_router(settings))
    secret = (tmp_path / "config" / "dashboard-token").read_text("utf-8").strip()
    test = TestClient(app)
    test.headers["Authorization"] = f"Bearer {secret}"
    test.state, test.settings = state, settings
    return test


def test_token_file_and_auth(client, tmp_path):
    path = tmp_path / "config" / "dashboard-token"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/v1/texts", headers={"Authorization": ""}).status_code == 200   # public
    assert client.get("/api/v1/discord", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.put("/api/v1/discord", json={"guild_id": "44444"}, headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/v1/auth").json() == {"ok": True}


def test_page_texts_are_english(client):
    texts = client.get("/api/v1/texts").json()["texts"]
    assert texts["save_button"] == "Save" and texts["error.invalid_token"] == "Discord rejected the token."
    assert not any(key.startswith("language") for key in texts)          # no language switch


def test_inspect_intent_and_save(client, tmp_path):
    assert client.post("/api/v1/discord/inspect", json={"token": "nope"}).json()["detail"]["code"] == "invalid_token"
    assert client.post("/api/v1/discord/inspect", json={}).json()["detail"]["code"] == "no_token"
    found = client.post("/api/v1/discord/inspect", json={"token": TOKEN}).json()
    assert found["bot"] == {"id": "111", "name": "kernel-bot"} and found["owner_id"] == "33333"
    assert found["message_content"] is False and found["guilds"] == [{"id": "44444", "name": "My server"}]
    assert "client_id=222" in found["invite_url"] and f"permissions={setup.PERMISSIONS}" in found["invite_url"]
    assert client.post("/api/v1/discord/intent", json={"token": TOKEN}).json() == {"message_content": True}
    assert client.state["flags"] == setup.MESSAGE_CONTENT_LIMITED

    bad = client.put("/api/v1/discord", json={"token": TOKEN, "guild_id": "99999", "allowed_user_ids": ["33333"]})
    assert bad.json()["detail"]["code"] == "bot_not_in_guild"
    config_path = tmp_path / "config" / "core-discord.yaml"
    config_path.write_text(yaml.safe_dump({"guild_id": "1", "shared_bot": True,
                                           "projects": {"kernel": {"pm_channel_id": "5", "supervisor_channel_id": "6"}}}))
    saved = client.put("/api/v1/discord", json={"token": TOKEN, "guild_id": "44444", "allowed_user_ids": ["33333"]})
    assert saved.json()["saved"] is True and saved.json()["env_overrides"] is False
    config = load_gateway_config(config_path)
    assert config.guild_id == "44444" and config.allowed_user_ids == {"33333"} and config.shared_bot
    assert list(config.projects) == ["kernel"]                                   # channels kept
    credentials = json.loads(client.settings.service_credentials_path.read_text("utf-8"))
    assert credentials["services"]["discord"]["bot_token"] == TOKEN
    state = client.get("/api/v1/discord").json()
    assert state["token"] == {"saved": "…abcd", "source": "file", "file": "…abcd"}
    assert TOKEN not in json.dumps(state)                                        # never sent back
    # the saved token is used when none is given
    assert client.post("/api/v1/discord/inspect", json={}).json()["application"]["id"] == "222"


def test_environment_token_wins(client, monkeypatch):
    monkeypatch.setenv("CORE_DISCORD_BOT_TOKEN", "other.token.zzzz")
    saved = client.put("/api/v1/discord", json={"token": TOKEN, "guild_id": "44444", "allowed_user_ids": []})
    assert saved.status_code == 200 and saved.json()["env_overrides"] is True


def test_gateway_config_without_file(tmp_path):
    config = load_gateway_config(tmp_path / "missing.yaml")
    assert config.guild_id == "" and config.projects == {}
