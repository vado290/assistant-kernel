from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from test_stage_integration import settings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.services.agent_config import AgentConfig, AgentConfigManager, default_agent_config
from assistant_kernel.storage import ProjectRuntimeManager


pytestmark = pytest.mark.skipif(
    os.getenv("KERNEL_STAGE_INTEGRATION") != "1", reason="isolated PostgreSQL opt-in"
)


def test_agent_config_rejects_protected_defaults():
    payload = default_agent_config("project:p:manager", "project_manager", "p").model_dump(mode="json")
    payload["tools"]["required"][0]["defaults"]["project_id"] = "another"
    with pytest.raises(ValueError, match="protected tool defaults"):
        AgentConfig.model_validate(payload)


def test_default_agent_configs_are_role_specific():
    butler = default_agent_config("system-butler", "butler", None)
    manager = default_agent_config("project:p:manager", "project_manager", "p")
    implementation = default_agent_config("project:p:implementation", "stage_implementation", "p")
    assert butler.skills.required[0].skill == "assistant-butler-intake"
    assert {item.tool for item in manager.tools.required} >= {
        "workspace_read_text", "search_project_history", "search_project_capabilities"
    }
    assert "workspace_write_text" in manager.tools.denied
    assert "workspace_write_text" in {item.tool for item in implementation.tools.required}


async def test_agent_config_and_history_repository(settings):
    repo = ControlPlaneRepository(settings)
    await repo.ensure_default_agent("p")
    agent_id = "project:p:manager"
    role = await repo.ensure_role_version(agent_id, "primary", "project manager")
    manager = AgentConfigManager(settings)
    resolved = await manager.resolve(
        agent_id=agent_id,
        agent_type="project_manager",
        project_id="p",
        role_version_id=str(role["role_version_id"]),
    )
    assert resolved.config.config_version == 1
    assert Path(settings.data_root / "vaults" / "p" / "agent-config.yaml").is_file()
    assert "assistant-project-manager" in resolved.required_skill_texts
    async with repo.database.connection() as connection:
        assert await (
            await connection.execute(
                "SELECT 1 FROM assistant.agent_config_versions WHERE agent_id=%s AND status='active'",
                (agent_id,),
            )
        ).fetchone()

    await repo.bind_channel("test", "user", "chat", "p", "owner")
    conversation, message, _ = await repo.ingest_message(
        frontend="test",
        user_id="user",
        channel_conversation_id="chat",
        external_message_id="history-1",
        content={"text": "这是一条项目历史测试消息"},
    )
    from assistant_kernel.repositories.history import HistoryRepository

    history = HistoryRepository(repo.database)
    found = await history.search_project_history(project_id="p", query="历史测试", lookback_days=365)
    assert found["items"][0]["message_id"] == message["message_id"]
    listed = await history.list_project_conversations(project_id="p", lookback_days=365)
    assert listed["items"][0]["conversation_id"] == conversation["conversation_id"]
    with pytest.raises(ValueError, match="between 1 and 3650"):
        await history.search_project_history(project_id="p", query="x", lookback_days=4000)


async def test_agent_config_revision_is_versioned_with_diff(settings):
    import yaml

    repo = ControlPlaneRepository(settings)
    await repo.ensure_default_agent("p")
    agent_id = "project:p:manager"
    role = await repo.ensure_role_version(agent_id, "primary", "project manager")
    manager = AgentConfigManager(settings)
    first = await manager.resolve(
        agent_id=agent_id,
        agent_type="project_manager",
        project_id="p",
        role_version_id=str(role["role_version_id"]),
    )
    projection = settings.data_root / "vaults" / "p" / "agent-config.yaml"
    candidate = yaml.safe_load(projection.read_text(encoding="utf-8"))
    for item in candidate["tools"]["required"]:
        if item["tool"] == "search_project_history":
            item["defaults"]["lookback_days"] = 730
    published = await manager.publish(
        agent_id=agent_id,
        content_yaml=yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False),
        change_reason="extend default history window",
        accepted_by="owner",
    )
    assert published["version"] == 2
    assert (settings.data_root / "vaults" / "p" / "config-versions" / "v0002" / "agent-config.yaml").is_file()
    history_files = list((settings.data_root / "vaults" / "p" / "config-history").glob("v0002__*.md"))
    assert len(history_files) == 1 and "lookback_days" in history_files[0].read_text(encoding="utf-8")
    resolved = await manager.resolve(
        agent_id=agent_id,
        agent_type="project_manager",
        project_id="p",
        role_version_id=str(role["role_version_id"]),
    )
    defaults = next(
        item.defaults for item in resolved.config.tools.required if item.tool == "search_project_history"
    )
    assert defaults["lookback_days"] == 730
    assert resolved.config_version_id == published["agent_config_version_id"]
    policy = dict(resolved.scope_policy)
    policy["denied_write_paths"] = ["agent-config.yaml", ".agents/skills/**"]
    scoped = await manager.publish_scope(
        agent_id=agent_id,
        policy=policy,
        change_reason="protect active configuration files",
        accepted_by="owner",
    )
    assert scoped["scope_version"] == 2
    current = await manager.repository.get_active(agent_id)
    assert current["policy"]["denied_write_paths"] == policy["denied_write_paths"]
    assert str(current["effective_scope_version_id"]) == scoped["agent_scope_version_id"]
    assert int(current["version"]) == 3


async def test_real_agent_stdio_mcp_is_project_bound(settings):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo = ControlPlaneRepository(settings)
    await repo.ensure_default_agent("p")
    agent_id = "project:p:manager"
    role = await repo.ensure_role_version(agent_id, "primary", "project manager")
    manager = AgentConfigManager(settings)
    resolved = await manager.resolve(
        agent_id=agent_id,
        agent_type="project_manager",
        project_id="p",
        role_version_id=str(role["role_version_id"]),
    )
    await repo.bind_channel("test", "user", "chat", "p", "owner")
    _, message, _ = await repo.ingest_message(
        frontend="test",
        user_id="user",
        channel_conversation_id="chat",
        external_message_id="mcp-history-1",
        content={"text": "MCP历史检索样例"},
    )
    job = await repo.claim_job("test", str(uuid.uuid4()), 120, job_types=["conversation_turn"])
    assert job
    invocation_id = str(uuid.uuid4())
    await manager.record_invocation(invocation_id, resolved)
    runtime = ProjectRuntimeManager(settings.data_root).ensure("p")
    (runtime.vault_root / "notes.txt").write_text("workspace MCP read sample", encoding="utf-8")
    context = {
        **resolved.tool_context,
        "invocation_id": invocation_id,
        "agent_config_version_id": resolved.config_version_id,
        "job_id": str(job["job_id"]),
        "worker_instance_id": str(job["worker_instance_id"]),
    }
    from assistant_kernel.mcp_tools.context import AgentMcpContext, ContextDocument
    forged = {**context, "agent_id": "system-butler"}
    with pytest.raises(PermissionError, match="identity"):
        await AgentMcpContext(settings, ContextDocument.model_validate(forged)).assert_live()
    forged_scope = {
        **context,
        "scope_policy": {**context["scope_policy"], "file_roots": [{"uri": "vault://p/", "access": "write"}]},
    }
    from assistant_kernel.services.agent_config import AgentConfigManager as _ConfigManager
    forged_scope["effective_scope_sha256"] = _ConfigManager._canonical_sha(forged_scope["scope_policy"])
    with pytest.raises(PermissionError, match="frozen invocation bindings"):
        await AgentMcpContext(settings, ContextDocument.model_validate(forged_scope)).assert_live()
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "POSTGRES_DB": settings.postgres_database,
        "ASSISTANT_DATA_ROOT": str(settings.data_root),
        "KERNEL_AGENT_CONTEXT_JSON": json.dumps(context, ensure_ascii=False),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "assistant_kernel.mcp_server.agent_server"],
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "workspace_read_text" in names
            assert "search_project_history" in names
            assert "workspace_write_text" not in names
            read_result = await client.call_tool("workspace_read_text", {"relative_path": "notes.txt"})
            assert not read_result.is_error
            assert "workspace MCP read sample" in read_result.content[0].text
            history_result = await client.call_tool("search_project_history", {"query": "MCP历史检索"})
            assert not history_result.is_error
            assert str(message["message_id"]) in history_result.content[0].text
    async with repo.database.connection() as connection:
        count = await (
            await connection.execute(
                "SELECT count(*) AS n FROM assistant.mcp_tool_call_audits WHERE invocation_id=%s",
                (invocation_id,),
            )
        ).fetchone()
        assert count["n"] == 2
