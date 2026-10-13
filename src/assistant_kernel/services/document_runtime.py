"""Versioned semantic documents for one Project Task.

The filesystem owns immutable semantic content. PostgreSQL owns which
``resource-ref.v1`` revisions are accepted for the live Task. ``current``
files are rebuildable projections and never become a second state authority.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import os
import re
import uuid
from contextlib import contextmanager
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


def same_resource_ref(left: Any, right: Any) -> bool:
    """Description is not identity; every source/version/hash field still is."""
    if not isinstance(left,dict) or not isinstance(right,dict):return left is None and right is None
    keys=('schema_version','kind','id','uri','document_schema','revision_id','sha256')
    return all(left.get(key)==right.get(key) for key in keys)


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

    @staticmethod
    @contextmanager
    def _lock(target: Path):
        lock_path = target.with_suffix(target.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

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

    def write_scoped_revision(
        self, *, project_id: str, scope: str, object_id: str,
        document_kind: str, document_schema: str, payload: dict[str, Any],
        status: str, actor_type: str, actor_id: str, reason: str,
        task_id: str | None = None, run_id: str | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        supersedes_ref: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a strict immutable revision, without accepting or publishing it.

        Existing callers keep the historical writer until their adopted package
        explicitly selects this protocol. Database acceptance stays separate.
        """
        from assistant_kernel.services.contracts import canonical_json, contracts
        from copy import deepcopy
        payload = deepcopy(payload)
        registry = contracts()
        definition = registry.document_definition(document_schema)
        registry.validate(definition, payload)
        if not SAFE_KIND.fullmatch(document_kind) or not SAFE_KIND.fullmatch(object_id):
            raise ValueError("document kind and object id must be safe slugs")
        if actor_type not in ACTOR_TYPES or not reason.strip() or not actor_id:
            raise ValueError("document actor and reason are required")
        vault = self.runtime.describe(project_id).vault_root
        if scope == "project":
            if task_id is not None or run_id is not None:
                raise ValueError("Project document cannot claim a Task or Run owner")
            root = vault / "operations" / "documents" / document_kind / object_id
            owner = project_id + ":" + object_id
            document_id = document_kind + ":project:" + hashlib.sha256(owner.encode()).hexdigest()[:24]
        elif scope in {"task", "run"}:
            owner = task_id if scope == "task" else run_id
            if owner is None:
                raise ValueError("scoped document requires its real owner")
            uuid.UUID(owner)
            if task_id is not None:
                uuid.UUID(task_id)
            if run_id is not None:
                uuid.UUID(run_id)
            root = self._root(project_id, owner, document_kind)
            document_id = f"{document_kind}:{owner}"
        else:
            raise ValueError("unsupported document scope")
        sources = [validate_resource_ref(x) for x in source_refs or []]
        previous = validate_resource_ref(supersedes_ref) if supersedes_ref else None
        if previous:
            old = self.read(project_id, previous)
            if old["document_id"] != document_id or old["schema_version"] != document_schema:
                raise ValueError("document predecessor belongs to another object")
        stable = {
            "envelope_version": "doc-envelope.v3", "design_revision": "1.2",
            "schema_library_ref": registry.library_ref(), "payload_definition": definition,
            "schema_version": document_schema, "document_id": document_id,
            "scope": scope, "project_id": project_id, "task_id": task_id,
            "run_id": run_id, "status": status, "reason": reason,
            "created_by": {"actor_type": actor_type, "actor_id": actor_id, "run_id": run_id},
            "source_refs": sources, "supersedes_ref": previous, "commit_id": None,
            "content_sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
            "payload": payload,
        }
        revision = "rev-" + hashlib.sha256(canonical_json(stable)).hexdigest()[:24]
        document = {**stable, "revision_id": revision,
                    "created_at": datetime.now(timezone.utc).isoformat()}
        registry.validate_document(document)
        registry.preserve(self.data_root)
        target = root / "revisions" / (revision + ".json")
        # Linux is the supported execution host. Unlike the legacy projection
        # helper, the new writer must not silently continue without its lock.
        import fcntl
        target.parent.mkdir(parents=True, exist_ok=True)
        with (root / "writer.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if target.exists():
                encoded = target.read_bytes()
                existing = json.loads(encoded)
                if {k:v for k,v in existing.items() if k not in {"revision_id", "created_at"}} != stable:
                    raise RuntimeError("immutable document revision collision")
                registry.validate_document(existing)
            else:
                encoded = json.dumps(document, ensure_ascii=False, sort_keys=True,
                                     indent=2, allow_nan=False).encode("utf-8")
                temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(encoded)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        return resource_ref(
            kind="document", identity=document_id,
            uri=f"vault://{project_id}/{target.relative_to(vault).as_posix()}",
            schema_version=document_schema, revision_id=revision,
            sha256=hashlib.sha256(encoded).hexdigest(), purpose=document_kind,
        )

    def update_current(
        self, *, project_id: str, task_id: str, document_kind: str,
        ref: dict[str, Any], commit_id: str, state_revision: int,
    ) -> dict[str, Any]:
        normalized = validate_resource_ref(ref)
        # Required revisions are checked before the derived pointer can move.
        self.read(project_id, normalized)
        current = self._root(project_id, task_id, document_kind) / "current.json"
        projection = {
            "schema_version": "document-current.v1",
            "project_id": project_id,
            "task_id": task_id,
            "document_kind": document_kind,
            "state_revision": int(state_revision),
            "commit_id": str(commit_id),
            "ref": normalized,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        encoded = json.dumps(
            projection, ensure_ascii=False, sort_keys=True, indent=2,
        ).encode("utf-8")
        with self._lock(current):
            if current.exists():
                prior = json.loads(current.read_text(encoding="utf-8"))
                prior_revision = int(prior.get("state_revision") or 0)
                if prior_revision > int(state_revision):
                    raise RuntimeError("current document projection is newer")
                if prior_revision == int(state_revision) and prior.get("ref") != normalized:
                    raise RuntimeError("current document projection conflicts")
            current.parent.mkdir(parents=True, exist_ok=True)
            temporary = current.with_suffix(
                current.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            temporary.write_bytes(encoded)
            temporary.replace(current)
        return projection

    def current(self, project_id: str, task_id: str, document_kind: str) -> dict[str, Any] | None:
        target = self._root(project_id, task_id, document_kind) / "current.json"
        if not target.is_file():
            return None
        projection = json.loads(target.read_text(encoding="utf-8"))
        if projection.get("schema_version") != "document-current.v1":
            raise ValueError("current document pointer is invalid")
        return projection

    def change_receipt(self, project_id: str, ref: dict[str, Any], *,
                       previous_ref: dict[str, Any] | None = None,
                       purpose: str = "", state_revision: int | None = None,
                       context_epoch: int | None = None) -> dict[str, Any] | None:
        """Produce byte-verified revision evidence; this never accepts a document.

        Each revision is a new physical file. The diff and line counts compare
        logical document revisions when a predecessor exists, explicitly marked
        as such. History is retained while the Map navigates the latest head.
        """
        normalized = validate_resource_ref(ref)
        if normalized["kind"] != "document":
            raise ValueError("Kernel document receipt requires a document reference")
        document = self.read(project_id, normalized)
        if (document.get("document_id") != normalized["id"]
                or document.get("schema_version") != normalized.get("document_schema")
                or document.get("project_id") != project_id):
            raise ValueError("Kernel document receipt identity differs")
        previous = validate_resource_ref(previous_ref) if previous_ref else None
        if previous == normalized:
            return None
        prefix = f"vault://{project_id}/"
        old = b""
        if previous:
            prior = self.read(project_id, previous)
            if prior.get("document_id") != document["document_id"]:
                raise ValueError("document receipt predecessor belongs to another document")
            old = self.runtime.resolve_vault_file(project_id, previous["uri"][len(prefix):]).read_bytes()
        relative = normalized["uri"][len(prefix):]
        new = self.runtime.resolve_vault_file(project_id, relative).read_bytes()
        old_lines, new_lines = old.decode("utf-8").splitlines(True), new.decode("utf-8").splitlines(True)
        diff = "".join(difflib.unified_diff(old_lines, new_lines,
            fromfile=previous["uri"] if previous else "/dev/null", tofile=normalized["uri"]))
        identity = hashlib.sha256(json.dumps([previous, normalized], sort_keys=True).encode()).hexdigest()
        vault = self.runtime.describe(project_id).vault_root
        path = vault / "runtime" / "document-change-diffs" / f"{identity}.patch"
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = diff.encode("utf-8")
        if path.exists() and path.read_bytes() != encoded:
            raise RuntimeError("document diff is immutable")
        if not path.exists():
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(encoded); temporary.replace(path)
        operations = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes()
        return {
            "target_uri": f"workspace://project/{relative}", "operation": "create",
            "before_sha256": None, "after_sha256": hashlib.sha256(new).hexdigest(),
            "document_ref": normalized, "previous_document_ref": previous,
            "state_revision": state_revision, "context_epoch": context_epoch,
            "purpose": purpose or normalized.get("purpose") or str(document["document_id"]),
            "occurred_at": document.get("created_at"),
            "diff_basis": "previous_document_revision" if previous else "new_document_revision",
            "lines_added": sum(j2-j1 for tag, i1, i2, j1, j2 in operations if tag in {"insert", "replace"}),
            "lines_deleted": sum(i2-i1 for tag, i1, i2, j1, j2 in operations if tag in {"delete", "replace"}),
            "file_lines_added": len(new_lines), "file_lines_deleted": 0,
            "diff_ref": {"uri": f"vault://{project_id}/{path.relative_to(vault).as_posix()}",
                         "sha256": hashlib.sha256(encoded).hexdigest()},
        }
