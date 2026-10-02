"""Seal one Task candidate into an immutable, content-addressed review bundle."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


class CandidateBundleService:
    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def seal(self, project_id: str, task_id: str) -> dict[str, Any]:
        vault = self.runtime.ensure(project_id).vault_root
        candidate_root = vault / "runs" / task_id / "candidate"
        manifest_path = candidate_root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("Task candidate manifest is missing")
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("schema_version") != "candidate-manifest.v2":
            raise ValueError("unsupported candidate manifest")
        prepared: list[dict[str, Any]] = []
        for raw in manifest.get("files") or []:
            if not isinstance(raw, dict):
                raise ValueError("candidate manifest entry must be an object")
            item = dict(raw)
            operation = str(item.get("operation") or "")
            target_uri = str(item.get("target_uri") or "")
            candidate_uri = str(item.get("candidate_uri") or "")
            if operation not in {"create", "update", "delete"} or not target_uri:
                raise ValueError("candidate manifest entry is incomplete")
            data = None
            relative = None
            if operation != "delete":
                prefix = "workspace://candidate/"
                if not candidate_uri.startswith(prefix):
                    raise ValueError("candidate URI is invalid")
                relative = candidate_uri[len(prefix):]
                source = (candidate_root / relative).resolve()
                if candidate_root.resolve() not in source.parents or not source.is_file():
                    raise FileNotFoundError(candidate_uri)
                data = source.read_bytes()
                if self._sha256(data) != str(item.get("candidate_sha256") or ""):
                    raise ValueError("candidate file hash differs from manifest")
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("candidate file exceeds the 2 MiB review limit")
            prepared.append({
                "operation": operation,
                "target_uri": target_uri,
                "candidate_uri": candidate_uri or None,
                "relative": relative,
                "original_sha256": item.get("original_sha256"),
                "candidate_sha256": item.get("candidate_sha256"),
                "size": int(item.get("size") or 0),
                "data": data,
            })
        identity = [{
            key: value for key, value in item.items()
            if key not in {"data"}
        } for item in prepared]
        bundle_seed = {
            "schema_version": "candidate-bundle.v1",
            "project_id": project_id,
            "task_id": task_id,
            "manifest_sha256": self._sha256(manifest_bytes),
            "files": identity,
        }
        canonical = json.dumps(
            bundle_seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        bundle_hash = self._sha256(canonical)
        bundle_root = vault / "runtime" / "candidate-snapshots" / bundle_hash
        files_root = bundle_root / "files"
        if not bundle_root.exists():
            temporary = bundle_root.with_name(bundle_root.name + ".tmp")
            if temporary.exists():
                shutil.rmtree(temporary)
            files_root_temp = temporary / "files"
            for item in prepared:
                if item["operation"] == "delete":
                    continue
                target = files_root_temp / str(item["relative"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item["data"])
            projected = []
            for item in identity:
                artifact_uri = None
                if item["operation"] != "delete":
                    artifact_uri = (
                        f"vault://{project_id}/runtime/candidate-snapshots/"
                        f"{bundle_hash}/files/{item['relative']}"
                    )
                projected.append({**item, "artifact_uri": artifact_uri})
            payload = {
                **bundle_seed,
                "bundle_sha256": bundle_hash,
                "files": projected,
            }
            temporary.mkdir(parents=True, exist_ok=True)
            (temporary / "bundle.json").write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            temporary.replace(bundle_root)
        bundle_path = bundle_root / "bundle.json"
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        file_artifacts = [{
            "uri": item["artifact_uri"],
            "sha256": item["candidate_sha256"],
            "size": item["size"],
            "kind": "candidate_file",
            "target_uri": item["target_uri"],
            "operation": item["operation"],
            "required": True,
        } for item in payload["files"] if item.get("artifact_uri")]
        return {
            "schema_version": "candidate-bundle.v1",
            "bundle_sha256": bundle_hash,
            "bundle_uri": (
                f"vault://{project_id}/runtime/candidate-snapshots/"
                f"{bundle_hash}/bundle.json"
            ),
            "bundle_size": bundle_path.stat().st_size,
            "bundle_file_sha256": self._sha256(bundle_path.read_bytes()),
            "manifest_sha256": payload["manifest_sha256"],
            "files": payload["files"],
            "file_artifacts": file_artifacts,
        }
