"""Runtime navigation catalog, separate from audit-map snapshots."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


class RuntimeCatalogService:
    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def refresh(self) -> dict[str, Any]:
        directory = self.settings.data_root / "config" / "project-directory.yaml"
        raw = yaml.safe_load(directory.read_text(encoding="utf-8")) if directory.is_file() else {}
        projects = []
        for entry in (raw or {}).get("projects") or []:
            project_id = str(entry.get("project_id") or "")
            if not project_id:
                continue
            vault = self.runtime.describe(project_id).vault_root
            project_file = vault / "project.yaml"
            project = (
                yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
                if project_file.is_file() else {}
            )
            map_root = vault / "knowledge" / "map"
            map_index = map_root / "index.yaml"
            map_latest_path = map_root / "latest.json"
            semantic_map_ref: dict[str, Any]
            if map_latest_path.is_file():
                map_latest = json.loads(map_latest_path.read_text(encoding="utf-8"))
                manifest_relative = Path(str(map_latest.get("manifest") or ""))
                manifest_path = (map_root / manifest_relative).resolve()
                if (
                    not map_latest.get("complete")
                    or not manifest_relative.parts
                    or manifest_path != map_root.resolve()
                    and map_root.resolve() not in manifest_path.parents
                    or not manifest_path.is_file()
                ):
                    raise ValueError(f"Project Semantic Map latest is invalid: {project_id}")
                manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                if manifest_sha != str(map_latest.get("manifest_sha256") or ""):
                    raise ValueError(f"Project Semantic Map manifest differs: {project_id}")
                semantic_map_ref = {
                    "uri": (
                        f"vault://{project_id}/knowledge/map/"
                        f"{manifest_relative.as_posix()}"
                    ),
                    "sha256": manifest_sha,
                    "generation_id": str(map_latest.get("generation_id") or ""),
                    "complete": True,
                }
            else:
                semantic_map_ref = {
                    "uri": f"vault://{project_id}/knowledge/map/index.yaml",
                    "sha256": hashlib.sha256(map_index.read_bytes()).hexdigest()
                    if map_index.is_file() else None,
                    "generation_id": None,
                    "complete": False,
                }
            projects.append({
                "project_id": project_id,
                "status": str(entry.get("status") or project.get("status") or "active"),
                "display_name": str(project.get("display_name") or project_id),
                "purpose": str(project.get("purpose") or entry.get("purpose") or ""),
                "default_agent": str(project.get("default_agent") or "pm"),
                "project_contract_ref": {
                    "uri": f"vault://{project_id}/project.yaml",
                    "sha256": hashlib.sha256(project_file.read_bytes()).hexdigest()
                    if project_file.is_file() else None,
                },
                "semantic_map_ref": semantic_map_ref,
            })
        payload = {
            "schema_version": "runtime-global-catalog.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "projects": sorted(projects, key=lambda item: item["project_id"]),
            "authority": {
                "project_semantics": "project_contract",
                "runtime_state": "postgresql",
                "navigation": "runtime_catalog",
            },
        }
        content_sha = hashlib.sha256(json.dumps(
            {"projects": payload["projects"], "authority": payload["authority"]},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        generation_id = "gen-" + content_sha[:20]
        root = self.runtime.ensure("system").vault_root / "knowledge" / "runtime-map"
        generation = root / "generations" / generation_id
        catalog_path = generation / "catalog.json"
        manifest_path = generation / "manifest.json"
        generation.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
        if catalog_path.exists() and manifest_path.exists():
            encoded = catalog_path.read_bytes()
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        else:
            catalog_path.write_bytes(encoded)
            manifest = {
                "schema_version": "runtime-map-manifest.v1",
                "generation_id": generation_id,
                "status": "complete",
                "catalog": f"generations/{generation_id}/catalog.json",
                "catalog_sha256": hashlib.sha256(encoded).hexdigest(),
                "project_count": len(projects),
                "generated_at": payload["generated_at"],
            }
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, indent=2
            ).encode()
            manifest_path.write_bytes(manifest_bytes)
        latest = {
            "schema_version": "runtime-map-latest.v1",
            "generation_id": generation_id,
            "manifest": f"generations/{generation_id}/manifest.json",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "complete": True,
            "updated_at": manifest["generated_at"],
        }
        latest_path = root / "latest.json"
        temporary = latest_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(latest_path)
        return latest


class ConversationDigestService:
    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def materialize(
        self, *, project_id: str, channel: str, chat_id: str,
        messages: list[dict[str, Any]], cumulative_state: dict[str, Any],
        task_refs: list[str], quality: dict[str, Any],
    ) -> dict[str, Any]:
        message_ids = [str(item.get("message_id") or "") for item in messages]
        material = {"message_ids": message_ids, "state": cumulative_state}
        digest_id = "digest-" + hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()[:20]
        payload = {
            "schema_version": "conversation-digest.v1",
            "digest_id": digest_id,
            "project_id": project_id,
            "conversation": {"channel": channel, "chat_id": chat_id},
            "covered_message_ids": message_ids,
            "coverage_start": message_ids[0] if message_ids else None,
            "coverage_end": message_ids[-1] if message_ids else None,
            "user_decisions": list(cumulative_state.get("decisions") or []),
            "confirmed_facts": list(cumulative_state.get("confirmed_facts") or []),
            "agent_claims": [],
            "intent_hypotheses": [],
            "constraints": list(cumulative_state.get("constraints") or []),
            "open_questions": list(cumulative_state.get("open_questions") or []),
            "task_refs": list(task_refs),
            "artifact_refs": [],
            "conflicts": [],
            "quality": dict(quality),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        root = self.runtime.ensure(project_id).vault_root / "knowledge" / "history" / "conversations"
        conversation_key = hashlib.sha256(f"{channel}:{chat_id}".encode()).hexdigest()[:20]
        directory = root / conversation_key
        target = directory / f"{digest_id}.json"
        if target.exists():
            return json.loads(target.read_text(encoding="utf-8"))
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode()
        directory.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != encoded:
            raise RuntimeError("conversation digest collision")
        if not target.exists():
            target.write_bytes(encoded)
        latest = directory / "latest.json"
        temporary = latest.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({
            "schema_version": "conversation-digest-latest.v1",
            "digest_id": digest_id,
            "uri": f"vault://{project_id}/{target.relative_to(self.runtime.ensure(project_id).vault_root).as_posix()}",
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(latest)
        return payload
