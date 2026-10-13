"""File-backed LLM-oriented semantic map and bounded context selection."""
from __future__ import annotations

import hashlib
import os
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
TASK_KIND_ALIASES = {"analysis": "requirements", "design": "requirements",
                     "implementation": "build", "review": "audit", "operation": "operate"}
CRITICAL_KINDS = {
    "permission", "credential", "trade", "external_effect", "run_state",
    "approval", "publisher", "recovery", "service_entrypoint",
    "persistent_contract", "user_flow",
}


class SemanticMapService:
    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _root(self, project_id: str) -> Path:
        return self.runtime.ensure(project_id).vault_root / "knowledge" / "map"

    def _index(self, project_id: str) -> Path:
        return self._root(project_id) / "index.yaml"

    def publish_current(self, project_id: str) -> dict[str, Any]:
        """Snapshot the compatibility index into an immutable runtime generation."""
        latest_path=self._root(project_id)/'latest.json'
        if latest_path.is_file() and json.loads(latest_path.read_bytes()).get('schema_version')=='semantic-map-latest.v2':
            raise PermissionError('Map 2.1 has one JSON writer; the YAML compatibility source is read-only')
        index = self._index(project_id)
        if not index.is_file():
            raise FileNotFoundError("Project Semantic Map index is missing")
        self._validate(project_id, yaml.safe_load(index.read_text(encoding="utf-8")) or {})
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
        (root / "stale.json").unlink(missing_ok=True)
        return latest

    def mark_stale(self, project_id: str, *, reason: str) -> None:
        path = self._root(project_id) / "stale.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value={"schema_version": "semantic-map-freshness.v1",
                              "status": "stale", "reason": str(reason)[:500],
                              "observed_at": datetime.now(timezone.utc).isoformat()}
        latest_path=self._root(project_id)/'latest.json'
        latest=json.loads(latest_path.read_bytes()) if latest_path.is_file() else {}
        if latest.get('schema_version')=='semantic-map-latest.v2':
            from assistant_kernel.services.contracts import contracts
            value.update(schema_version='semantic-map-freshness.v2',project_id=project_id,
                generation_id=latest['generation_id'],pending_batch_refs=[])
            contracts().validate('MapStale',value)
        encoded=json.dumps(value)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)

    def current_ref(self, project_id: str) -> dict[str, Any]:
        root = self._root(project_id)
        latest_path = root / "latest.json"
        if not latest_path.is_file():
            return {"uri": f"vault://{project_id}/knowledge/map/index.yaml",
                    "complete": False, "freshness": "legacy"}
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        if not latest.get("complete"):
            raise ValueError("Semantic Map generation is incomplete")
        manifest_path = self.runtime.resolve_below(root, str(latest["manifest"]))
        encoded = manifest_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != latest["manifest_sha256"]:
            raise ValueError("Semantic Map manifest hash differs")
        manifest = json.loads(encoded)
        if latest.get('schema_version')=='semantic-map-latest.v2':
            from assistant_kernel.services.contracts import contracts
            contracts().validate('MapLatest',latest);contracts().validate('MapManifest',manifest)
        if (manifest.get("status") != "complete" or manifest.get("project_id") != project_id
                or manifest.get("generation_id") != latest.get("generation_id")):
            raise ValueError("Semantic Map manifest is invalid")
        index_path = self.runtime.resolve_below(root, str(manifest["index"]))
        index_bytes = index_path.read_bytes()
        if hashlib.sha256(index_bytes).hexdigest() != manifest["index_sha256"]:
            raise ValueError("Semantic Map index hash differs")
        return {"uri": f"vault://{project_id}/knowledge/map/{manifest['index']}",
                "sha256": manifest["index_sha256"], "generation_id": latest["generation_id"],
                "complete": True,
                "freshness": "stale" if (root / "stale.json").exists() else "current"}

    @staticmethod
    def _skip_file(relative: str, *, project_vault: bool = False) -> bool:
        parts = Path(relative).parts
        if any(part in {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
                        "logs", "build", "dist"} for part in parts):
            return True
        if project_vault and relative.startswith(("runtime/", "runs/", "knowledge/map/", "knowledge/change-diffs/")):
            return True
        return Path(relative).name.endswith((".tmp", ".lock", ".pyc"))

    def scan_files(self, project_id: str, roots: list[Any], *,
                   source_uris: list[str] | None = None, exact_sources: bool = False,
                   file_limit: int | None = 10000) -> dict[str, dict[str, Any]]:
        from assistant_kernel.services.workspace_scope import WorkspaceScopeService
        files: dict[str, dict[str, Any]] = {}
        payload = self.load(project_id)
        registered_documents = {str(item.get("uri") or ""): item for item in payload.get("files") or []
                                if item.get("kind") == "document" and item.get("document_ref")}
        registered_documents.update(getattr(self, "_scan_document_entries", {}))
        omitted_document_uris = getattr(self, "_scan_omitted_document_uris", set())
        for uri in omitted_document_uris:
            registered_documents.pop(uri, None)
        managed = {str(item.get("uri") or "") for item in payload.get("files") or []}
        managed.update(self.source_workspace_uri(project_id, uri)
                       for component in payload.get("components") or []
                       for uri in component.get("source_refs") or [])
        managed.update(source_uris or [])
        managed.update(registered_documents)
        managed.difference_update(omitted_document_uris)
        # Grant parents are capability boundaries, not automatic discovery roots.
        if any(root.key == "project" for root in roots):
            managed.update(f"workspace://project/{name}" for name in (
                "project.yaml", "source-map.yaml", "agents", ".agents/skills", "background"))
        if exact_sources: managed=set(source_uris or [])
        scopes = WorkspaceScopeService(self.settings)
        for selected_uri in sorted(managed):
            if not selected_uri.startswith("workspace://"):
                continue
            registered_document = registered_documents.get(selected_uri)
            try:
                if registered_document:
                    from assistant_kernel.services.document_runtime import TaskDocumentService
                    from assistant_kernel.services.workspace_scope import AuthorizedRoot
                    ref = registered_document["document_ref"]
                    TaskDocumentService(self.settings).read(project_id, ref)
                    vault = self.runtime.describe(project_id).vault_root
                    root = AuthorizedRoot("project", vault, "read", "Kernel-managed document index")
                    selected = self.runtime.resolve_vault_file(project_id, ref["uri"][len(f"vault://{project_id}/"):])
                else:
                    root, selected, _ = scopes.resolve_uri(roots, selected_uri)
            except (ValueError, PermissionError):
                continue
            if root.key in {"candidate", "run_work"} or selected.is_symlink():
                continue
            entries = [(str(selected.parent), [], [selected.name])] if selected.is_file() else (
                os.walk(selected, followlinks=False) if selected.is_dir() else [])
            for parent, directories, names in entries:
                relative_parent = Path(parent).relative_to(root.path)
                directories[:] = [name for name in sorted(directories)
                    if not (Path(parent) / name).is_symlink()
                    and not self._skip_file((relative_parent / name / "_").as_posix(), project_vault=root.key == "project")
                    and not WorkspaceScopeService._root_excluded(
                        root, (relative_parent / name).as_posix(), is_directory=True)]
                for name in sorted(names):
                    relative = (relative_parent / name).as_posix()
                    if (not registered_document and self._skip_file(relative, project_vault=root.key == "project")) or WorkspaceScopeService._root_excluded(
                            root, relative, is_directory=False):
                        continue
                    target = Path(parent) / name
                    if target.is_symlink() or not target.is_file():
                        continue
                    if file_limit is not None and len(files) >= file_limit:
                        raise RuntimeError("Project Map scan exceeds 10000 files; old generation retained")
                    uri = f"workspace://{root.key}/{relative}"
                    digest = hashlib.sha256()
                    with target.open("rb") as handle:
                        while chunk := handle.read(1024 * 1024):
                            digest.update(chunk)
                    files[uri] = {"root_key": root.key, "path": relative, "kind": "file",
                                  "sha256": digest.hexdigest(), "uri": uri}
        return files

    @staticmethod
    def source_workspace_uri(project_id: str, uri: str) -> str:
        if isinstance(uri,dict): uri=uri.get('uri') or ''
        prefix = f"vault://{project_id}/"
        return f"workspace://project/{uri[len(prefix):]}" if uri.startswith(prefix) else uri

    def validate_sources(self, project_id: str, *, roots: list[Any] | None = None,
                         component_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Check referenced source hashes without treating navigation as approval."""
        from assistant_kernel.services.workspace_scope import AuthorizedRoot, WorkspaceScopeService
        if roots is None:
            roots = [AuthorizedRoot("project", self.runtime.describe(project_id).vault_root,
                                    "read", "current Project Vault")]
        diagnostics = []
        wanted = set(component_ids or [])
        for component in self.load(project_id).get("components") or []:
            if wanted and component["component_id"] not in wanted:
                continue
            expected = component.get("source_sha256")
            hashes = expected if isinstance(expected, list) else [expected]
            for index, uri in enumerate(component.get("source_refs") or []):
                expected_sha = uri.get('sha256') if isinstance(uri,dict) else hashes[index] if index < len(hashes) else None
                if isinstance(uri,dict):uri=uri.get('uri') or ''
                status, actual = "unresolved_scope", None
                try:
                    prefix = f"vault://{project_id}/"
                    if str(uri).startswith(prefix):
                        target = self.runtime.resolve_vault_file(project_id, str(uri)[len(prefix):])
                    else:
                        _, target, _ = WorkspaceScopeService(self.settings).resolve_uri(roots, str(uri))
                    if target.is_file():
                        actual = hashlib.sha256(target.read_bytes()).hexdigest()
                        status = "current" if expected_sha and actual == expected_sha else "stale" if expected_sha else "unverified"
                    else:
                        status = "present_directory" if target.is_dir() else "missing"
                except (ValueError, PermissionError):
                    pass
                diagnostics.append({"component_id": component["component_id"], "uri": uri,
                                    "status": status, "expected_sha256": expected_sha,
                                    "actual_sha256": actual})
        if any(item["status"] in {"stale", "missing"} for item in diagnostics):
            self.mark_stale(project_id, reason="referenced source differs or is missing")
        return diagnostics

    def refresh_files(self, project_id: str, *, roots: list[Any],
                      changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Reconcile current source bytes; modification times are never event times."""
        latest_path=self._root(project_id)/'latest.json'
        if latest_path.is_file() and json.loads(latest_path.read_bytes()).get('schema_version')=='semantic-map-latest.v2':
            from assistant_kernel.services.semantic_map_v2 import SemanticMapV2
            return SemanticMapV2(self.settings).refresh(project_id,roots=roots,changes=changes or [])
        try:
            payload = self.load(project_id)
            old = {str(item.get("uri") or f"workspace://{item['root_key']}/{item['path']}"): item
                   for item in payload.get("files") or []}
            document_changes = [item for item in changes or []
                                if item.get("scope_kind") == "artifact" and item.get("actor") == "kernel"
                                and item.get("document_ref")]
            registered = dict(old)
            for item in document_changes:
                ref = item["document_ref"]
                prior = next((entry for entry in registered.values() if entry.get("document_id") == ref["id"]), None)
                if prior:
                    previous_version = (prior.get("state_revision"), prior.get("context_epoch") or 0)
                    desired_version = (item.get("state_revision"), item.get("context_epoch") or 0)
                    if isinstance(previous_version[0], int) and isinstance(desired_version[0], int):
                        if desired_version < previous_version:
                            continue
                    elif prior.get("last_changed_at") and item.get("occurred_at") and item["occurred_at"] < prior["last_changed_at"]:
                        continue
                # One navigation head per logical document; immutable old files
                # remain available through the ledger and prior generations.
                registered = {uri: entry for uri, entry in registered.items()
                              if entry.get("document_id") != ref["id"]}
                relative = ref["uri"][len(f"vault://{project_id}/"):]
                registered[item["target_uri"]] = {
                    "uri": item["target_uri"], "root_key": "project", "path": relative,
                    "kind": "document", "sha256": item.get("after_sha256"),
                    "document_id": ref["id"], "document_schema": ref.get("document_schema"),
                    "document_ref": ref, "purpose": item.get("purpose") or ref.get("purpose"),
                    "state_revision": item.get("state_revision"), "context_epoch": item.get("context_epoch"),
                    "last_changed_at": item.get("occurred_at"), "last_change_id": item.get("change_id")}
            # Pass only the typed registration to scanning, without publishing a
            # draft index. Scope soft filters still apply to all ordinary files.
            self._scan_document_entries = {uri: entry for uri, entry in registered.items() if entry.get("kind") == "document"}
            self._scan_omitted_document_uris = set(old) - set(registered)
            scanned = self.scan_files(project_id, roots, source_uris=[
                str(item.get("target_uri") or item.get("uri") or "") for item in changes or []])
            by_uri = {str(item.get("target_uri") or item.get("uri") or ""): item
                      for item in changes or []}
            now = datetime.now(timezone.utc).isoformat()
            result = []
            for uri, file in sorted(scanned.items()):
                previous = registered.get(uri) or {}
                change = by_uri.get(uri) or {}
                changed = previous.get("sha256") != file["sha256"]
                components = [item for item in payload.get("components") or []
                              if uri in [self.source_workspace_uri(project_id, str(ref))
                                          for ref in item.get("source_refs") or []]]
                result.append({**previous, **file,
                    "kind": previous.get("kind") or file["kind"],
                    "file_id": previous.get("file_id") or "file-" + hashlib.sha256(uri.encode()).hexdigest()[:20],
                    "purpose": change.get("purpose") or previous.get("purpose") or
                               (components[0].get("purpose") if components else "待分类"),
                    "component_ids": [item["component_id"] for item in components],
                    "last_changed_at": change.get("occurred_at") or
                        (None if changed else previous.get("last_changed_at")),
                    "last_observed_at": now if changed else previous.get("last_observed_at", now),
                    "last_change_id": change.get("change_id") or
                        (None if changed else previous.get("last_change_id")),
                    "lines_added": change.get("lines_added") if changed or change else previous.get("lines_added"),
                    "lines_deleted": change.get("lines_deleted") if changed or change else previous.get("lines_deleted"),
                    "diff_ref": (change.get("diff_ref") if change.get("diff_matches_observed", True) else None)
                        if changed or change else previous.get("diff_ref"),
                    "freshness": "current", "cause_status": change.get("cause_status") or
                        ("unknown" if changed else previous.get("cause_status", "unknown"))})
            payload["files"] = result
            payload["files_coverage"] = "managed_sources"
            for component in payload.get("components") or []:
                refs = component.get("source_refs") or []
                hashes = [scanned.get(self.source_workspace_uri(project_id, str(uri)), {}).get("sha256") for uri in refs]
                if any(hashes):
                    component["source_sha256"] = hashes[0] if len(hashes) == 1 else hashes
            payload["files_complete"] = True
            if payload != self.load(project_id):
                payload["generated_at"] = now
                target = self._index(project_id)
                temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
                temporary.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
                temporary.replace(target)
            return {**self.publish_current(project_id), "file_count": len(result), "freshness": "current"}
        except Exception as exc:
            self.mark_stale(project_id, reason=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._scan_document_entries = {}
            self._scan_omitted_document_uris = set()

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
        if (self._root(project_id) / "latest.json").is_file():
            ref = self.current_ref(project_id)
            path = self.runtime.resolve_vault_file(project_id, ref["uri"].split(f"vault://{project_id}/", 1)[1])
        if not path.is_file():
            return {
                "schema_version": "semantic-map.v1", "project_id": project_id,
                "components": [], "unclassified_sources": [],
            }
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return self._validate(project_id, payload)

    @staticmethod
    def _validate(project_id: str, payload: Any) -> dict[str, Any]:
        if isinstance(payload,dict) and payload.get('schema_version')=='semantic-map.v2':
            from assistant_kernel.services.contracts import contracts
            contracts().validate('Active_MapIndex',payload)
            if payload['project_id']!=project_id:raise PermissionError('Semantic Map belongs to another Project')
            if len({x['component_id'] for x in payload['components']})!=len(payload['components']):raise ValueError('Duplicate Map component identity')
            if len({x['uri'] for x in payload['files']})!=len(payload['files']):raise ValueError('Duplicate Map file identity')
            return payload
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
        limit: int = 20, roots: list[Any] | None = None,
    ) -> dict[str, Any]:
        payload = self.load(project_id)
        wanted = {str(item) for item in component_ids or []}
        terms = self._tokens(" ".join(query_terms))
        scored: list[tuple[float, str, dict[str, Any], list[str]]] = []
        for component in payload.get("components") or []:
            component_id = str(component["component_id"])
            reasons: list[str] = []
            task_relevance = RELEVANCE.get(
                str((component.get("relevance") or {}).get(task_kind) or
                    (component.get("relevance") or {}).get(TASK_KIND_ALIASES.get(task_kind)) or "low"),
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
            "source_diagnostics": self.validate_sources(
                project_id, roots=roots, component_ids=[item["component_id"] for item in selected]),
            "map_ref": self.current_ref(project_id),
        }

    def materialize_context(
        self, project_id: str, run_id: str, *, task_kind: str,
        query_terms: list[str], component_ids: list[str] | None = None,
        variant: str | None = None,
        accepted_documents: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        index = self._index(project_id)
        if not index.is_file() and not (self._root(project_id)/'latest.json').is_file():
            return None
        map_ref = self.current_ref(project_id)
        if map_ref.get("complete"):
            index = self.runtime.resolve_vault_file(
                project_id, map_ref["uri"].split(f"vault://{project_id}/", 1)[1])
        index_bytes = index.read_bytes()
        query = self.query(
            project_id, task_kind=task_kind, query_terms=query_terms,
            component_ids=component_ids, limit=20,
        )
        map_ref = query.get("map_ref") or map_ref
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
            component = next(item for item in self.load(project_id).get("components") or []
                             if item["component_id"] == selected["component_id"])
            expected = component.get("source_sha256")
            hashes = expected if isinstance(expected, list) else [expected]
            for source_index, uri in enumerate(selected.get("source_refs") or []):
                entries.append({
                    "ref": dict(uri) if isinstance(uri,dict) else {
                        "schema_version": "resource-ref.v1", "kind": "source",
                        "id": f"{selected['component_id']}:{uri}", "uri": str(uri),
                        "document_schema": None, "revision_id": None,
                        "sha256": hashes[source_index] if source_index < len(hashes) else None,
                        "purpose": "semantic_map_source",
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
            "map_uri": map_ref["uri"],
            "map_sha256": hashlib.sha256(index_bytes).hexdigest(),
            "map_generation_id": map_ref.get("generation_id"),
            "map_freshness": map_ref.get("freshness"),
            "source_diagnostics": query.get("source_diagnostics") or [],
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
