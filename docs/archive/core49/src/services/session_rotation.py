from __future__ import annotations

from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.harness import AgentTurnRequest, HarnessAdapter
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.storage import ProjectRuntimeManager


class SessionRotationProcessor:
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

    async def process(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(job.get("payload") or {})
        logical_session_id = str(payload["logical_session_id"])
        checkpoint_id = str(payload["checkpoint_id"])
        context = await self.repository.session_rotation_context(
            logical_session_id, checkpoint_id
        )
        if context is None:
            return {"status": "already_rotated_or_obsolete"}
        runtime_project_id = str(context["project_id"] or "system")
        runtime = self.runtime_manager.ensure(runtime_project_id)
        provider = context.get("default_provider") or self.settings.default_codex_provider
        model = context.get("default_model") or self.settings.default_codex_model
        prompt = (
            f"<effective_role>\n{context.get('role_content') or ''}\n</effective_role>\n"
            f"<validated_checkpoint>\n{context['checkpoint_state']}\n</validated_checkpoint>\n"
            "Validate that the checkpoint contains a next action and identifiers. "
            "Do not perform the next action. Reply exactly HANDOFF_OK if it is usable; "
            "otherwise reply HANDOFF_INVALID followed by a concise reason."
        )
        result = await self.harness.run_turn(
            AgentTurnRequest(
                project_id=runtime_project_id,
                prompt=prompt,
                cwd=runtime.vault_root,
                physical_session_id=None,
                provider=provider,
                model=model,
                reasoning_effort=self.settings.default_codex_reasoning_effort,
                sandbox="read-only",
                timeout_seconds=self.settings.agent_timeout_seconds,
            )
        )
        if result.final_text.strip() != "HANDOFF_OK":
            raise RuntimeError(f"new physical session rejected checkpoint: {result.final_text[:500]}")
        physical = await self.repository.record_successful_turn(
            logical_session_id=logical_session_id,
            expected_current_record_id=str(context["parent_physical_session_record_id"]),
            native_session_id=result.physical_session_id,
            harness=self.harness.name,
            provider=result.provider or provider,
            model=result.model or model,
            role_version_id=(str(context["role_version_id"]) if context.get("role_version_id") else None),
            prompt_manifest={
                "schema_version": "2.0",
                "kind": "session_rotation_self_check",
                "logical_session_id": logical_session_id,
                "checkpoint_id": checkpoint_id,
            },
            message_id=None,
            active_input_tokens=result.usage.input_tokens,
            context_window_tokens=self.settings.default_context_window_tokens,
            archive_uri=result.archive_uri,
            archive_sha256=result.archive_sha256,
            starts_new_physical_session=True,
        )
        await self.repository.record_harness_events(
            logical_session_id=logical_session_id,
            physical_session_record_id=str(physical["physical_session_record_id"]),
            job_id=str(job["job_id"]),
            events=result.events,
            raw_trace_uri=result.archive_uri,
        )
        return {
            "status": "rotated",
            "physical_session_record_id": str(physical["physical_session_record_id"]),
            "native_session_id": result.physical_session_id,
        }
