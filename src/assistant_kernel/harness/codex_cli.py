from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import uuid
import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from assistant_kernel.harness.base import AgentTurnRequest, AgentTurnResult
from assistant_kernel.harness.codex_models import CodexModelCatalog, ResolvedCodexModel
from assistant_kernel.harness.context_budget import ContextBudgetDecision, ContextBudgetManager, TurnUsage
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.harness.process_control import run_managed_process


@dataclass(frozen=True)
class CodexTurnResult:
    physical_session_id: str
    usage: TurnUsage
    events: list[dict]
    final_text: str


class CodexCliHarness:
    """Runs Codex with one persistent CODEX_HOME per project.

    Every turn uses a short-lived process. Existing native sessions continue
    through `codex exec resume`; rotation starts a fresh `codex exec`.
    """

    name = "codex_cli"
    _MCP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    def __init__(
        self,
        runtime_manager: ProjectRuntimeManager,
        codex_binary: str = "/home/v/AI/bin/codex",
        timeout_seconds: int = 1800,
    ):
        self.runtime_manager = runtime_manager
        self.codex_binary = codex_binary
        self.timeout_seconds = timeout_seconds
        self.model_catalog = CodexModelCatalog(codex_binary)

    def authentication_mode(self, project_id: str) -> str:
        """Return the billing/authentication route without exposing credentials."""
        auth_path = self.runtime_manager.ensure(project_id).codex_home / "auth.json"
        try:
            mode = str(
                json.loads(auth_path.read_text(encoding="utf-8")).get("auth_mode") or ""
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return "unknown"
        return {
            "chatgpt": "chatgpt_subscription",
            "apikey": "api_key",
            "api_key": "api_key",
        }.get(mode.lower(), mode.lower() or "unknown")

    async def list_model_catalog(self, project_id: str) -> list[dict]:
        runtime = self.runtime_manager.ensure(project_id)
        models = await self.model_catalog.list_models(runtime.codex_home)
        return [self.model_catalog.public_projection(item) for item in models]

    async def resolve_model_profile(
        self,
        *,
        project_id: str,
        provider: str,
        model: str | None,
        reasoning_effort: str | None,
        context_policy: str,
        custom_context_window_tokens: int | None,
        custom_auto_compact_token_limit: int | None,
    ) -> ResolvedCodexModel:
        runtime = self.runtime_manager.ensure(project_id)
        return await self.model_catalog.resolve(
            codex_home=runtime.codex_home,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            context_policy=context_policy,
            custom_context_window_tokens=custom_context_window_tokens,
            custom_auto_compact_token_limit=custom_auto_compact_token_limit,
        )

    async def run_fresh_turn(self, project_id: str, prompt: str, cwd: Path) -> CodexTurnResult:
        result = await self.run_turn(
            AgentTurnRequest(project_id=project_id, prompt=prompt, cwd=cwd)
        )
        return CodexTurnResult(
            result.physical_session_id, result.usage, result.events, result.final_text
        )

    async def run_turn(self, request: AgentTurnRequest) -> AgentTurnResult:
        runtime = self.runtime_manager.ensure(request.project_id)
        codex_home=request.codex_home or runtime.codex_home
        environment = (os.environ.copy() if request.base_environment is None else dict(request.base_environment))
        environment["CODEX_HOME"] = str(codex_home)
        environment.update(request.extra_environment)
        runtime_overrides = ["-c", "features.goals=false"]
        if not 0 <= request.max_concurrent_subagents <= 3:
            raise ValueError('Native temporary subagents must stay within the approved 0..3 bound')
        multi_agent_enabled=request.multi_agent_enabled and request.max_concurrent_subagents>0
        runtime_overrides.extend([
            "-c", f"features.multi_agent={'true' if multi_agent_enabled else 'false'}",
            "-c", f"agents.enabled={'true' if multi_agent_enabled else 'false'}",
            # Keep the verified installed backend; a user config cannot enable
            # a different backend outside the fixed invocation configuration.
            "-c", "features.multi_agent_v2=false",
        ])
        if multi_agent_enabled:
            runtime_overrides.extend(['-c',f'agents.max_concurrent_threads_per_session={request.max_concurrent_subagents}'])
        profile_overrides: list[str] = []
        if request.sandbox == 'danger-full-access':
            if request.permission_profile:
                raise ValueError('shared Project agreement cannot use a per-Run directory permission profile')
            runtime_overrides.extend(['-c','approval_policy="never"'])
        # The model's shell never sees database or scope variables, whatever the sandbox.
        runtime_overrides.extend(['-c','shell_environment_policy.inherit="core"'])
        for name in ['POSTGRES_*','ASSISTANT_*','KERNEL_SCOPE_*','RAG_ENV_FILE']:
            runtime_overrides.extend(['-c','shell_environment_policy.filters.'+json.dumps(name)+'="exclude"'])
        if request.permission_profile:
            if not re.fullmatch(r"kernel_run_[A-Za-z0-9_]+", request.permission_profile):
                raise ValueError("invalid pinned Codex permission profile")
            profile_path = codex_home / f"{request.permission_profile}.config.toml"
            if not profile_path.is_file() or hashlib.sha256(profile_path.read_bytes()).hexdigest() != request.permission_profile_sha256:
                raise ValueError("pinned Codex permission profile is missing or changed")
            # `exec resume` rejects `-p`; the profile belongs to `exec` and
            # must appear before the `resume` subcommand on both turns.
            profile_overrides = ["-p", request.permission_profile]
        elif request.sandbox == "workspace-write":
            runtime_overrides.extend([
                "-c", "sandbox_workspace_write.network_access=true",
            ])
            if request.writable_roots:
                # `codex exec resume` does not expose `--add-dir`.  Keep the
                # effective workspace identical across fresh and resumed turns
                # by applying the config form on every invocation.
                runtime_overrides.extend([
                    "-c",
                    "sandbox_workspace_write.writable_roots="
                    + json.dumps([str(root) for root in request.writable_roots]),
                ])
        if request.model_context_window_tokens is not None:
            if request.model_context_window_tokens <= 0:
                raise ValueError("model_context_window_tokens must be positive")
            runtime_overrides.extend([
                "-c", f"model_context_window={request.model_context_window_tokens}",
            ])
        if request.model_auto_compact_token_limit is not None:
            if request.model_auto_compact_token_limit <= 0:
                raise ValueError("model_auto_compact_token_limit must be positive")
            if (
                request.model_context_window_tokens is not None
                and request.model_auto_compact_token_limit
                >= request.model_context_window_tokens
            ):
                raise ValueError(
                    "model_auto_compact_token_limit must be below model_context_window_tokens"
                )
            runtime_overrides.extend([
                "-c",
                f"model_auto_compact_token_limit={request.model_auto_compact_token_limit}",
            ])
        if request.provider:
            runtime_overrides.extend([
                "-c", f'model_provider="{request.provider}"',
            ])
        invocation_id = request.invocation_id or uuid.uuid4().hex
        archive_dir = runtime.vault_root / "runs" / "agent-traces" / "invocations" / invocation_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        from assistant_kernel.harness.mcp_config import compile_mcp
        scoped_overrides, mcp_private = await compile_mcp(request, environment, self.codex_binary, archive_dir)
        writable_root_args = [
            value
            for root in request.writable_roots
            for value in ("--add-dir", str(root))
        ]
        if request.physical_session_id:
            command = [
                self.codex_binary,
                "exec",
                *profile_overrides,
                "resume",
                *runtime_overrides,
                *scoped_overrides,
                "--json",
                "--skip-git-repo-check",
            ]
            if not request.permission_profile:
                command.extend(["-c", f'sandbox_mode="{request.sandbox}"'])
            if request.model:
                command.extend(["-m", request.model])
            if request.reasoning_effort:
                command.extend(["-c", f'model_reasoning_effort="{request.reasoning_effort}"'])
            command.extend([request.physical_session_id, "-"])
        else:
            command = [
                self.codex_binary,
                "exec",
                *profile_overrides,
                *runtime_overrides,
                *scoped_overrides,
                "--json",
                "--skip-git-repo-check",
                "-C",
                str(request.cwd),
            ]
            if request.ephemeral:
                command.insert(2, "--ephemeral")
            if not request.permission_profile:
                command[2:2] = ["--sandbox", request.sandbox, *writable_root_args]
            if request.model:
                command.extend(["-m", request.model])
            if request.reasoning_effort:
                command.extend(["-c", f'model_reasoning_effort="{request.reasoning_effort}"'])
            command.append("-")
        invocation_path = archive_dir / "request.json"
        invocation_path.write_text(
            json.dumps(
                {
                    "cwd": str(request.cwd),
                    "prompt_sha256": hashlib.sha256(request.prompt.encode()).hexdigest(),
                    "provider": request.provider,
                    "model": request.model,
                    "reasoning_effort": request.reasoning_effort,
                    "context_policy": request.context_policy,
                    "model_context_window_tokens": request.model_context_window_tokens,
                    "model_auto_compact_token_limit": request.model_auto_compact_token_limit,
                    "sandbox": request.sandbox,
                    "permission_profile": request.permission_profile,
                    "permission_profile_sha256": request.permission_profile_sha256,
                    "scoped_tools": list(request.scoped_tools),
                    "writable_roots": [str(item) for item in request.writable_roots],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (archive_dir / "prompt.md").write_text(request.prompt, encoding="utf-8")
        try:
            code, stdout, stderr, _ = await run_managed_process(
                *command,
                cwd=str(request.cwd),
                timeout_seconds=request.timeout_seconds or self.timeout_seconds,
                env=environment,
                input_data=request.prompt.encode("utf-8"),
                capture_directory=archive_dir,
                cancel_event=request.cancel_event,
                on_started=request.process_started_callback,
            )
        finally:
            from assistant_kernel.harness.mcp_cleanup import cleanup_mcp_processes
            process_cleanup=await asyncio.shield(cleanup_mcp_processes(invocation_id))
            (archive_dir/'mcp-cleanup.json').write_text(json.dumps(process_cleanup))
            if mcp_private is not None:mcp_private.unlink(missing_ok=True)
        (archive_dir / "stdout.jsonl").write_bytes(stdout)
        (archive_dir / "stderr.log").write_bytes(stderr)
        if code != 0:   # Codex reports most failures as JSON events on stdout, not on stderr
            raise RuntimeError(f"Codex exited with code {code}: "
                               f"{_last_error(stdout) or stderr.decode('utf-8', 'replace')[-2000:]}")
        mcp_receipts = []
        if request.scoped_tools:
            permitted = set(request.scoped_tools)
            paths=list(archive_dir.glob('mcp-runtime.*.json'))
            mcp_receipts=[json.loads(path.read_text()) for path in paths]
            if not mcp_receipts or process_cleanup['status']!='verified':
                raise RuntimeError('Kernel tool server initialization or connection cleanup is unverified')
            if any(not set(r.get('exposed',[])) <= permitted for r in mcp_receipts):
                raise PermissionError('Kernel tool server exposed tools outside the fixed selection')
        content = stdout.decode("utf-8", "replace")
        parsed = self.parse_jsonl(content)
        native_usage = self.read_native_context_usage(
            codex_home, parsed.physical_session_id
        )
        if native_usage is None:
            context_usage = TurnUsage(
                input_tokens=0,
                output_tokens=parsed.usage.output_tokens,
                reasoning_output_tokens=parsed.usage.reasoning_output_tokens,
            )
            usage_metadata = {
                "context_usage_known": False,
                "usage_source": "turn_completed_total_fallback",
                "total_usage": parsed.usage.model_dump(),
            }
        else:
            context_usage, native_window, total_usage = native_usage
            usage_metadata = {
                "context_usage_known": True,
                "usage_source": "native_last_token_usage",
                "context_window_tokens": native_window,
                "total_usage": total_usage,
            }
        archive_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        archive_dir = runtime.vault_root / "runs" / "agent-traces" / parsed.physical_session_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        archive_path = archive_dir / f"{timestamp}.jsonl"
        archive_path.write_text(content, encoding="utf-8")
        archive_uri = (
            f"vault://{request.project_id}/"
            f"{archive_path.relative_to(runtime.vault_root).as_posix()}"
        )
        usage_metadata['mcp_runtime'] = {'receipt_count':len(mcp_receipts),
            'exposed_tools':sorted({name for r in mcp_receipts for name in r.get('exposed',[])}),
            'receipts':[{'uri':f'vault://{request.project_id}/'+path.relative_to(runtime.vault_root).as_posix(),
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for path in archive_dir.glob('mcp-runtime.*.json')],
            'process_cleanup':process_cleanup,
            'inventory_scope':'managed entry points only; source policy separately verified'}
        return AgentTurnResult(
            physical_session_id=parsed.physical_session_id,
            usage=context_usage,
            events=parsed.events,
            final_text=parsed.final_text,
            archive_uri=archive_uri,
            archive_sha256=archive_hash,
            provider=request.provider,
            model=request.model,
            metadata=usage_metadata,
        )

    @staticmethod
    def parse_jsonl(content: str) -> CodexTurnResult:
        events = [json.loads(line) for line in content.splitlines() if line.strip()]
        session_id = ""
        usage = TurnUsage(input_tokens=0)
        final_text = ""
        completed = False
        for event in events:
            event_type = event.get("type")
            if event_type == "thread.started":
                session_id = str(event.get("thread_id") or event.get("thread", {}).get("id") or "")
            elif event_type == "turn.completed":
                completed = True
                raw = event.get("usage") or {}
                usage = TurnUsage(
                    input_tokens=int(raw.get("input_tokens", 0)),
                    cached_input_tokens=int(raw.get("cached_input_tokens", 0)),
                    output_tokens=int(raw.get("output_tokens", 0)),
                    reasoning_output_tokens=int(raw.get("reasoning_output_tokens", 0)),
                )
            elif event_type == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message":
                    final_text = str(item.get("text", final_text))
            elif event_type == "turn.failed":
                raise ValueError("Codex reported an unsuccessful turn")
        if not session_id:
            raise ValueError("Codex JSONL did not contain thread.started")
        if not completed or not final_text.strip():
            raise ValueError("Codex turn has no completed nonempty final result")
        return CodexTurnResult(session_id, usage, events, final_text)

    @staticmethod
    def read_native_context_usage(
        codex_home: Path, physical_session_id: str,
    ) -> tuple[TurnUsage, int, dict] | None:
        """Read the last request occupancy from the native Codex rollout."""
        sessions_root = codex_home / "sessions"
        matches = list(sessions_root.rglob(f"*{physical_session_id}.jsonl"))
        if not matches:
            return None
        source = max(matches, key=lambda item: item.stat().st_mtime_ns)
        last_info: dict | None = None
        with source.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    if info.get("last_token_usage"):
                        last_info = info
        if last_info is None:
            return None
        last = last_info["last_token_usage"]
        window = int(last_info.get("model_context_window") or 0)
        if window <= 0:
            return None
        usage = TurnUsage(
            input_tokens=int(last.get("input_tokens") or 0),
            cached_input_tokens=int(last.get("cached_input_tokens") or 0),
            output_tokens=int(last.get("output_tokens") or 0),
            reasoning_output_tokens=int(last.get("reasoning_output_tokens") or 0),
        )
        return usage, window, dict(last_info.get("total_token_usage") or {})

    @staticmethod
    def rotation_decision(result: CodexTurnResult, context_window_tokens: int) -> ContextBudgetDecision:
        return ContextBudgetManager().evaluate(result.usage, context_window_tokens)


def _last_error(stdout: bytes) -> str:
    """The last error Codex reported in its JSON event stream (e.g. "Selected model is at capacity")."""
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = (event.get("error") or {}).get("message") if event.get("type") == "turn.failed" else \
            event.get("message") if event.get("type") == "error" else None
        if message:
            return str(message)[:1000]
    return ""
