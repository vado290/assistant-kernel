from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


class StageHandoffService:
    """Materialize and resolve the exact upstream inputs for one Stage Run."""

    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    @staticmethod
    def artifact_id(item: dict[str, Any]) -> str:
        encoded = json.dumps(
            {"uri": item.get("uri"), "sha256": item.get("sha256")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return "artifact-" + hashlib.sha256(encoded).hexdigest()[:24]

    def normalize_inputs(self, project_id: str, request: dict[str, Any]) -> list[dict[str, Any]]:
        prefix = f"vault://{project_id}/"
        result: list[dict[str, Any]] = []
        for raw in request.get("prior_artifacts") or []:
            item = dict(raw) if isinstance(raw, dict) else {"uri": str(raw)}
            uri = str(item.get("uri") or "")
            if not uri.startswith(prefix):
                raise ValueError("handoff artifact belongs to another Project")
            path = self.runtime.resolve_vault_file(project_id, uri[len(prefix):])
            if not path.is_file():
                raise FileNotFoundError(uri)
            data = path.read_bytes()
            actual_hash = hashlib.sha256(data).hexdigest()
            expected_hash = item.get("sha256")
            if expected_hash and expected_hash != actual_hash:
                raise ValueError("handoff artifact hash changed")
            normalized = {
                **item,
                "uri": uri,
                "sha256": actual_hash,
                "size": len(data),
                "artifact_id": item.get("artifact_id") or self.artifact_id({
                    "uri": uri, "sha256": actual_hash,
                }),
                "workspace_uri": f"workspace://project/{uri[len(prefix):]}",
                "required": bool(item.get("required", True)),
            }
            result.append(normalized)
        return result

    def materialize(self, run: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        project_id = str(run["project_id"])
        request = dict(run.get("request_json") or {})
        inputs = self.normalize_inputs(project_id, request)
        payload = {
            "schema_version": "stage-handoff.v1",
            "task_root_run_id": request.get("task_root_run_id") or str(run["run_id"]),
            "current_run_id": str(run["run_id"]),
            "current_stage": run.get("stage"),
            "stage_index": int(request.get("stage_index") or 0),
            "stage_round": int(request.get("stage_round") or 1),
            "task_kind": request.get("task_kind") or "project_task",
            "goal": request.get("goal"),
            "capability_change": request.get("capability_change"),
            "required_inputs": inputs,
            "user_decisions": request.get("answer_history") or [],
            "acceptance_criteria": request.get("acceptance_criteria") or [],
            "open_questions": [],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        vault = self.runtime.ensure(project_id).vault_root
        attempt = int(run.get("attempt") or 0)
        relative = (
            Path("runs") / str(run["run_id"]) / "input" /
            f"handoff-attempt-{attempt}.json"
        )
        target = vault / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != encoded:
            raise RuntimeError("Stage handoff changed after it was materialized")
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(target)
        return payload, f"vault://{project_id}/{relative.as_posix()}", digest

    def read_bound_artifact(
        self, run: dict[str, Any], artifact_id: str, max_bytes: int = 262144
    ) -> dict[str, Any]:
        project_id = str(run["project_id"])
        request = dict(run.get("request_json") or {})
        inputs = self.normalize_inputs(project_id, request)
        item = next((entry for entry in inputs if entry["artifact_id"] == artifact_id), None)
        if item is None:
            raise PermissionError("artifact is not bound to the current Stage")
        prefix = f"vault://{project_id}/"
        target = self.runtime.resolve_vault_file(project_id, item["uri"][len(prefix):])
        data = target.read_bytes()
        limit = min(max(int(max_bytes), 1), 2 * 1024 * 1024)
        if len(data) > limit:
            return {**item, "text": None, "content_omitted": True}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {**item, "text": None, "content_omitted": True, "binary": True}
        return {**item, "text": text, "content_omitted": False, "binary": False}
