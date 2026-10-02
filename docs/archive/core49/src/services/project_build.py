from __future__ import annotations

import hashlib
import json
import uuid
import re
from psycopg.types.json import Jsonb
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.harness import AgentTurnRequest, HarnessAdapter
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.domain.project_build import ProjectBuildState
from assistant_kernel.repositories.stages import StageRepository
from assistant_kernel.services.prompt_bundle import PromptBundle, STAGE_SKILLS, default_role
from assistant_kernel.services.stage_protocol import read_stage_intent
from assistant_kernel.services.agent_config import AgentConfigManager


STAGE_ROLES = {stage: default_role(stage) for stage in STAGE_SKILLS}


class CodexStageExecutor:
    def __init__(
        self,
        settings: KernelSettings,
        repository: ControlPlaneRepository,
        harness: HarnessAdapter,
    ):
        self.settings = settings
        self.repository = repository
        self.harness = harness
        self.runtime_manager = ProjectRuntimeManager(settings.data_root)
        self.agent_configs = AgentConfigManager(settings)

    async def execute(self, stage: str, state: ProjectBuildState) -> dict[str, Any]:
        if stage not in STAGE_ROLES:
            raise ValueError(f"unknown project-build stage {stage!r}")
        project_id = str(state["project_id"])
        task_id = str(state["task_id"])
        # Reuse a finished artifact if the worker died before the stage transaction.
        async with self.repository.database.connection() as connection:
            cursor = await connection.execute("""
                SELECT * FROM assistant.artifact_versions WHERE project_id=%s AND task_id=%s
                  AND metadata->>'stage_run_id'=%s ORDER BY version DESC LIMIT 1
            """, (project_id,task_id,str(state["stage_run_id"])))
            cached = await cursor.fetchone()
        if cached:
            return {**cached["metadata"]["stage_result"],"artifact_version_id":str(cached["artifact_version_id"])}
        agent_id = await self.repository.ensure_stage_agent(project_id, stage)
        role = await self.repository.get_active_role(agent_id)
        if role is None:
            role = await self.repository.ensure_role_version(agent_id, "primary", STAGE_ROLES[stage])
        bundle = PromptBundle(self.settings)
        binding = state.get("prompt_binding") or bundle.bind(project_id, stage, role)
        bound = bundle.load(project_id, binding)
        role = {**role, "content": bound["role"], "role_version_id": binding["role_version_id"]}
        agent_config = await self.agent_configs.resolve(
            agent_id=agent_id,
            agent_type=f"stage_{stage}",
            project_id=project_id,
            role_version_id=str(role["role_version_id"]),
        )
        configured_harness = agent_config.config.harness_defaults.harness
        if configured_harness not in {"codex_exec", "codex_cli", self.harness.name}:
            raise ValueError(f"configured harness is not available: {configured_harness}")
        async with self.repository.database.connection() as c:
            await c.execute("UPDATE assistant.node_runs SET prompt_binding=%s WHERE node_run_id=%s AND prompt_binding='{}'::jsonb",
                (Jsonb(binding), state["stage_run_id"]))
            await c.commit()
        attempt = int(state["stage_attempt"])
        logical = await self.repository.resolve_logical_session(
            agent_id=agent_id,
            scope_type="task_stage",
            scope_id=f"{task_id}:{stage}:{attempt}",
            project_id=project_id,
        )
        physical = logical.get("physical_session")
        runtime = self.runtime_manager.ensure(project_id)
        requested_profile = dict((state.get("request") or {}).get("execution_profile") or {})
        harness_defaults = agent_config.config.harness_defaults
        provider = requested_profile.get("provider") or harness_defaults.provider or self.settings.default_codex_provider
        model = requested_profile.get("model") or harness_defaults.model or self.settings.default_codex_model
        reasoning_effort = (
            requested_profile.get("reasoning_effort")
            or harness_defaults.reasoning_effort
            or self.settings.default_codex_reasoning_effort
        )
        timeout_seconds = min(
            max(
                int(requested_profile.get("timeout_seconds", harness_defaults.timeout_seconds)),
                30,
            ),
            1800,
        )
        sandbox = "workspace-write" if stage == "implementation" else "read-only"
        profile_id = await self.repository.record_model_execution_profile(
            project_id=project_id,
            task_id=task_id,
            stage_id=stage,
            harness=self.harness.name,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            sandbox_policy=sandbox,
            approval_policy="human_for_external_effects",
            requested_profile=requested_profile,
            resolved_profile={
                "harness": self.harness.name,
                "provider": provider,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "sandbox": sandbox,
            },
        )
        cwd = runtime.vault_root
        if state.get("flow") == "optimization":
            from assistant_kernel.services.optimization import prepare_candidate
            cwd = prepare_candidate(self.settings, state, stage)
        prompt = self._stage_prompt(stage, state, str(role["content"]), self._artifact_contents(state))
        prompt += "\n<bound_skill>\n" + bound["skill"] + "\n</bound_skill>\n"
        if state.get("flow") == "optimization":
            prompt += f"\nCandidate working directory: {cwd}. Only modify candidate files, never publish automatically."
        turn_key = str(uuid.uuid4())
        tool_context = {"job_id": str(state["job_id"]), "node_run_id": str(state["stage_run_id"]),
                        "worker_instance_id": str(state["worker_instance_id"]), "turn_key": turn_key}
        agent_tool_context = {
            **agent_config.tool_context,
            **tool_context,
            "invocation_id": turn_key,
            "agent_config_version_id": agent_config.config_version_id,
        }
        await self.agent_configs.record_invocation(turn_key, agent_config)
        result = await self.harness.run_turn(
            AgentTurnRequest(
                project_id=project_id,
                prompt=prompt,
                cwd=cwd,
                physical_session_id=(str(physical["native_session_id"]) if physical else None),
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
                sandbox=sandbox,
                timeout_seconds=timeout_seconds,
                stage_tool_context=tool_context,
                invocation_id=turn_key,
                agent_tool_context=agent_tool_context,
            )
        )
        intent = await read_stage_intent(self.settings, turn_key)
        if intent:
            document = intent["request_json"]["document"]
        else:
            if self.harness.name == "codex_cli" and stage == "requirements":
                raise RuntimeError("stage must use ask_user or submit_stage_for_confirmation before ending")
            document = result.final_text
        expected_current = str(physical["physical_session_record_id"]) if physical else None
        physical_record = await self.repository.record_successful_turn(
            logical_session_id=str(logical["logical_session_id"]),
            expected_current_record_id=expected_current,
            native_session_id=result.physical_session_id,
            harness=self.harness.name,
            provider=result.provider,
            model=result.model,
            role_version_id=str(role["role_version_id"]),
            prompt_manifest={
                "schema_version": "2.0",
                "task_id": task_id,
                "stage_id": stage,
                "attempt": attempt,
                "role_version_id": str(role["role_version_id"]),
                "model_execution_profile_id": profile_id,
                "prompt_binding": binding,
                "stage_tool_turn_key": turn_key,
                "cwd": str(cwd),
                "agent_config_version_id": agent_config.config_version_id,
                "agent_config_sha256": agent_config.config_sha256,
                "agent_scope_version_id": agent_config.scope_version_id,
                "effective_scope_sha256": agent_config.scope_sha256,
                "tool_bindings": agent_config.tool_context["tools"],
            },
            message_id=None,
            active_input_tokens=result.usage.input_tokens,
            context_window_tokens=self.settings.default_context_window_tokens,
            archive_uri=result.archive_uri,
            archive_sha256=result.archive_sha256,
            starts_new_physical_session=physical is None,
        )
        await self.repository.record_harness_events(
            logical_session_id=str(logical["logical_session_id"]),
            physical_session_record_id=str(physical_record["physical_session_record_id"]),
            job_id=state.get("job_id"),
            events=result.events,
            raw_trace_uri=result.archive_uri,
        )
        artifact_dir = runtime.vault_root / "artifacts" / task_id / stage
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"attempt-{attempt}.md"
        artifact_path.write_text(document, encoding="utf-8")
        content_hash = hashlib.sha256(document.encode("utf-8")).hexdigest()
        storage_uri = f"vault://{project_id}/{artifact_path.relative_to(runtime.vault_root).as_posix()}"
        output = {"storage_uri":storage_uri,"content_sha256":content_hash,"summary":document[:2000]}
        if intent:
            output.update({"interaction_request": intent["request_json"], "stage_request_id": str(intent["request_id"])})
        if state.get("flow") == "optimization" and stage == "implementation":
            output["candidate_uri"] = f"vault://{project_id}/runs/{task_id}/candidate"
            output["publication_status"] = "candidate_only"
            output["candidate_files"] = []
            for p in sorted(cwd.rglob('*')):
                if p.is_file():
                    resolved = self.runtime_manager.resolve_below(cwd, p.relative_to(cwd).as_posix())
                    output["candidate_files"].append({"path":p.relative_to(cwd).as_posix(),"sha256":hashlib.sha256(resolved.read_bytes()).hexdigest()})
        if stage == "review":
            verdicts = re.findall(r"^VERDICT: (APPROVE|REQUEST_CHANGES|BLOCKED)\s*$", result.final_text.strip(), re.MULTILINE)
            last_line = result.final_text.strip().splitlines()[-1] if result.final_text.strip() else ""
            output["verdict"] = verdicts[0].lower() if len(verdicts) == 1 and last_line.strip() == f"VERDICT: {verdicts[0]}" else "blocked"
        artifact = await self.repository.create_artifact_version(
            project_id=project_id,
            task_id=task_id,
            stage_id=stage,
            artifact_type=stage,
            storage_uri=storage_uri,
            content_sha256=content_hash,
            metadata={
                "attempt": attempt,
                "physical_session_record_id": str(physical_record["physical_session_record_id"]),
                "native_session_id": result.physical_session_id,
                "trace_uri": result.archive_uri,
                "stage_run_id":str(state["stage_run_id"]),
                "stage_result":output,
            },
        )
        output["artifact_version_id"] = str(artifact["artifact_version_id"])
        return output

    def _artifact_contents(self, state: ProjectBuildState) -> dict[str, str]:
        """Load complete, hash-verified documents from this project's Vault only."""
        project_id = str(state["project_id"])
        root = self.runtime_manager.describe(project_id).vault_root
        prefix = f"vault://{project_id}/"
        contents: dict[str, str] = {}
        total_bytes = 0
        for stage in STAGE_ROLES:
            artifact = state.get(f"{stage}_artifact")
            if not artifact:
                continue
            uri = artifact.get("storage_uri", "")
            if not uri.startswith(prefix):
                raise ValueError(f"{stage} artifact is outside the project Vault")
            path = self.runtime_manager.resolve_below(root, uri[len(prefix):])
            # Refuse oversized context rather than silently dropping accepted requirements.
            if path.stat().st_size > 262144:
                raise ValueError(f"{stage} artifact exceeds the context document limit")
            data = path.read_bytes()
            total_bytes += len(data)
            if total_bytes > 524288:
                raise ValueError("stage documents exceed the combined context limit")
            if hashlib.sha256(data).hexdigest() != artifact.get("content_sha256"):
                raise ValueError(f"{stage} artifact hash mismatch")
            contents[stage] = data.decode("utf-8")
        return contents

    @staticmethod
    def _stage_prompt(stage: str, state: ProjectBuildState, role: str,
                      artifact_contents: dict[str, str] | None = None) -> str:
        relevant = {
            "request": state.get("request"),
            "requirements": state.get("requirements_artifact"),
            "implementation": state.get("implementation_artifact"),
            "review": state.get("review_artifact"),
            "requirements_feedback": state.get("requirements_feedback"),
            "acceptance_decision": state.get("acceptance_decision"),
            "approval_history": state.get("approval_history", []),
            "question_history": state.get("question_history", []),
            "artifact_contents": artifact_contents or {},
            "rework_attempt": state.get("rework_attempt", 0),
        }
        prompt = (
            f"<effective_role>{role}</effective_role>\n"
            f"<stage>{stage}</stage>\n"
            f"<project_build_state>{json.dumps(relevant, ensure_ascii=False)}</project_build_state>\n"
            "Use the complete artifact_contents and all approval_history as business context. "
            "Apply acceptance_decision when reworking; document any conflicting requirements. "
            "Produce a concise, auditable stage result. Reference actual files and evidence."
        )
        # This Harness still passes the prompt as one argv entry. Fail explicitly
        # before the Linux argument-size ceiling, without truncating approved text.
        if len(prompt.encode("utf-8")) > 524288:
            raise ValueError("complete stage prompt exceeds the context budget; split the input documents")
        return prompt


class ProjectBuildProcessor:
    def __init__(
        self,
        settings: KernelSettings,
        repository: ControlPlaneRepository,
        harness: HarnessAdapter,
    ):
        self.stage_executor = CodexStageExecutor(settings, repository, harness)
        self.stages = StageRepository(settings)

    async def process(self, job: dict[str, Any]) -> dict[str, Any]:
        async with self.stages.database.connection() as guard:
            await guard.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (f"build:{job['workflow_run_id']}",))
            await guard.commit()
            context = await self.stages.context(job)
            if context is None:
                raise ValueError("stage job/run identity mismatch")
            receipt = (context["stage_output"] or {}).get("job_result")
            if context["stage_status"] in {"succeeded","cancelled"} and receipt:
                return receipt
            proceed = await self.stages.begin(context,job)
            state = {**context["stage_input"],"stage_attempt":context["stage_attempt"],
                     "stage_run_id":str(context["node_run_id"]),"job_id":str(job["job_id"]),
                     "worker_instance_id":str(job["worker_instance_id"]),"prompt_binding":context.get("prompt_binding") or {}}
            artifact = await self.stage_executor.execute(context["node_id"],state) if proceed else {}
            return await self.stages.finish(context,job,artifact)
