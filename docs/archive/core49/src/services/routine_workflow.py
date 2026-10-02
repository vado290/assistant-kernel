from __future__ import annotations

import hashlib
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import ControlPlaneRepository
from assistant_kernel.services.schema_validation import validate_json_schema
from assistant_kernel.storage import ProjectRuntimeManager


class RoutineWorkflowProcessor:
    def __init__(self, settings: KernelSettings, repository: ControlPlaneRepository):
        self.settings = settings
        self.repository = repository
        self.runtime_manager = ProjectRuntimeManager(settings.data_root)

    async def process(self, job: dict[str, Any]) -> dict[str, Any]:
        workflow_run_id = str(job["workflow_run_id"])
        context = await self.repository.routine_workflow_context(workflow_run_id)
        if context is None:
            raise ValueError("workflow run not found")
        input_data = dict(context["input_json"] or {})
        validate_json_schema(input_data, dict(context["input_schema"] or {}))
        spec = dict(context["spec_json"] or {})
        executor = dict(spec.get("executor") or {})
        if executor.get("type") != "python_script":
            raise ValueError("only python_script workflow executor is enabled in this stage")
        project_id = str(context["project_id"])
        definition_path = str(context["definition_path"])
        prefix = f"vault://{project_id}/"
        if not definition_path.startswith(prefix):
            raise ValueError("workflow definition must be inside its Project Vault")
        source = self.runtime_manager.resolve_vault_file(
            project_id, definition_path[len(prefix):]
        )
        if not source.is_file() or source.suffix.lower() != ".py":
            raise ValueError("workflow definition is not an existing Python file")
        actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual_hash != context["definition_sha256"]:
            raise ValueError("workflow definition hash does not match the pinned version")
        timeout_seconds = min(max(int(executor.get("timeout_seconds", 300)), 1), 3600)
        args = [str(value) for value in executor.get("args", [])]
        return await self.repository.dispatch_routine_python_execution(
            workflow_run_id=workflow_run_id,
            source_uri=definition_path,
            input_data=input_data,
            output_schema=dict(context["output_schema"] or {}),
            timeout_seconds=timeout_seconds,
            args=args,
        )
