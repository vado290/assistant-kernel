from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from assistant_kernel.config import KernelSettings
from assistant_kernel.harness.base import AgentTurnRequest
from assistant_kernel.harness.codex_cli import CodexCliHarness
from assistant_kernel.services.agent_files import AgentFileLoader
from assistant_kernel.services.execution_plan import ProjectResourceResolver
from assistant_kernel.services.feishu import parse_slash_command
from assistant_kernel.services.run_processor import _has_successful_mcp_tool_call
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.services.channel_health import (
    WorkerHealthReporter, read_worker_health,
)
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.worker.main import _notify_terminal_failure


def settings_for(tmp_path: Path) -> KernelSettings:
    return KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path,
        "provider_secrets_path": tmp_path / "providers.json",
    })


def test_legacy_scope_reader_retains_old_semantics_but_current_scope_is_shared(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    ProjectRuntimeManager(tmp_path).ensure("demo")
    roots = WorkspaceScopeService(settings).resolve(
        "demo", "run-1", "implementation", candidate_only=True, shared_agreement=False,
    )
    by_key = {item.key: item for item in roots}
    assert by_key["project"].access == "read"
    assert by_key["project"].write_mode == "gated"
    assert by_key["candidate"].access == "write"
    assert by_key["run_work"].access == "write"
    current = WorkspaceScopeService(settings).resolve('demo', 'run-2', 'requirements', candidate_only=True)
    assert next(item for item in current if item.key == 'project').access == 'write'
    assert all(item.native_mode == 'behavioral' for item in current)


def test_worker_health_reports_fresh_and_stale_instances(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    reporter = WorkerHealthReporter(settings, "kernel-run-worker-00")
    reporter.update(last_process_heartbeat_at="2026-10-01T00:00:00+00:00")
    current = read_worker_health(settings)
    assert current["fresh_count"] == 1
    assert current["workers"][0]["worker_id"] == "kernel-run-worker-00"

    path = settings.data_root / "runtime" / "worker-health" / "kernel-run-worker-00.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    stale = read_worker_health(settings)
    assert stale["fresh_count"] == 0
    assert stale["workers"][0]["stale"] is True


def write_v2_project(tmp_path: Path, *, with_mcp: bool = False) -> tuple[KernelSettings, Path]:
    settings = settings_for(tmp_path)
    root = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    (root / "project.yaml").write_text(
        yaml.safe_dump({
            "schema_version": "project.v2",
            "project_id": "demo",
            "layout_version": 2,
            "status": "active",
            "default_agent": "pm",
        }),
        encoding="utf-8",
    )
    agent_root = root / "agents" / "analyst"
    agent_root.mkdir(parents=True)
    config = {
        "schema_version": "agent-config.v2",
        "agent_id": "analyst",
        "agent_key": "project:demo:analyst",
        "kind": "business",
        "status": "active",
        "display_name": "Analyst",
        "aliases": ["analysis"],
        "role": "role.md",
        "skills": [],
        "scope_profile": "current-project",
        "mcp_servers": (
            {"local-tools": {"enabled_tools": ["echo_value"]}}
            if with_mcp else {}
        ),
        "entrypoints": {"pm_handoff": True, "feishu_slash": True},
        "triggers": ["user_message"],
        "harness": {
            "provider": "openai",
            "model": "gpt-test",
            "reasoning_effort": "medium",
            "sandbox": "read-only",
            "timeout_seconds": 60,
        },
    }
    (agent_root / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    (agent_root / "role.md").write_text("# Analyst\n", encoding="utf-8")
    if with_mcp:
        server_root = root / "mcp" / "local-tools"
        server_root.mkdir(parents=True)
        (server_root / "mcp-manifest.yaml").write_text(
            yaml.safe_dump({
                "server_id": "local-tools",
                "transport": "stdio",
                "lifecycle": "per_run",
                "entrypoint": "server.py",
                "tools": [{"name": "echo_value", "effect": "read"}],
            }, sort_keys=False),
            encoding="utf-8",
        )
        (server_root / "server.py").write_text("print('server')\n", encoding="utf-8")
    return settings, root


def test_v2_agent_registry_discovers_alias_and_loads_config(tmp_path: Path) -> None:
    settings, _ = write_v2_project(tmp_path)
    loader = AgentFileLoader(settings)

    agents = loader.list_agents("demo")
    selected = loader.resolve_agent("demo", "analysis", require_feishu_slash=True)
    config = loader.load("demo", selected.agent_key)

    assert len(agents) == 1
    assert selected.agent_id == "analyst"
    assert config.agent_key == "project:demo:analyst"
    assert config.display_name == "Analyst"
    assert config.entrypoints["feishu_slash"] is True


def test_project_mcp_external_effect_requires_a_real_action_grant(tmp_path: Path) -> None:
    settings, root = write_v2_project(tmp_path, with_mcp=True)
    manifest_path = root / "mcp" / "local-tools" / "mcp-manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["tools"][0]["effect"] = "external"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    config = AgentFileLoader(settings).load("demo", "project:demo:analyst")
    with pytest.raises(PermissionError, match="action grant is not implemented"):
        ProjectResourceResolver(settings).resolve_mcp_servers("demo", config)


def test_feishu_slash_parser_is_small_and_deterministic() -> None:
    assert parse_slash_command("/agents") == ("/agents", None)
    assert parse_slash_command(" /agent analysis ") == ("/agent", "analysis")
    assert parse_slash_command("/pm@assistant") == ("/pm", None)
    assert parse_slash_command("/butler") == ("/butler", None)
    assert parse_slash_command("@_user_1 /bind_project demo") == (
        "/bind_project", "demo"
    )
    assert parse_slash_command("/bind-project demo") == ("/bind_project", "demo")
    assert parse_slash_command("/unbind_project demo") == (
        "/unbind_project", "demo"
    )
    assert parse_slash_command("/binding") == ("/binding", None)
    assert parse_slash_command("/unknown value") is None
    assert parse_slash_command("normal message") is None


def test_handshake_accepts_a_successful_retry_but_not_only_failures() -> None:
    failed = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "tool": "get_authorized_workspace",
            "status": "failed",
            "error": None,
        },
    }
    succeeded = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "tool": "get_authorized_workspace",
            "status": "completed",
            "error": None,
        },
    }

    assert _has_successful_mcp_tool_call(
        [failed, succeeded], "get_authorized_workspace"
    ) is True
    assert _has_successful_mcp_tool_call(
        [failed], "get_authorized_workspace"
    ) is False
    assert _has_successful_mcp_tool_call(
        [succeeded], "get_stage_handoff"
    ) is False


@pytest.mark.parametrize("channel", ["feishu", "discord"])
async def test_channel_dead_letter_notice_is_safe_and_idempotently_keyed(channel) -> None:
    class Repository:
        def __init__(self):
            self.created = []

        async def get_message(self, message_id: str):
            return {
                "message_id": message_id,
                "channel": channel,
                "chat_id": "chat-1",
                "user_id": "user-1",
            }

        async def create_outbound(self, **kwargs):
            self.created.append(kwargs)

    repository = Repository()
    await _notify_terminal_failure(repository, {
        "run_id": "run-1",
        "project_id": "system",
        "session_id": "session-1",
        "trigger_message_id": "message-1",
    })

    assert len(repository.created) == 1
    notice = repository.created[0]
    assert notice["idempotency_key"] == "dead-letter-notice:run-1"
    assert notice["reply_to_message_id"] == "message-1"
    assert notice["channel"] == channel
    assert notice["content"]["terminal_status"] == "dead_letter"
    assert "Run ID：run-1" in notice["content"]["text"]
    assert "required workspace scope" not in notice["content"]["text"]




def test_v2_layout_does_not_fall_back_to_legacy_agent(tmp_path: Path) -> None:
    settings, root = write_v2_project(tmp_path)
    (root / "agent-config.yaml").write_text(
        "agent_key: project:demo:manager\n", encoding="utf-8"
    )
    with pytest.raises(FileNotFoundError, match="published Agent"):
        AgentFileLoader(settings).load("demo", "project:demo:manager")


def test_execution_plan_snapshots_project_stdio_mcp(tmp_path: Path) -> None:
    settings, root = write_v2_project(tmp_path, with_mcp=True)
    scopes = settings.data_root / "config" / "project-scopes.yaml"
    scopes.parent.mkdir(parents=True, exist_ok=True)
    scopes.write_text(yaml.safe_dump({
        "schema_version": "workspace-scope.v1",
        "projects": {"default": {"roots": [{
            "key": "project", "path": "{project_root}",
            "description": "Project Vault",
        }] }},
    }, sort_keys=False), encoding="utf-8")
    loader = AgentFileLoader(settings)
    config = loader.load("demo", "project:demo:analyst")
    snapshot_uri, snapshot_sha = loader.snapshot("demo", config)
    resolver = ProjectResourceResolver(settings)
    run = {
        "run_id": "run-1",
        "project_id": "demo",
        "agent_key": config.agent_key,
        "trigger_message_id": "message-1",
        "idempotency_key": "run:1",
        "request_json": {},
        "max_attempts": 3,
    }
    session = {
        "logical_session_id": "logical-1",
        "native_session_id": None,
    }

    plan, harness, uri, file_sha = resolver.materialize(
        run=run,
        config=config,
        config_snapshot_uri=snapshot_uri,
        config_snapshot_sha256=snapshot_sha,
        provider="openai",
        model="gpt-test",
        reasoning_effort="medium",
        timeout_seconds=60,
        session=session,
        model_context_window_tokens=1_000_000,
        model_auto_compact_token_limit=900_000,
    )
    loaded, loaded_harness = resolver.load_materialized("demo", uri, file_sha)
    repeated, repeated_harness, repeated_uri, repeated_sha = resolver.materialize(
        run=run,
        config=config,
        config_snapshot_uri=snapshot_uri,
        config_snapshot_sha256=snapshot_sha,
        provider="openai",
        model="gpt-test",
        reasoning_effort="medium",
        timeout_seconds=60,
        session=session,
        model_context_window_tokens=1_000_000,
        model_auto_compact_token_limit=900_000,
    )

    server = plan["bindings"]["mcp_servers"][0]
    context_view = plan["bindings"]["context_view"]
    scope_uri = plan["bindings"]["workspace_scope_snapshot_uri"]
    scope_sha = plan["bindings"]["workspace_scope_snapshot_sha256"]
    assert server["transport"] == "stdio"
    assert context_view["schema_version"] == "context-view.v1"
    assert {item["key"] for item in context_view["roots"]} == {"project", "run_work"}
    assert server["snapshot_uri"].startswith("vault://demo/runtime/mcp-snapshots/")
    assert (root / "runtime" / "mcp-snapshots").is_dir()
    assert scope_uri == "vault://demo/runs/run-1/resolved/workspace-scope.json"
    assert [
        item.key for item in WorkspaceScopeService(settings).load_snapshot(
            "demo", "run-1", None, scope_uri, scope_sha,
        )
    ] == ["project", "run_work"]
    assert harness[0]["command"]
    assert loaded["plan_sha256"] == plan["plan_sha256"]
    assert loaded["runtime"]["model_context_window_tokens"] == 1_000_000
    assert loaded["runtime"]["model_auto_compact_token_limit"] == 900_000
    assert loaded_harness[0]["enabled_tools"] == ["echo_value"]
    assert repeated["plan_sha256"] == plan["plan_sha256"]
    assert repeated_harness[0]["enabled_tools"] == ["echo_value"]
    assert (repeated_uri, repeated_sha) == (uri, file_sha)


def test_candidate_submission_does_not_reduce_the_project_file_agreement(tmp_path: Path) -> None:
    settings, _ = write_v2_project(tmp_path)
    scopes = settings.data_root / "config" / "project-scopes.yaml"
    scopes.parent.mkdir(parents=True, exist_ok=True)
    scopes.write_text(yaml.safe_dump({
        "schema_version": "workspace-scope.v1",
        "projects": {"default": {"roots": [{
            "key": "project", "path": "{project_root}",
            "description": "Project Vault",
        }]}},
    }, sort_keys=False), encoding="utf-8")
    loader = AgentFileLoader(settings)
    config = loader.load("demo", "project:demo:analyst")
    snapshot_uri, snapshot_sha = loader.snapshot("demo", config)
    plan, _, _, _ = ProjectResourceResolver(settings).materialize(
        run={
            "run_id": "child-run-1", "project_id": "demo",
            "agent_key": config.agent_key, "trigger_message_id": "message-1",
            "idempotency_key": "child:1", "max_attempts": 3,
            "request_json": {
                "candidate_only": True,
                "task_protocol": "single-agent.v2",
                "permission_profile_name": "candidate-write",
            },
        },
        config=config, config_snapshot_uri=snapshot_uri,
        config_snapshot_sha256=snapshot_sha, provider="openai",
        model="gpt-test", reasoning_effort="medium", timeout_seconds=60,
        session={"logical_session_id": "logical-child", "native_session_id": None},
    )
    roots = {
        item.key: item for item in WorkspaceScopeService(settings).load_snapshot(
            "demo", "child-run-1", None,
            plan["bindings"]["workspace_scope_snapshot_uri"],
            plan["bindings"]["workspace_scope_snapshot_sha256"],
        )
    }
    assert roots["project"].access == "write"
    assert roots["candidate"].access == "write"
    assert plan["runtime"]["candidate_only"] is True
    assert plan['bindings']['stage_permissions']['formal_write'] is False
    assert plan['bindings']['stage_permissions']['business_approval_required'] is True


def test_model_or_context_change_changes_session_compatibility(tmp_path: Path) -> None:
    settings, _ = write_v2_project(tmp_path)
    loader = AgentFileLoader(settings)
    config = loader.load("demo", "project:demo:analyst")
    resolver = ProjectResourceResolver(settings)

    old = resolver.compatibility_hash(
        config,
        provider="openai",
        model="gpt-5.1",
        reasoning_effort="medium",
        model_context_window_tokens=272_000,
        model_auto_compact_token_limit=244_800,
    )
    upgraded = resolver.compatibility_hash(
        config,
        provider="openai",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
        model_context_window_tokens=1_000_000,
        model_auto_compact_token_limit=900_000,
    )

    assert upgraded != old
    assert resolver.compatibility_hash(
        config, scope_compatibility_hash="scope-a",
    ) != resolver.compatibility_hash(
        config, scope_compatibility_hash="scope-b",
    )


async def test_codex_harness_injects_stdio_and_http_mcp(tmp_path: Path, monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv("SHARED_MCP_TOKEN", "synthetic")

    async def fake_process(*command, **kwargs):
        captured["command"] = command
        return 0, (
            b'{"type":"thread.started","thread_id":"thread-agent"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        ), b"", 1

    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    harness = CodexCliHarness(ProjectRuntimeManager(tmp_path), codex_binary="codex")
    await harness.run_turn(AgentTurnRequest(
        project_id="demo",
        prompt="test",
        cwd=tmp_path,
        mcp_servers=(
            {
                "server_id": "local-tools",
                "transport": "stdio",
                "command": "python",
                "args": ["server.py"],
                "cwd": str(tmp_path),
                "env": {"SERVER_ID": "local-tools"},
                "enabled_tools": ["echo_value"],
                "required": True,
            },
            {
                "server_id": "shared-tools",
                "transport": "streamable-http",
                "url": "http://127.0.0.1:19000/mcp",
                "bearer_token_env_var": "SHARED_MCP_TOKEN",
                "enabled_tools": ["shared_read"],
                "required": False,
            },
        ),
    ))

    command = " ".join(captured["command"])
    assert "assistant_kernel.mcp_server.managed" in command
    assert "mcp_servers.kernel_scoped.enabled_tools" in command
    assert "SHARED_MCP_TOKEN" not in command
    request = json.loads(
        (tmp_path / "vaults" / "demo" / "runs" / "agent-traces" / "invocations")
        .glob("*/request.json").__next__().read_text(encoding="utf-8")
    )
    assert request["mcp_servers"][0]["transport"] == "stdio"


async def test_project_stdio_launcher_completes_real_mcp_lifecycle(tmp_path: Path) -> None:
    server_root = tmp_path / "server"
    server_root.mkdir()
    (server_root / "server.py").write_text(
        """from mcp.server.mcpserver import MCPServer

server = MCPServer(name="runtime-v2-test")

@server.tool()
def echo_value(value: str) -> dict:
    return {"value": value}

server.run(transport="stdio")
""",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "KERNEL_PROJECT_MCP_ROOT": str(server_root),
        "KERNEL_PROJECT_MCP_ENTRYPOINT": "server.py",
        "KERNEL_PROJECT_MCP_SERVER_ID": "runtime-v2-test",
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "assistant_kernel.mcp_server.project_launcher"],
        env=environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("echo_value", {"value": "ok"})

    assert [tool.name for tool in tools.tools] == ["echo_value"]
    assert result.is_error is False
    assert json.loads(result.content[0].text) == {"value": "ok"}


@pytest.fixture(autouse=True)
def isolated_codex_config_query(monkeypatch):
    async def configured(*args,**kwargs):return []
    monkeypatch.setattr('assistant_kernel.harness.mcp_config.configured_servers',configured)
    from assistant_kernel.harness.mcp_config import compile_mcp
    async def compile_fixture(request,environment,binary,archive_dir):
        result=await compile_mcp(request,environment,binary,archive_dir)
        if result[1]:
            payload=json.loads(result[1].read_text())
            (archive_dir/'mcp-runtime.fixture.json').write_text(json.dumps({'exposed':payload['selected'],'cleanup':'closed','test_fake':True}))
        return result
    monkeypatch.setattr('assistant_kernel.harness.mcp_config.compile_mcp',compile_fixture)
