from __future__ import annotations

import httpx

from assistant_kernel.config import KernelSettings


def main() -> None:
    settings = KernelSettings.from_environment()
    token = settings.load_provider_secrets().internal.kernel_api_token.get_secret_value()
    if not token:
        raise RuntimeError("kernel API token is not configured")
    headers = {"Authorization": f"Bearer {token}"}
    base = f"http://127.0.0.1:{settings.api_port}"
    with httpx.Client(base_url=base, headers=headers, timeout=20) as client:
        ready = client.get("/health/ready")
        ready.raise_for_status()
        butler = client.get("/v2/agents/system-butler/config")
        butler.raise_for_status()
        butler_scope = client.get("/v2/agents/system-butler/scope")
        butler_scope.raise_for_status()
        manager = client.get("/v2/agents/project:super-assistant:manager/config")
        manager.raise_for_status()
        history = client.get(
            "/v2/projects/kernel-v2-smoke/conversations",
            params={"lookback_days": 365, "limit": 5},
        )
        history.raise_for_status()
    assert ready.json()["status"] == "ready"
    assert butler.json()["config_json"]["agent_id"] == "system-butler"
    assert butler_scope.json()["policy"]["project_registry_actions"] == [
        "list", "propose", "create", "route"
    ]
    assert manager.json()["config_json"]["agent_id"] == "project:super-assistant:manager"
    assert history.json()["lookback_days"] == 365
    print(
        "AGENT_CONFIG_MCP_LIVE_OK; "
        f"history_conversations={len(history.json()['items'])}; "
        "lookback_days=365"
    )


if __name__ == "__main__":
    main()

