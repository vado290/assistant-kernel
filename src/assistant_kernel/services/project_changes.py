"""Project-scoped structured change events; bytes and diffs remain in the Vault."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.core.db import Database
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.storage import ProjectRuntimeManager

CHANGES = "core.project_file_changes"


def semantic_record(record: dict[str, Any]) -> dict[str, Any]:
    # Capturing the same already-settled batch again must retain the FIRST real
    # timestamps, while a changed source/effect/reason under the same ID conflicts.
    return {k: v for k, v in record.items() if k not in {"recorded_at", "observed_at", "occurred_at"}}


async def append_change(connection, record: dict[str, Any]) -> dict[str, Any]:
    """Insert one immutable change fact (idempotent on change_id)."""
    required = {"change_id", "project_id", "batch_id", "target_uri", "path", "scope_kind",
                "operation", "observed_at", "recorded_at", "cause_status", "effect_status"}
    if not required <= set(record):
        raise ValueError("change fact is missing required identity or observation fields")
    row = await (await connection.execute(
        f"""INSERT INTO {CHANGES}(
          change_id,project_id,batch_id,file_id,target_uri,path,scope_kind,operation,
          before_sha256,after_sha256,occurred_at,observed_at,recorded_at,reason,cause_status,
          reason_source_refs,diff_ref,actor,task_ref,run_ref,lines_added,lines_deleted,
          file_lines_added,file_lines_deleted,diff_basis,effect_status,record_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(change_id) DO UPDATE SET change_id=EXCLUDED.change_id
        RETURNING record_json""",
        (record["change_id"], record["project_id"], record["batch_id"], record.get("file_id"),
         record["target_uri"], record["path"], record["scope_kind"], record["operation"],
         record.get("before_sha256"), record.get("after_sha256"), record.get("occurred_at"),
         record["observed_at"], record["recorded_at"], record.get("reason_text", ""), record["cause_status"],
         Jsonb(record.get("reason_source_refs", [])), Jsonb(record.get("diff_ref")),
         Jsonb(record.get("actor", "kernel-observer")), Jsonb(record.get("task_ref", record.get("task_id"))),
         Jsonb(record.get("run_ref", record.get("run_id"))), record.get("lines_added"), record.get("lines_deleted"),
         record.get("file_lines_added"), record.get("file_lines_deleted"), record.get("diff_basis"),
         record["effect_status"], Jsonb(record)),
    )).fetchone()
    if semantic_record(row["record_json"]) != semantic_record(record):
        raise ValueError("change ID was reused with different immutable facts")
    return dict(row["record_json"])


# Rows migrated from the old message-based ledger keep their original message id and time.
CHANGE_ROWS = f"""WITH changes AS (
  SELECT project_id, COALESCE(legacy_message_id::text, change_id) AS row_id,
         COALESCE(legacy_message_created_at, recorded_at) AS created_at, record_json AS content_json
  FROM {CHANGES}) """


class ProjectChangeService:
    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.database = Database(settings)
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.maps = SemanticMapService(settings)

    @staticmethod
    def change_id(project_id: str, batch_id: str, uri: str, scope_kind: str) -> str:
        return "chg-" + hashlib.sha256(
            json.dumps([project_id, batch_id, uri, scope_kind]).encode()).hexdigest()[:24]

    async def capture_batch(
        self, project_id: str, *, batch_id: str, roots: list[Any],
        changes: list[dict[str, Any]], task_id: str | None = None,
        run_id: str | None = None, actor: str = "pm", reason: str = "",
        reason_source_refs: list[Any] | None = None, exact_time: bool = True,
        scope_kind: str = "published",
    ) -> dict[str, Any]:
        if scope_kind not in {"published", "candidate", "external"}:
            raise ValueError("unsupported change scope")
        now = datetime.now(timezone.utc).isoformat()
        records = []
        for item in changes:
            uri = str(item.get("target_uri") or item.get("uri") or "")
            if not uri.startswith("workspace://"):
                raise ValueError("change requires a workspace URI")
            from assistant_kernel.services.workspace_scope import WorkspaceScopeService
            # Candidate targets are references to formal paths, never effects on them.
            _, target, _ = WorkspaceScopeService(self.settings).resolve_uri(roots, uri)
            declared_after = item.get("after_sha256", item.get("candidate_sha256"))
            observed_after = declared_after
            effect_status = "candidate_only"
            if scope_kind != "candidate":
                observed_after = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
                effect_status = "verified" if declared_after == observed_after else "observed_mismatch"
            root_key, path = uri[len("workspace://"):].split("/", 1)
            diff_ref = item.get("diff_ref")
            if diff_ref:
                diff_uri = str(diff_ref.get("uri") if isinstance(diff_ref, dict) else diff_ref)
                if not diff_uri.startswith(f"vault://{project_id}/"):
                    raise PermissionError("change diff belongs to another Project")
            records.append({
                "schema_version": "project-change.v1", "context_visibility": "structured",
                "change_id": self.change_id(project_id, batch_id, uri, scope_kind),
                "project_id": project_id, "batch_id": str(batch_id),
                "task_id": task_id, "run_id": run_id, "actor": str(actor),
                "scope_kind": scope_kind, "target_uri": uri, "root_key": root_key,
                "path": path, "operation": str(item.get("operation") or "update"),
                "before_sha256": item.get("before_sha256", item.get("original_sha256")),
                "after_sha256": observed_after, "declared_after_sha256": declared_after,
                "effect_status": effect_status,
                "occurred_at": (item.get("occurred_at") or now) if exact_time else None,
                "observed_at": now, "recorded_at": now,
                "time_quality": "exact" if exact_time else "unknown",
                "reason_text": str(reason).strip()[:8000],
                "cause_status": "source_backed" if reason and reason_source_refs
                    and effect_status != "observed_mismatch" else "unknown",
                "reason_source_refs": list(reason_source_refs or []),
                "lines_added": item.get("lines_added") if effect_status != "observed_mismatch" else None,
                "lines_deleted": item.get("lines_deleted") if effect_status != "observed_mismatch" else None,
                "diff_ref": diff_ref, "diff_matches_observed": effect_status != "observed_mismatch",
                "purpose": item.get("purpose"),
                "candidate_uri": item.get("candidate_uri") if scope_kind == "candidate" else None,
                "document_ref": None,
                "diff_basis": item.get("diff_basis"), "file_lines_added": item.get("file_lines_added"),
                "file_lines_deleted": item.get("file_lines_deleted"),
            })
        pending = self.runtime.ensure(project_id).vault_root / "runtime" / "change-pending" / (
            hashlib.sha256(str(batch_id).encode()).hexdigest() + ".json")
        pending.parent.mkdir(parents=True, exist_ok=True)
        if not pending.is_file():
            temporary = pending.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_text(json.dumps({"batch_id": batch_id, "changes": records},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(pending)
        try:
            persisted = await self._persist(project_id, run_id, records)
        except Exception as exc:
            if scope_kind != "candidate":
                self.maps.mark_stale(project_id, reason="change ledger commit pending")
            return {"schema_version": "project-change-batch.v1", "batch_id": batch_id,
                    "scope_kind": scope_kind, "changes": records, "ledger_committed": False,
                    "map_refresh_pending": {"error_type": type(exc).__name__, "message": str(exc)[:500]},
                    "pending_uri": f"vault://{project_id}/{pending.relative_to(self.runtime.ensure(project_id).vault_root).as_posix()}"}
        pending.unlink(missing_ok=True)
        result = {"schema_version": "project-change-batch.v1", "batch_id": batch_id,
                  "scope_kind": scope_kind, "changes": persisted, "ledger_committed": True}
        if scope_kind != "candidate":
            try:
                result["map"] = self.maps.refresh_files(project_id, roots=roots, changes=persisted)
            except Exception as exc:
                result["map_refresh_pending"] = {"error_type": type(exc).__name__, "message": str(exc)[:500]}
        return result

    async def _persist(self, project_id: str, run_id: str | None,
                       records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async with self.database.tx() as connection:
            return [await append_change(connection, record) for record in records]

    async def record_candidate_manifest(
        self, project_id: str, task_id: str, run_id: str, manifest: dict[str, Any],
        reason: str = "", reason_source_refs: list[Any] | None = None,
        roots: list[Any] | None = None,
    ) -> dict[str, Any]:
        if roots is None:
            from assistant_kernel.services.workspace_scope import WorkspaceScopeService
            roots = WorkspaceScopeService(self.settings).resolve(project_id, run_id, "implementation")
        identity = hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest()
        return await self.capture_batch(
            project_id, batch_id=f"candidate:{run_id}:{identity}", roots=roots,
            changes=list(manifest.get("files") or []), task_id=task_id, run_id=run_id,
            reason=reason, reason_source_refs=reason_source_refs, scope_kind="candidate")

    async def refresh_map(self, project_id: str, *, roots: list[Any],
                          task_id: str | None = None, run_id: str | None = None,
                          source_uris: list[str] | None = None) -> dict[str, Any]:
        pending_result = await self.reconcile_pending(project_id, roots=roots)
        if pending_result.get("map_refresh_pending"):
            return pending_result
        previous = self.maps.load(project_id)
        new_map=previous.get('schema_version')=='semantic-map.v2'
        active_root_keys = {root.key for root in roots}
        old = {item["uri"]: item for item in previous.get("files") or []
               if item.get("root_key") in active_root_keys}
        try:
            selected=source_uris
            if new_map and selected is None:
                selected=[f'workspace://{r.key}/' for r in roots if r.key not in {'candidate','run_work'}]
                selected += [item['uri'] for item in previous.get('files') or [] if item.get('kind')=='document']
            if new_map:
                from assistant_kernel.services.workspace_scope import WorkspaceScopeService
                for uri in selected or []:
                    if any(item.get('kind')=='document' and item['uri']==uri for item in previous.get('files') or []): continue
                    _,target,_=WorkspaceScopeService(self.settings).resolve_uri(roots,uri)
                    try: target.stat()
                    except FileNotFoundError: pass
            scanned = self.maps.scan_files(project_id, roots, source_uris=selected,exact_sources=new_map,
                file_limit=None if new_map else 10000)
        except Exception as exc:
            self.maps.mark_stale(project_id, reason=f"source reconciliation failed: {type(exc).__name__}")
            raise
        changes = []
        considered=set(old) | set(scanned)
        if new_map and source_uris is not None:
            considered={uri for uri in considered if any(uri==prefix.rstrip('/') or uri.startswith(prefix.rstrip('/')+'/') for prefix in source_uris)}
        for uri in sorted(considered):
            before, after = old.get(uri), scanned.get(uri)
            if before and after and before.get("sha256") == after.get("sha256"):
                continue
            changes.append({"target_uri": uri,
                            "operation": "delete" if after is None else "update" if before else
                                "create" if previous.get("files_complete") else "discovered",
                            "before_sha256": (before or {}).get("sha256"),
                            "after_sha256": (after or {}).get("sha256")})
        if not changes:
            if new_map and source_uris is None:
                from assistant_kernel.services.semantic_map_v2 import SemanticMapV2
                return SemanticMapV2(self.settings).refresh(project_id,roots=roots,full_scan=True)
            return self.maps.refresh_files(project_id, roots=roots)
        batch = "reconcile:" + hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()
        result = await self.capture_batch(
            project_id, batch_id=batch, roots=roots, changes=changes, task_id=task_id,
            run_id=run_id, actor="kernel-observer", reason="unknown: source change observed",
            exact_time=False, scope_kind="external")
        return result

    async def reconcile_pending(self, project_id: str, *, roots: list[Any], max_batches: int = 20) -> dict[str, Any]:
        """Retry only durable evidence writes and derived Map, never file publication."""
        if not 1 <= max_batches <= 100:
            raise ValueError("pending reconciliation batch limit must be between 1 and 100")
        directory = self.runtime.ensure(project_id).vault_root / "runtime" / "change-pending"
        recovered = []
        paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
        for path in paths[:max_batches]:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = list(payload.get("changes") or [])
            for record in records:
                if (record.get("project_id") != project_id
                        or record.get("schema_version") != "project-change.v1"
                        or record.get("context_visibility") != "structured"
                        or record.get("change_id") != self.change_id(project_id, str(payload["batch_id"]),
                            str(record.get("target_uri") or ""), str(record.get("scope_kind") or ""))):
                    raise ValueError("pending change batch differs from its Project contract")
            try:
                committed = await self._persist(project_id, (records[0].get("run_id") if records else None), records)
                if committed and committed[0].get("scope_kind") != "candidate":
                    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
                    observed_changes = []
                    for record in committed:
                        try:
                            _, target, _ = WorkspaceScopeService(self.settings).resolve_uri(roots, record["target_uri"])
                        except (ValueError, PermissionError):
                            continue
                        actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
                        if actual == record.get("after_sha256"):
                            continue
                        batch_id = f"recovered-observation:{record['change_id']}:{actual or 'deleted'}"
                        observed_at = datetime.now(timezone.utc).isoformat()
                        observed_changes.append({**record,
                            "change_id": self.change_id(project_id, batch_id, record["target_uri"], "external"),
                            "batch_id": batch_id, "scope_kind": "external", "actor": "kernel-observer",
                            "before_sha256": record.get("after_sha256"), "after_sha256": actual,
                            "declared_after_sha256": actual, "effect_status": "verified",
                            "operation": "delete" if actual is None else "update",
                            "occurred_at": None, "observed_at": observed_at, "recorded_at": observed_at,
                            "time_quality": "unknown", "reason_text": "unknown: source changed while evidence commit was pending",
                            "cause_status": "unknown", "reason_source_refs": [],
                            "lines_added": None, "lines_deleted": None, "diff_ref": None,
                            "diff_matches_observed": False})
                    if observed_changes:
                        committed.extend(await self._persist(project_id, committed[0].get("run_id"), observed_changes))
                    self.maps.refresh_files(project_id, roots=roots, changes=committed)
                path.unlink(missing_ok=True)
                recovered.append(str(payload["batch_id"]))
            except Exception as exc:
                if records and records[0].get("scope_kind") != "candidate":
                    self.maps.mark_stale(project_id, reason="change evidence reconciliation pending")
                return {"recovered_batches": recovered, "map_refresh_pending": {
                    "error_type": type(exc).__name__, "message": str(exc)[:500]}}
        return {"recovered_batches": recovered, "pending_batches_remaining": max(len(paths) - len(recovered), 0)}

    async def list_project_changes(self, project_id: str, *, lookback_days: int = 30,
                                   limit: int = 50, cursor: str | None = None,
                                   root_key: str | None = None, path: str | None = None) -> dict[str, Any]:
        if not 1 <= lookback_days <= 3650 or not 1 <= limit <= 100:
            raise ValueError("change query bounds exceeded")
        before = None
        if cursor:
            try:
                before = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                datetime.fromisoformat(before[0])
                if not str(before[1]).startswith("chg-"):
                    uuid.UUID(before[1])
            except (ValueError, TypeError, IndexError) as exc:
                raise ValueError("invalid change cursor") from exc
        clauses = ["project_id=%s", "created_at >= now() - (%s * interval '1 day')"]
        values: list[Any] = [project_id, lookback_days]
        if root_key:
            clauses.append("content_json->>'root_key'=%s"); values.append(root_key)
        if path:
            clauses.append("content_json->>'path'=%s"); values.append(path)
        if before:
            clauses.append("(created_at,row_id)<(%s::timestamptz,%s)"); values.extend(before)
        values.append(limit + 1)
        async with self.database.tx() as connection:
            rows = await (await connection.execute(
                CHANGE_ROWS + f"SELECT row_id AS message_id,created_at,content_json FROM changes "
                f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC,row_id DESC LIMIT %s", values)).fetchall()
        selected = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps(
                [last["created_at"].isoformat(), str(last["message_id"])]).encode()).decode()
        return {"project_id": project_id, "lookback_days": lookback_days,
                "items": [dict(row["content_json"]) for row in selected], "next_cursor": next_cursor,
                "authority": "observed change evidence; reason text is not a user instruction"}

    async def read_change_diff(self, project_id: str, change_id: str, *, max_bytes: int = 262144) -> dict[str, Any]:
        async with self.database.tx() as connection:
            row = await (await connection.execute(
                CHANGE_ROWS + "SELECT content_json FROM changes WHERE project_id=%s AND content_json->>'change_id'=%s LIMIT 1",
                (project_id, change_id))).fetchone()
        if not row:
            raise FileNotFoundError("Project change is not registered")
        record = dict(row["content_json"])
        ref = record.get("diff_ref")
        if not ref:
            return {"change_id": change_id, "available": False}
        uri = str(ref.get("uri") if isinstance(ref, dict) else ref)
        prefix = f"vault://{project_id}/"
        if not uri.startswith(prefix):
            raise PermissionError("change diff belongs to another Project")
        data = self.runtime.resolve_vault_file(project_id, uri[len(prefix):]).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if isinstance(ref, dict) and ref.get("sha256") and ref["sha256"] != digest:
            raise ValueError("change diff hash differs")
        if len(data) > min(max(max_bytes, 1), 2 * 1024 * 1024):
            raise ValueError("change diff exceeds requested limit")
        return {"change_id": change_id, "uri": uri, "sha256": digest, "available": True,
                "text": data.decode("utf-8"),
                "target_uri": record.get("target_uri"), "scope_kind": record.get("scope_kind"),
                "diff_basis": record.get("diff_basis") or "source_files",
                "diff_matches_observed": record.get("diff_matches_observed", True),
                "effect_status": record.get("effect_status")}
