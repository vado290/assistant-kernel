from __future__ import annotations

from typing import Any
import uuid

from assistant_kernel.config import KernelSettings
from assistant_kernel.harness import AgentTurnRequest, HarnessAdapter
from assistant_kernel.repositories import ControlPlaneRepository, canonical_sha256
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.services.agent_config import AgentConfigManager


BUTLER_ROLE = """You are the system butler for a personal assistant Kernel.
Understand the user's message and distinguish casual conversation, an existing
project request, a new-project candidate, and system management. Never silently
create or switch a project. If project identity is unclear, ask one concise
clarification question. Do not perform long implementation work.
"""


PROJECT_MANAGER_ROLE = """You are the persistent project manager for the project
named in the prompt. Handle normal project conversation, clarify new requests,
surface risks and missing requirements, and explain formal stage results. Never
claim that a formal workflow or approval ran when it did not. Recommend a formal
task when implementation, review, or external effects are required.
"""


class ConversationProcessor:
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

    async def process(self, job: dict[str, Any]) -> dict[str, Any]:
        context = await self.repository.conversation_job_context(str(job["job_id"]))
        if context is None:
            raise ValueError("conversation job context not found")
        agent_id = str(context["agent_id_snapshot"])
        project_id = context["project_id_snapshot"]
        role_content = context.get("role_content")
        role_version_id = context.get("role_version_id")
        if not role_content:
            default_role = BUTLER_ROLE if agent_id == "system-butler" else PROJECT_MANAGER_ROLE
            role_version = await self.repository.ensure_role_version(
                agent_id, "primary", default_role
            )
            role_content = role_version["content"]
            role_version_id = role_version["role_version_id"]

        agent_type = "butler" if agent_id == "system-butler" else "project_manager"
        agent_config = await self.agent_configs.resolve(
            agent_id=agent_id,
            agent_type=agent_type,
            project_id=project_id,
            role_version_id=str(role_version_id) if role_version_id else None,
        )
        configured_harness = agent_config.config.harness_defaults.harness
        if configured_harness not in {"codex_exec", "codex_cli", self.harness.name}:
            raise ValueError(f"configured harness is not available: {configured_harness}")

        logical = await self.repository.resolve_logical_session(
            agent_id=agent_id,
            scope_type="conversation",
            scope_id=str(context["conversation_id"]),
            project_id=project_id,
        )
        physical = logical.get("physical_session")
        starts_new = physical is None or physical["status"] == "rotation_required"
        native_session_id = None if starts_new else str(physical["native_session_id"])
        runtime_project_id = str(project_id or "system")
        runtime = self.runtime_manager.ensure(runtime_project_id)

        message_text = self._message_text(context["message_content"])
        projects = await self.repository.list_projects() if project_id is None else []
        project_context = (
            await self.repository.list_project_context(str(project_id)) if project_id else []
        )
        checkpoint = logical.get("checkpoint")
        requested_profile = dict((context.get("message_metadata") or {}).get("execution_profile") or {})
        harness_defaults = agent_config.config.harness_defaults
        provider = (
            requested_profile.get("provider")
            or context.get("default_provider")
            or harness_defaults.provider
            or self.settings.default_codex_provider
        )
        model = (
            requested_profile.get("model")
            or context.get("default_model")
            or harness_defaults.model
            or self.settings.default_codex_model
        )
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
        profile_id = await self.repository.record_model_execution_profile(
            project_id=project_id,
            task_id=None,
            stage_id=None,
            harness=self.harness.name,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            sandbox_policy="read-only",
            approval_policy="deny_external_effects",
            requested_profile=requested_profile,
            resolved_profile={
                "harness": self.harness.name,
                "provider": provider,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "sandbox": "read-only",
            },
        )
        manifest = {
            "schema_version": "2.0",
            "logical_session_id": str(logical["logical_session_id"]),
            "parent_physical_session_record_id": (
                str(physical["physical_session_record_id"]) if physical else None
            ),
            "role_version_id": str(role_version_id) if role_version_id else None,
            "role_sha256": canonical_sha256({"content": role_content}),
            "checkpoint_id": str(checkpoint["checkpoint_id"]) if checkpoint else None,
            "message_id": str(context["message_id"]),
            "binding_version": int(context["binding_version"]),
            "project_id": project_id,
            "agent_id": agent_id,
            "model_execution_profile_id": profile_id,
            "agent_config_version_id": agent_config.config_version_id,
            "agent_config_sha256": agent_config.config_sha256,
            "agent_scope_version_id": agent_config.scope_version_id,
            "effective_scope_sha256": agent_config.scope_sha256,
            "skill_bindings": agent_config.tool_context["skill_bindings"],
            "tool_bindings": agent_config.tool_context["tools"],
        }
        prompt = self._prompt(
            role_content=str(role_content),
            project_id=project_id,
            projects=projects,
            project_context=project_context,
            checkpoint=checkpoint["state_json"] if checkpoint else None,
            user_message=message_text,
            required_skills=agent_config.required_skill_texts,
        )
        invocation_id = str(uuid.uuid4())
        tool_context = {
            **agent_config.tool_context,
            "invocation_id": invocation_id,
            "agent_config_version_id": agent_config.config_version_id,
            "job_id": str(job["job_id"]),
            "worker_instance_id": str(job["worker_instance_id"]),
        }
        await self.agent_configs.record_invocation(invocation_id, agent_config)
        result = await self.harness.run_turn(
            AgentTurnRequest(
                project_id=runtime_project_id,
                prompt=prompt,
                cwd=runtime.vault_root,
                physical_session_id=native_session_id,
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
                sandbox="read-only",
                timeout_seconds=timeout_seconds,
                invocation_id=invocation_id,
                agent_tool_context=tool_context,
            )
        )
        if not result.final_text.strip():
            raise RuntimeError("agent returned an empty reply")
        expected_current = (
            str(physical["physical_session_record_id"]) if physical is not None else None
        )
        physical_record = await self.repository.record_successful_turn(
            logical_session_id=str(logical["logical_session_id"]),
            expected_current_record_id=expected_current,
            native_session_id=result.physical_session_id,
            harness=self.harness.name,
            provider=result.provider or provider,
            model=result.model or model,
            role_version_id=str(role_version_id) if role_version_id else None,
            prompt_manifest=manifest,
            message_id=str(context["message_id"]),
            active_input_tokens=result.usage.input_tokens,
            context_window_tokens=self.settings.default_context_window_tokens,
            archive_uri=result.archive_uri,
            archive_sha256=result.archive_sha256,
            starts_new_physical_session=starts_new,
        )
        await self.repository.record_harness_events(
            logical_session_id=str(logical["logical_session_id"]),
            physical_session_record_id=str(physical_record["physical_session_record_id"]),
            job_id=str(job["job_id"]),
            events=result.events,
            raw_trace_uri=result.archive_uri,
        )
        reply = await self.repository.append_reply_and_outbox(
            job_id=str(job["job_id"]),
            source_message_id=str(context["message_id"]),
            conversation_id=str(context["conversation_id"]),
            binding_version=int(context["binding_version"]),
            project_id=project_id,
            agent_id=agent_id,
            text=result.final_text,
            metadata={
                "logical_session_id": str(logical["logical_session_id"]),
                "physical_session_record_id": str(physical_record["physical_session_record_id"]),
                "native_session_id": result.physical_session_id,
            },
        )
        ratio = result.usage.input_tokens / self.settings.default_context_window_tokens
        rotation = None
        if ratio > self.settings.context_soft_ratio:
            recent = await self.repository.get_messages(str(context["conversation_id"]), 20)
            checkpoint_state = {
                "schema_version": "2.0",
                "project_id": project_id,
                "task_id": None,
                "stage_id": None,
                "logical_session_id": str(logical["logical_session_id"]),
                "physical_session_record_id": str(physical_record["physical_session_record_id"]),
                "role_version_id": str(role_version_id) if role_version_id else None,
                "goal": "continue the bound conversation",
                "constraints": ["preserve project scope", "do not infer approval"],
                "acceptance_status": {},
                "frozen_decisions": [],
                "open_questions": [],
                "completed": [f"replied to message {context['message_id']}"],
                "pending": [],
                "artifact_refs": [],
                "evidence_refs": [f"message://{item['message_id']}" for item in recent],
                "tool_state": {},
                "risks": [],
                "next_action": "process the next FIFO message",
                "prompt_manifest": manifest,
            }
            rotation = await self.repository.require_rotation(
                logical_session_id=str(logical["logical_session_id"]),
                physical_session_record_id=str(physical_record["physical_session_record_id"]),
                project_id=project_id,
                task_id=None,
                checkpoint_state=checkpoint_state,
            )
        return {"reply": reply, "rotation": rotation}

    @staticmethod
    def _message_text(content: dict[str, Any]) -> str:
        text = content.get("text") if isinstance(content, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message content must contain non-empty text")
        return text.strip()

    @staticmethod
    def _prompt(
        *, role_content: str, project_id: str | None,
        projects: list[dict[str, Any]], project_context: list[dict[str, Any]],
        checkpoint: dict[str, Any] | None,
        user_message: str,
        required_skills: dict[str, str] | None = None,
    ) -> str:
        project_block = (
            f"Current confirmed project: {project_id}"
            if project_id
            else "Known project candidates: "
            + ", ".join(str(item["project_id"]) for item in projects[:20])
        )
        checkpoint_block = (
            f"Validated recovery checkpoint: {checkpoint}" if checkpoint else "No recovery checkpoint."
        )
        control_context = [
            {
                "entry_id": item["entry_id"],
                "type": item["entry_type"],
                "statement": item["statement"],
                "evidence_refs": item["evidence_refs"],
            }
            for item in project_context
        ]
        skills_block = "\n\n".join(
            f"<skill name=\"{name}\">\n{content}\n</skill>"
            for name, content in (required_skills or {}).items()
        )
        return (
            f"<effective_role>\n{role_content}\n</effective_role>\n"
            f"<required_skills>\n{skills_block}\n</required_skills>\n"
            f"<project_context>\n{project_block}\n</project_context>\n"
            f"<project_control_context>\n{control_context}\n</project_control_context>\n"
            f"<checkpoint>\n{checkpoint_block}\n</checkpoint>\n"
            f"<user_message>\n{user_message}\n</user_message>"
        )
