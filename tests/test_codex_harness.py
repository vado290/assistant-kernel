from assistant_kernel.harness.codex_cli import CodexCliHarness
from assistant_kernel.harness.base import AgentTurnRequest
from assistant_kernel.storage import ProjectRuntimeManager
from pathlib import Path
import json
import pytest


def test_parse_codex_jsonl() -> None:
    result = CodexCliHarness.parse_jsonl(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
                '{"type":"turn.completed","usage":{"input_tokens":51,"cached_input_tokens":20}}',
            ]
        )
    )
    assert result.physical_session_id == "thread-1"
    assert result.final_text == "ok"
    assert result.usage.input_tokens == 51


def test_codex_harness_reports_chatgpt_subscription_auth(tmp_path) -> None:
    runtime = ProjectRuntimeManager(tmp_path)
    home = runtime.ensure("p").codex_home
    (home / "auth.json").write_text(
        '{"auth_mode":"chatgpt","tokens":{"access_token":"not-a-real-token"}}',
        encoding="utf-8",
    )
    assert CodexCliHarness(runtime).authentication_mode("p") == "chatgpt_subscription"


@pytest.mark.parametrize('end',['', '{"type":"turn.failed"}', '{"type":"error"}'])
def test_partial_or_failed_cli_is_not_a_completed_stage(end):
    with pytest.raises(ValueError):
        CodexCliHarness.parse_jsonl('{"type":"thread.started","thread_id":"id"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}\n'+end)


def test_transient_error_before_completed_turn_is_not_replayed() -> None:
    result = CodexCliHarness.parse_jsonl(
        '{"type":"thread.started","thread_id":"id"}\n'
        '{"type":"error","message":"Reconnecting"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
    )
    assert result.final_text == "done"


async def test_prompt_is_archived_without_legacy_database_mcp(tmp_path, monkeypatch):
    captured = {}

    async def fake_process(*command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        if kwargs["env"].get("KERNEL_MCP_CONFIG_PATH"):
            captured["mcp"] = json.loads(Path(kwargs["env"]["KERNEL_MCP_CONFIG_PATH"]).read_text())
        output = (
            '{"type":"thread.started","thread_id":"thread-agent"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        ).encode()
        return 0, output, b"", None

    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    harness = CodexCliHarness(ProjectRuntimeManager(tmp_path), codex_binary="codex")
    invocation_id = "00000000-0000-0000-0000-000000000001"
    await harness.run_turn(
        AgentTurnRequest(
            project_id="p",
            prompt="test",
            cwd=tmp_path,
            invocation_id=invocation_id,
            model_context_window_tokens=1_000_000,
            model_auto_compact_token_limit=900_000,
            ephemeral=True,
            extra_environment={"PROJECT_API_KEY": "project-secret"},
        )
    )
    command = " ".join(captured["command"])
    assert "kernel_agent" not in command
    assert "kernel_stage" not in command
    assert "POSTGRES_PASSWORD" not in command
    assert "model_context_window=1000000" in command
    assert "model_auto_compact_token_limit=900000" in command
    assert "--ephemeral" in captured["command"]
    assert captured["env"]["PROJECT_API_KEY"] == "project-secret"
    prompt = tmp_path / "vaults" / "p" / "runs" / "agent-traces" / "invocations" / invocation_id / "prompt.md"
    assert prompt.read_text(encoding="utf-8") == "test"
    archived_request = json.loads(
        (prompt.parent / "request.json").read_text(encoding="utf-8")
    )
    assert archived_request["model_context_window_tokens"] == 1_000_000
    assert archived_request["model_auto_compact_token_limit"] == 900_000


async def test_native_last_usage_drives_context_budget_not_turn_total(tmp_path, monkeypatch):
    captured = {}

    async def fake_process(*command, **kwargs):
        captured["command"] = command
        native = (
            tmp_path / "vaults" / "p" / "runtime" / "codex-home" / "sessions" /
            "2026" / "09" / "10" / "rollout-thread-native.jsonl"
        )
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text(json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 2_007_191},
                    "last_token_usage": {
                        "input_tokens": 28_555,
                        "cached_input_tokens": 24_704,
                        "output_tokens": 1_082,
                        "reasoning_output_tokens": 462,
                    },
                    "model_context_window": 258_400,
                },
            },
        }) + "\n", encoding="utf-8")
        output = (
            '{"type":"thread.started","thread_id":"thread-native"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":2007191}}\n'
        ).encode()
        return 0, output, b"", None

    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    result = await CodexCliHarness(
        ProjectRuntimeManager(tmp_path), codex_binary="codex"
    ).run_turn(AgentTurnRequest(project_id="p", prompt="test", cwd=Path(tmp_path)))
    assert result.usage.input_tokens == 28_555
    assert result.metadata["context_window_tokens"] == 258_400
    assert result.metadata["usage_source"] == "native_last_token_usage"
    assert result.metadata["total_usage"]["input_tokens"] == 2_007_191
    assert "features.goals=false" in captured["command"]


async def test_scoped_mcp_is_injected_without_scope_values_in_command(tmp_path, monkeypatch):
    captured = {}
    external = tmp_path / "external"
    external.mkdir()

    async def fake_process(*command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        if kwargs["env"].get("KERNEL_MCP_CONFIG_PATH"):
            captured["mcp"] = json.loads(Path(kwargs["env"]["KERNEL_MCP_CONFIG_PATH"]).read_text())
        return 0, (
            b'{"type":"thread.started","thread_id":"thread-agent"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        ), b"", 1

    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    harness = CodexCliHarness(ProjectRuntimeManager(tmp_path), codex_binary="codex")
    await harness.run_turn(AgentTurnRequest(
        project_id="p", prompt="test", cwd=tmp_path,
        sandbox="workspace-write",
        scoped_tools=("vault_list",),
        scope_context={
            "KERNEL_SCOPE_PROJECT_ID": "p",
            "KERNEL_SCOPE_RUN_ID": "run-secret",
            "KERNEL_SCOPE_CLAIM_TOKEN": "claim-secret",
            "KERNEL_SCOPE_WORKER_INSTANCE_ID": "worker-secret",
            "KERNEL_SCOPE_ALLOWED_TOOLS": "vault_list",
        },
        writable_roots=(external,),
    ))
    command = " ".join(captured["command"])
    assert "assistant_kernel.core.tools" in command
    assert "default_tools_approval_mode" in command
    assert "enabled_tools" in command
    assert "features.goals=false" in command
    assert "claim-secret" not in command
    assert "--add-dir" in captured["command"]
    assert str(external) in captured["command"]
    assert "sandbox_workspace_write.network_access=true" in captured["command"]
    assert "sandbox_workspace_write.writable_roots=" in command
    assert captured["mcp"]["scope_context"]["KERNEL_SCOPE_PROJECT_ID"] == "p"


async def test_resume_reapplies_writable_roots(tmp_path, monkeypatch):
    captured = {}
    external = tmp_path / "external"
    external.mkdir()

    async def fake_process(*command, **kwargs):
        captured["command"] = command
        return 0, (
            b'{"type":"thread.started","thread_id":"thread-resumed"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        ), b"", 1

    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    await CodexCliHarness(
        ProjectRuntimeManager(tmp_path), codex_binary="codex"
    ).run_turn(AgentTurnRequest(
        project_id="p", prompt="continue", cwd=tmp_path,
        physical_session_id="thread-old", sandbox="workspace-write",
        writable_roots=(external,),
    ))
    command = " ".join(captured["command"])
    assert "exec resume" in command
    assert "sandbox_workspace_write.network_access=true" in command
    assert "sandbox_workspace_write.writable_roots=" in command
    assert str(external) in command


async def test_pinned_permission_profile_replaces_legacy_sandbox_on_fresh_and_resume(
    tmp_path, monkeypatch,
):
    import hashlib

    captured = []
    runtime = ProjectRuntimeManager(tmp_path)
    home = runtime.ensure("p").codex_home
    name = "kernel_run_11111111_1111_1111_1111_111111111111"
    policy = b'default_permissions = "test"\n'
    (home / f"{name}.config.toml").write_bytes(policy)

    async def fake_process(*command, **kwargs):
        captured.append(command)
        return 0, (
            b'{"type":"thread.started","thread_id":"thread-profile"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        ), b"", 1

    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    harness = CodexCliHarness(runtime, codex_binary="codex")
    for native in (None, "thread-old"):
        await harness.run_turn(AgentTurnRequest(
            project_id="p", prompt="test", cwd=tmp_path,
            physical_session_id=native, sandbox="workspace-write",
            provider="openai",
            permission_profile=name,
            permission_profile_sha256=hashlib.sha256(policy).hexdigest(),
        ))
    for command in captured:
        assert "-p" in command
        assert name in command
        assert "--sandbox" not in command
        assert not any("sandbox_mode=" in item for item in command)
        assert not any("sandbox_workspace_write." in item for item in command)
        assert command.index("-p") < command.index("resume") if "resume" in command else True
        assert 'model_provider="openai"' in command


@pytest.fixture(autouse=True)
def isolated_codex_config_query(monkeypatch):
    async def configured(*args,**kwargs):return []
    monkeypatch.setattr('assistant_kernel.harness.mcp_config.configured_servers',configured)
    from assistant_kernel.harness.mcp_config import compile_mcp
    async def compile_fixture(request,environment,binary,archive_dir):
        result=await compile_mcp(request,environment,binary,archive_dir)
        if result[1]:
            payload=json.loads(result[1].read_text())
            (archive_dir/'mcp-runtime.fixture.json').write_text(json.dumps({'exposed':payload['scoped_tools'],'test_fake':True}))
        return result
    monkeypatch.setattr('assistant_kernel.harness.mcp_config.compile_mcp',compile_fixture)
