"""Versioned semantic documents for one Project Task.

The filesystem owns immutable semantic content; core.tasks records which
``resource-ref.v1`` revision is current for each document of a task.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


SAFE_KIND = re.compile(r"^[a-z][a-z0-9._-]{0,79}$")
RESOURCE_KINDS = {"document", "message", "source", "receipt", "trace"}
ACTOR_TYPES = {
    "user", "kernel", "supervisor", "pm", "native_subagent",
    "validator", "publisher",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def resource_ref(
    *, kind: str, identity: str, uri: str | None = None,
    schema_version: str | None = None, revision_id: str | None = None,
    sha256: str | None = None, purpose: str = "",
) -> dict[str, Any]:
    """Build the only cross-document reference shape used by v2 Tasks."""
    if kind not in RESOURCE_KINDS:
        raise ValueError("unsupported resource reference kind")
    if not str(identity).strip():
        raise ValueError("resource reference id is required")
    if kind == "document" and (not revision_id or not sha256 or not uri):
        raise ValueError("document reference requires uri, revision_id, and sha256")
    return {
        "schema_version": "resource-ref.v1",
        "kind": kind,
        "id": str(identity),
        "uri": str(uri) if uri else None,
        "document_schema": str(schema_version) if schema_version else None,
        "revision_id": str(revision_id) if revision_id else None,
        "sha256": str(sha256) if sha256 else None,
        "purpose": str(purpose or ""),
    }


def validate_resource_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != "resource-ref.v1":
        raise ValueError("resource reference must use resource-ref.v1")
    return resource_ref(
        kind=str(value.get("kind") or ""),
        identity=str(value.get("id") or ""),
        uri=value.get("uri"),
        schema_version=value.get("document_schema"),
        revision_id=value.get("revision_id"),
        sha256=value.get("sha256"),
        purpose=str(value.get("purpose") or ""),
    )


class TaskDocumentService:
    """Write immutable Task revisions and rebuildable current pointers."""

    def __init__(self, settings: KernelSettings):
        self.data_root = settings.data_root
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _root(self, project_id: str, task_id: str, document_kind: str) -> Path:
        if not SAFE_KIND.fullmatch(document_kind):
            raise ValueError("document kind must be a safe slug")
        return (
            self.runtime.ensure(project_id).vault_root / "runs" / task_id
            / "documents" / document_kind
        )

    def write_revision(
        self, *, project_id: str, task_id: str, document_kind: str,
        document_schema: str, payload: dict[str, Any], status: str,
        actor_type: str, actor_id: str, run_id: str | None,
        reason: str, source_refs: list[dict[str, Any]] | None = None,
        supersedes_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("document payload must be an object")
        if actor_type not in ACTOR_TYPES:
            raise ValueError("unsupported document actor_type")
        normalized_sources = [validate_resource_ref(item) for item in source_refs or []]
        normalized_supersedes = (
            validate_resource_ref(supersedes_ref) if supersedes_ref else None
        )
        document_id = f"{document_kind}:{task_id}"
        content_sha = hashlib.sha256(_canonical(payload)).hexdigest()
        revision_material = {"payload": payload, "status": str(status)}
        revision_id = "rev-" + hashlib.sha256(_canonical(revision_material)).hexdigest()[:20]
        root = self._root(project_id, task_id, document_kind)
        target = root / "revisions" / f"{revision_id}.json"
        if target.exists():
            encoded = target.read_bytes()
            existing = json.loads(encoded.decode("utf-8"))
            if (
                existing.get("content_sha256") != content_sha
                or existing.get("schema_version") != document_schema
                or existing.get("status") != str(status)
            ):
                raise RuntimeError("document revision collision")
            relative = target.relative_to(self.runtime.ensure(project_id).vault_root)
            return resource_ref(
                kind="document", identity=document_id,
                uri=f"vault://{project_id}/{relative.as_posix()}",
                schema_version=document_schema, revision_id=revision_id,
                sha256=hashlib.sha256(encoded).hexdigest(), purpose=document_kind,
            )
        created_at = datetime.now(timezone.utc).isoformat()
        document = {
            "schema_version": document_schema,
            "document_id": document_id,
            "project_id": project_id,
            "task_id": task_id,
            "run_id": str(run_id) if run_id else None,
            "revision_id": revision_id,
            "status": str(status),
            "created_at": created_at,
            "created_by": {
                "actor_type": actor_type,
                "actor_id": str(actor_id),
                "run_id": str(run_id) if run_id else None,
            },
            "reason": str(reason or ""),
            "source_refs": normalized_sources,
            "supersedes_ref": normalized_supersedes,
            "commit_id": None,
            "content_sha256": content_sha,
            "payload": payload,
        }
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, indent=2, default=str,
        ).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != encoded:
            raise RuntimeError("document revision collision")
        if not target.exists():
            temporary = target.with_suffix(target.suffix + f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(encoded)
            temporary.replace(target)
        relative = target.relative_to(self.runtime.ensure(project_id).vault_root)
        return resource_ref(
            kind="document", identity=document_id,
            uri=f"vault://{project_id}/{relative.as_posix()}",
            schema_version=document_schema, revision_id=revision_id,
            sha256=hashlib.sha256(encoded).hexdigest(), purpose=document_kind,
        )

    def read(self, project_id: str, ref: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_resource_ref(ref)
        prefix = f"vault://{project_id}/"
        uri = str(normalized.get("uri") or "")
        if not uri.startswith(prefix):
            raise PermissionError("document belongs to another Project")
        target = self.runtime.resolve_vault_file(project_id, uri[len(prefix):])
        encoded = target.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != normalized["sha256"]:
            raise ValueError("document revision file hash differs")
        document = json.loads(encoded.decode("utf-8"))
        payload = document.get("payload")
        if hashlib.sha256(_canonical(payload)).hexdigest() != document.get("content_sha256"):
            raise ValueError("document content hash differs")
        if document.get("revision_id") != normalized["revision_id"]:
            raise ValueError("document revision id differs")
        if (document.get("document_id") != normalized["id"]
                or document.get("schema_version") != normalized["document_schema"]
                or document.get("project_id") != project_id):
            raise ValueError("document reference identity or Schema differs")
        if document.get("envelope_version") == "doc-envelope.v3":
            from assistant_kernel.services.contracts import document_reader
            document_reader(self.data_root, document.get("schema_library_ref")).validate_document(document)
        return document

