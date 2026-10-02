"""File-backed LLM-oriented semantic map and bounded context selection."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


RELEVANCE = {"required": 1.0, "high": 0.8, "medium": 0.55,
             "low": 0.3, "archive": 0.0}
CRITICAL_KINDS = {
    "permission", "credential", "trade", "external_effect", "run_state",
    "approval", "publisher", "recovery", "service_entrypoint",
    "persistent_contract", "user_flow",
}


class SemanticMapService:
    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _root(self, project_id: str) -> Path:
        return self.runtime.ensure(project_id).vault_root / "knowledge" / "map"

    def _index(self, project_id: str) -> Path:
        return self._root(project_id) / "index.yaml"

    def publish_current(self, project_id: str) -> dict[str, Any]:
        """Snapshot the compatibility index into an immutable runtime generation."""
        index = self._index(project_id)
        if not index.is_file():
            raise FileNotFoundError("Project Semantic Map index is missing")
        self.load(project_id)
        encoded = index.read_bytes()
        source_sha = hashlib.sha256(encoded).hexdigest()
        generation_id = "gen-" + source_sha[:20]
        root = self._root(project_id)
        generation = root / "generations" / generation_id
        generation.mkdir(parents=True, exist_ok=True)
        snapshot = generation / "index.yaml"
        if snapshot.exists() and snapshot.read_bytes() != encoded:
            raise RuntimeError("Semantic Map generation collision")
        if not snapshot.exists():
            snapshot.write_bytes(encoded)
        manifest = {
            "schema_version": "semantic-map-manifest.v1",
            "generation_id": generation_id,
            "project_id": project_id,
            "status": "complete",
            "index": f"generations/{generation_id}/index.yaml",
            "index_sha256": source_sha,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = generation / "manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
        manifest_bytes = manifest_path.read_bytes()
        latest = {
            "schema_version": "semantic-map-latest.v1",
            "generation_id": generation_id,
            "manifest": f"generations/{generation_id}/manifest.json",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "complete": True,
            "updated_at": manifest["generated_at"],
        }
        latest_path = root / "latest.json"
        temporary = latest_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(latest, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temporary.replace(latest_path)
        return latest

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return set(re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]", value.lower()))

    @staticmethod
    def _write_once(path: Path, text: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    def bootstrap(
        self, project_id: str, *, purpose: str,
        base_pack_version: str = "assistant-base-pack.v1",
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "semantic-map.v1",
            "project_id": project_id,
            "base_pack_version": base_pack_version,
            "map_schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "authority": {
                "semantic_navigation": "this_map",
                "runtime_state": "postgresql",
                "implementation": "source_and_artifacts",
                "external_effects": "receipts",
            },
            "components": [],
            "unclassified_sources": [],
        }
        self._write_once(
            self._index(project_id),
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        )
        self._write_once(
            self._root(project_id) / "overview.md",
            "\n".join((
                f"# {project_id} · Project Semantic Map",
                "",
                str(purpose).strip(),
                "",
                "This document explains the current Project semantics. Runtime state,",
                "permissions, approvals and external effects remain authoritative in the",
                "Kernel database, source artifacts and external receipts.",
                "",
            )),
        )
        loaded = self.load(project_id)
        self.publish_current(project_id)
        return loaded

    def load(self, project_id: str) -> dict[str, Any]:
        path = self._index(project_id)
        if not path.is_file():
            return {
                "schema_version": "semantic-map.v1", "project_id": project_id,
                "components": [], "unclassified_sources": [],
            }
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict) or payload.get("schema_version") != "semantic-map.v1":
            raise ValueError("semantic map must use semantic-map.v1")
        if str(payload.get("project_id") or "") != project_id:
            raise ValueError("semantic map belongs to another Project")
        components = payload.get("components") or []
        if not isinstance(components, list):
            raise ValueError("semantic map components must be a list")
        seen: set[str] = set()
        for component in components:
            if not isinstance(component, dict):
                raise ValueError("semantic map component must be an object")
            component_id = str(component.get("component_id") or "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,159}", component_id):
                raise ValueError(f"invalid semantic component id: {component_id}")
            if component_id in seen:
                raise ValueError(f"duplicate semantic component id: {component_id}")
            seen.add(component_id)
            if not str(component.get("purpose") or "").strip():
                raise ValueError(f"semantic component has no purpose: {component_id}")
        return payload

    def query(
        self, project_id: str, *, task_kind: str,
        query_terms: list[str], component_ids: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        payload = self.load(project_id)
        wanted = {str(item) for item in component_ids or []}
        terms = self._tokens(" ".join(query_terms))
        scored: list[tuple[float, str, dict[str, Any], list[str]]] = []
        for component in payload.get("components") or []:
            component_id = str(component["component_id"])
            reasons: list[str] = []
            task_relevance = RELEVANCE.get(
                str((component.get("relevance") or {}).get(task_kind) or "low"),
                0.3,
            )
            haystack = " ".join((
                component_id, str(component.get("purpose") or ""),
                str(component.get("why") or ""),
                " ".join(str(item) for item in component.get("tags") or []),
            ))
            match = min(len(terms & self._tokens(haystack)) / max(len(terms), 1), 1.0)
            criticality = min(max(float(component.get("criticality") or 0) / 5, 0), 1)
            risk = min(max(float(component.get("change_risk") or 0) / 5, 0), 1)
            evidence = min(max(float(component.get("evidence_quality") or 3) / 5, 0), 1)
            score = 35 * task_relevance + 25 * match + 15 * criticality + 15 * risk + 10 * evidence
            if component_id in wanted:
                score = 100
                reasons.append("explicit component target")
            if task_relevance >= 1:
                reasons.append(f"required for {task_kind}")
            if match:
                reasons.append("task terms matched")
            if criticality >= 0.8:
                reasons.append("business critical")
            scored.append((score, component_id, component, reasons))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = [
            {
                "component_id": component_id,
                "score": round(score, 2),
                "required": component_id in wanted or score >= 80,
                "reason": reasons or ["bounded semantic-map selection"],
                "source_refs": list(component.get("source_refs") or []),
                "document_ref": component.get("document_ref"),
                "skill_rule_refs": list(component.get("skill_rule_refs") or []),
            }
            for score, component_id, component, reasons in scored[:min(max(limit, 1), 50)]
            if score > 0 or component_id in wanted
        ]
        return {
            "schema_version": "semantic-map-query.v1",
            "project_id": project_id, "task_kind": task_kind,
            "selected": selected,
        }

    def materialize_context(
        self, project_id: str, run_id: str, *, task_kind: str,
        query_terms: list[str], component_ids: list[str] | None = None,
        variant: str | None = None,
        accepted_documents: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        index = self._index(project_id)
        if not index.is_file():
            return None
        index_bytes = index.read_bytes()
        query = self.query(
            project_id, task_kind=task_kind, query_terms=query_terms,
            component_ids=component_ids, limit=20,
        )
        entries: list[dict[str, Any]] = []
        for name, ref in (accepted_documents or {}).items():
            if isinstance(ref, dict) and ref.get("schema_version") == "resource-ref.v1":
                entries.append({
                    "ref": dict(ref), "purpose": str(name),
                    "load_mode": (
                        "full" if name in {"task_contract", "current_action_plan"}
                        else "summary"
                    ),
                    "required": name in {"task_contract", "last_turn"},
                    "section_ids": [], "freshness": "current",
                    "stale_policy": "block" if name in {"task_contract", "last_turn"}
                    else "omit",
                })
        for selected in query["selected"]:
            for uri in selected.get("source_refs") or []:
                entries.append({
                    "ref": {
                        "schema_version": "resource-ref.v1", "kind": "source",
                        "id": f"{selected['component_id']}:{uri}", "uri": str(uri),
                        "document_schema": None, "revision_id": None,
                        "sha256": None, "purpose": "semantic_map_source",
                    },
                    "purpose": f"map_component:{selected['component_id']}",
                    "load_mode": "on_demand", "required": bool(selected.get("required")),
                    "section_ids": [], "freshness": "current",
                    "stale_policy": "verify_source",
                })
        document = {
            "schema_version": "context-manifest.v3",
            "project_id": project_id, "run_id": run_id,
            "task_kind": task_kind,
            "map_uri": f"vault://{project_id}/knowledge/map/index.yaml",
            "map_sha256": hashlib.sha256(index_bytes).hexdigest(),
            "selected": query["selected"],
            "entries": entries,
            "omitted": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        suffix = f"-{variant}" if variant else ""
        path = (
            self.runtime.ensure(project_id).vault_root / "runs" / run_id /
            "resolved" / f"context-manifest{suffix}.json"
        )
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != encoded:
            raise RuntimeError("Context Manifest is immutable for one Run variant")
        if not path.exists():
            temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(encoded)
            temporary.replace(path)
        return {
            **document,
            "uri": f"vault://{project_id}/runs/{run_id}/resolved/{path.name}",
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    @staticmethod
    def is_critical(component: dict[str, Any]) -> bool:
        return bool(
            int(component.get("criticality") or 0) >= 4
            or set(str(item) for item in component.get("kinds") or []) & CRITICAL_KINDS
        )
