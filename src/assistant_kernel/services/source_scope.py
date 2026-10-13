from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository, approval_decision
from assistant_kernel.services.project_grants import policy_sha256
from assistant_kernel.storage import ProjectRuntimeManager


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_SCAN_ENTRIES = 10_000
MAX_SCAN_SECONDS = 8.0
STRICT_EXCLUDES: tuple[str, ...] = ()
CONTROL_PLANE_EXCLUDES = (
    ".git", ".git/**", "assistant-data", "assistant-data/**",
    "assistant-kernel", "assistant-kernel/**", "docker-compose.yml",
    "tools", "tools/**",
    ".git.disabled-*", ".git.disabled-*/**", "**/.git.disabled-*",
    "**/.git.disabled-*/**",
    ".container-home", ".container-home/**",
)


class SourceScopeService:
    """Metadata-only discovery and explicit-request external workspace activation."""

    def __init__(self, settings: KernelSettings, repository: CoreRepository):
        self.settings = settings
        self.repository = repository
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.catalog_path = settings.data_root / "config" / "source-discovery.yaml"
        self.scopes_path = settings.data_root / "config" / "project-scopes.yaml"

    @staticmethod
    def _hash(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _patterns(values: Any) -> tuple[str, ...]:
        if not isinstance(values, list):
            return ()
        return tuple(
            str(item).replace("\\", "/").strip()
            for item in values if str(item).strip()
        )

    def _catalog(self) -> dict[str, dict[str, Any]]:
        data = (
            yaml.safe_load(self.catalog_path.read_text(encoding="utf-8")) or {}
            if self.catalog_path.is_file() else {}
        )
        if data.get("schema_version") != "source-discovery.v1":
            raise ValueError("source-discovery.yaml must use source-discovery.v1")
        result: dict[str, dict[str, Any]] = {}
        workspace = self.settings.workspace_root.resolve()
        for raw in data.get("roots") or []:
            discovery_id = str(raw.get("discovery_id") or "")
            if not SAFE_ID.fullmatch(discovery_id) or discovery_id in result:
                raise ValueError("source discovery IDs must be unique safe slugs")
            path = Path(str(raw.get("path") or "")).resolve()
            if path != workspace and workspace not in path.parents:
                raise PermissionError("source discovery root is outside AI workspace")
            if not path.is_dir():
                raise FileNotFoundError(f"source discovery root is missing: {discovery_id}")
            result[discovery_id] = {
                "discovery_id": discovery_id,
                "path": path,
                "display_path": str(raw.get("display_path") or path),
                "description": str(raw.get("description") or discovery_id),
                "excludes": self._patterns(raw.get("excludes")),
            }
        return result

    def list_roots(self) -> dict[str, Any]:
        return {
            "schema_version": "source-discovery-roots.v1",
            "roots": [
                {
                    "discovery_id": item["discovery_id"],
                    "display_path": item["display_path"],
                    "description": item["description"],
                }
                for item in self._catalog().values()
            ],
        }

    @staticmethod
    def _safe_relative(value: str) -> str:
        normalized = value.replace("\\", "/").strip().strip("/")
        relative = Path(normalized or ".")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("source path must stay below its discovery root")
        return "" if normalized in {"", "."} else relative.as_posix()

    @staticmethod
    def _matches(relative: str, patterns: tuple[str, ...]) -> bool:
        normalized = relative.replace("\\", "/").casefold()
        for pattern in patterns:
            pending = [pattern.replace("\\", "/").casefold()]
            variants: set[str] = set()
            while pending:
                candidate = pending.pop()
                if candidate in variants:
                    continue
                variants.add(candidate)
                if "**/" in candidate:
                    pending.append(candidate.replace("**/", "", 1))
            if any(fnmatch.fnmatchcase(normalized, candidate) for candidate in variants):
                return True
        return False

    def _resolve(self, discovery_id: str, relative_path: str) -> tuple[dict[str, Any], Path, str]:
        root = self._catalog().get(discovery_id)
        if root is None:
            raise KeyError(f"unknown source discovery root: {discovery_id}")
        relative = self._safe_relative(relative_path)
        if relative and self._matches(
            relative, (*CONTROL_PLANE_EXCLUDES, *root["excludes"])
        ):
            raise PermissionError("requested source path is excluded from discovery")
        target = (root["path"] / relative).resolve()
        if target != root["path"] and root["path"] not in target.parents:
            raise ValueError("source path escaped its discovery root")
        if not target.is_dir():
            raise FileNotFoundError("requested source directory does not exist")
        return root, target, relative

    @staticmethod
    def _git_state(target: Path) -> dict[str, Any]:
        def run(*args: str) -> str:
            try:
                completed = subprocess.run(
                    ["git", "-C", str(target), *args], capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return ""
            return completed.stdout.strip() if completed.returncode == 0 else ""

        root = run("rev-parse", "--show-toplevel")
        head = run("rev-parse", "HEAD") if root else ""
        status = run("status", "--short", "--", ".") if root else ""
        status_lines = [line for line in status.splitlines() if line.strip()]
        return {
            "kind": "git" if root and head else "unversioned",
            "repository_root": root or None,
            "head": head or None,
            "dirty": bool(status_lines),
            "dirty_count": len(status_lines),
            "dirty_sha256": (
                hashlib.sha256(status.encode("utf-8")).hexdigest()
                if status_lines else None
            ),
        }

    def inspect(
        self, discovery_id: str, relative_path: str = "", *,
        max_depth: int = 2, limit: int = 200, name_query: str = "",
    ) -> dict[str, Any]:
        root, target, relative = self._resolve(discovery_id, relative_path)
        max_depth = min(max(max_depth, 1), 4)
        limit = min(max(limit, 1), 500)
        query = name_query.strip().casefold()
        file_count = 0
        total_bytes = 0
        excluded_count = 0
        scanned = 0
        truncated = False
        scan_limit_reason: str | None = None
        entries_truncated = False
        extensions: dict[str, int] = {}
        entries: list[dict[str, Any]] = []
        excludes = (*CONTROL_PLANE_EXCLUDES, *root["excludes"], *STRICT_EXCLUDES)
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        stack = [target]
        while stack and not truncated:
            current_path = stack.pop()
            child_directories: list[Path] = []
            try:
                iterator = os.scandir(current_path)
            except OSError:
                excluded_count += 1
                continue
            with iterator:
                for entry in iterator:
                    if time.monotonic() >= deadline:
                        truncated = True
                        scan_limit_reason = "time_limit"
                        break
                    scanned += 1
                    if scanned > MAX_SCAN_ENTRIES:
                        truncated = True
                        scan_limit_reason = "entry_limit"
                        break
                    item = Path(entry.path)
                    item_relative = item.relative_to(target).as_posix()
                    try:
                        is_symlink = entry.is_symlink()
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        excluded_count += 1
                        continue
                    if is_symlink or self._matches(item_relative, excludes):
                        excluded_count += 1
                        continue
                    if is_directory:
                        child_directories.append(item)
                    depth = len(Path(item_relative).parts)
                    if depth <= max_depth and (not query or query in item_relative.casefold()):
                        if len(entries) < limit:
                            entries.append({
                                "relative_path": item_relative,
                                "type": "directory" if is_directory else "file",
                            })
                        else:
                            entries_truncated = True
                    if not is_directory:
                        file_count += 1
                        try:
                            total_bytes += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            pass
                        suffix = item.suffix.lower() or "[none]"
                        extensions[suffix] = extensions.get(suffix, 0) + 1
            stack.extend(sorted(child_directories, key=lambda item: item.name.casefold(), reverse=True))
        entries.sort(key=lambda item: str(item["relative_path"]).casefold())
        return {
            "schema_version": "source-inspection.v1",
            "discovery_id": discovery_id,
            "display_path": (
                str(root["display_path"]).rstrip("\\/")
                + ("\\" + relative.replace("/", "\\") if relative else "")
            ),
            "relative_path": relative,
            "metadata_only": True,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "excluded_entry_count": excluded_count,
            "scanned_entry_count": scanned,
            "scan_truncated": truncated,
            "scan_limit_reason": scan_limit_reason,
            "extensions": dict(sorted(extensions.items(), key=lambda item: (-item[1], item[0]))[:20]),
            "git": self._git_state(target),
            "entries": entries,
            "entries_truncated": truncated or entries_truncated,
        }

    def _validate_target(self, current_project_id: str, target_project_id: str) -> None:
        if target_project_id == "system":
            raise ValueError("source scope must target a Project, not the Butler")
        if current_project_id != "system" and target_project_id != current_project_id:
            raise PermissionError("a Project PM can request scope only for its own Project")
        directory = self.settings.data_root / "config" / "project-directory.yaml"
        data = yaml.safe_load(directory.read_text(encoding="utf-8")) or {}
        active = {
            str(item.get("project_id")) for item in data.get("projects") or []
            if str(item.get("status") or "active") == "active"
        }
        if target_project_id not in active:
            raise ValueError("target Project is not active")

    @staticmethod
    def _generated_root_key(path: Path) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.name).strip("-._") or "root"
        suffix = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
        return f"{stem[:80]}-{suffix}"

    def normalize_bindings(self, bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Resolve user-selected parent folders as recursive, unfiltered write roots."""
        if not bindings:
            return []
        normalized: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        seen_paths: set[str] = set()
        for raw in bindings:
            discovery_id = str(raw.get("discovery_id") or "")
            relative_path = str(raw.get("relative_path") or "")
            root, target, relative = self._resolve(discovery_id, relative_path)
            root_key = str(raw.get("root_key") or self._generated_root_key(target))
            if not SAFE_ID.fullmatch(root_key) or root_key in {"project", "candidate", "run_work"}:
                raise ValueError("root_key must be a safe non-reserved slug")
            resolved = str(target)
            if root_key in seen_keys or resolved in seen_paths:
                raise ValueError("scope proposal contains duplicate roots")
            seen_keys.add(root_key)
            seen_paths.add(resolved)
            display_path = str(root["display_path"]).rstrip("\\/") + (
                "\\" + relative.replace("/", "\\") if relative else ""
            )
            normalized.append({
                "root_key": root_key,
                "discovery_id": discovery_id,
                "relative_path": relative,
                "resolved_path": resolved,
                "display_path": display_path,
                "purpose": str(raw.get("purpose") or root_key).strip(),
                "access": "write",
                "write_mode": "direct",
                "native_mode": "enforced",
                "recursive": True,
                "includes": [],
                "excludes": [],
            })
        return normalized

    def propose(
        self, *, current_project_id: str, run_id: str, target_project_id: str,
        bindings: list[dict[str, Any]], reason: str,
        resume_blocked_run_id: str | None = None,
        authorization_mode: str = "explicit_request",
        assessment_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_target(current_project_id, target_project_id)
        if not reason.strip() or not bindings:
            raise ValueError("scope proposal requires a reason and at least one binding")
        if authorization_mode not in {"explicit_request", "explicit_approval"}:
            raise ValueError("unsupported source scope authorization_mode")
        normalized = self.normalize_bindings(bindings)
        proposal_base = {
            "schema_version": "source-scope-proposal.v1",
            "source_run_id": run_id,
            "requesting_project_id": current_project_id,
            "target_project_id": target_project_id,
            "reason": reason.strip(),
            "bindings": normalized,
            "resume_blocked_run_id": str(resume_blocked_run_id or "") or None,
            "authorization_mode": authorization_mode,
        }
        material_hash = self._hash(json.dumps(
            proposal_base, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"))
        relative = Path("runs") / run_id / f"source-scope-proposal-{material_hash[:16]}.yaml"
        target = self.runtime.ensure(current_project_id).vault_root / relative
        if target.is_file():
            existing = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            if any(existing.get(key) != value for key, value in proposal_base.items()):
                raise RuntimeError("source scope proposal path collision")
            existing_bytes = target.read_bytes()
            return {
                "proposal_uri": f"vault://{current_project_id}/{relative.as_posix()}",
                "proposal_sha256": self._hash(existing_bytes),
                "approval_question_id": str(existing["approval_question_id"]),
                "target_project_id": target_project_id,
                "bindings": normalized,
                "resume_blocked_run_id": str(resume_blocked_run_id or "") or None,
                "idempotent": True,
                "directory_disclosure": existing.get('directory_disclosure'),
                "directory_assessments": existing.get('directory_assessments',[]),
            }
        proposal = {
            **proposal_base,
            "approval_question_id": f"source-scope:{target_project_id}:{material_hash[:16]}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        from assistant_kernel.services.directory_assessment import assess_bindings,directory_disclosure
        from assistant_kernel.services.document_runtime import resource_ref
        assessments=assess_bindings(target_project_id,normalized,context='expand',
            source_refs=[resource_ref(kind='source',identity=run_id,uri='db://assistant/runs/'+run_id,
                                     purpose='directory expansion proposal; not approval')],parameters=assessment_parameters)
        proposal.update(directory_assessments=assessments,directory_disclosure=directory_disclosure(assessments),
                        presentation_version='directory-disclosure.v1')
        encoded = yaml.safe_dump(
            proposal, allow_unicode=True, sort_keys=False,
        ).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(target)
        return {
            "proposal_uri": f"vault://{current_project_id}/{relative.as_posix()}",
            "proposal_sha256": self._hash(encoded),
            "approval_question_id": proposal["approval_question_id"],
            "target_project_id": target_project_id,
            "bindings": normalized,
            "resume_blocked_run_id": str(resume_blocked_run_id or "") or None,
            "idempotent": False,
            "directory_disclosure": proposal['directory_disclosure'],
            "directory_assessments": assessments,
        }

    @staticmethod
    def _contains_path(parent: Path, child: Path) -> bool:
        return parent == child or parent in child.parents

    async def apply_full_directory_grants(
        self, *, connection: Any, target_project_id: str,
        bindings: list[dict[str, Any]], source_binding: dict[str, Any],
        source_message_id: str | None, approval_message_id: str | None,
    ) -> dict[str, Any]:
        """Apply recursive roots, replacing narrower child grants atomically in DB."""
        normalized = self.normalize_bindings(bindings)
        rows = list(await (await connection.execute(
            """
            SELECT grant_id,resource_key,policy_json,policy_sha256
            FROM assistant.project_grants
            WHERE project_id=%s AND grant_kind='file' AND subject_kind='project'
              AND status='active' FOR UPDATE
            """,
            (target_project_id,),
        )).fetchall())
        grant_ids: list[str] = []
        superseded_grant_ids: list[str] = []
        for binding in normalized:
            _, resolved_target, resolved_relative = self._resolve(
                str(binding.get("discovery_id") or ""),
                str(binding.get("relative_path") or ""),
            )
            if (
                str(resolved_target) != str(binding.get("resolved_path") or "")
                or resolved_relative != str(binding.get("relative_path") or "")
            ):
                raise PermissionError(
                    "source path changed after proposal; create a new scope proposal"
                )
            target_path = resolved_target.resolve()
            covering = None
            for row in rows:
                policy = dict(row.get("policy_json") or {})
                existing_path = Path(str(policy.get("path") or "")).resolve()
                full_recursive = (
                    not list(policy.get("excludes") or [])
                    and list(policy.get("includes") or []) in ([], ["**/*"])
                    and str(policy.get("access") or "write") == "write"
                    and policy.get('recursive') is True
                    and policy.get('write_mode') == 'direct'
                    and policy.get('native_mode') in {'enforced', 'behavioral'}
                )
                if full_recursive and self._contains_path(existing_path, target_path):
                    covering = row
                    break
            if covering is not None:
                grant_ids.append(str(covering["grant_id"]))
                continue

            revoke_rows = []
            for row in rows:
                policy = dict(row.get("policy_json") or {})
                existing_path = Path(str(policy.get("path") or "")).resolve()
                if (
                    str(row["resource_key"]) == str(binding["root_key"])
                    or self._contains_path(target_path, existing_path)
                ):
                    revoke_rows.append(row)
            for row in revoke_rows:
                await connection.execute(
                    """
                    UPDATE assistant.project_grants
                    SET status='revoked',revoked_at=now()
                    WHERE grant_id=%s AND status='active'
                    """,
                    (row["grant_id"],),
                )
                superseded_grant_ids.append(str(row["grant_id"]))
                rows.remove(row)

            root_entry = {
                "key": str(binding["root_key"]),
                "path": str(target_path),
                "description": str(binding.get("purpose") or binding["root_key"]),
                "access": "write",
                "write_mode": "direct",
                "native_mode": "enforced",
                "publish": True,
                "recursive": True,
                "includes": [],
                "excludes": [],
                "source_binding": dict(source_binding),
            }
            digest = policy_sha256(root_entry)
            grant_id = str(uuid.uuid4())
            await connection.execute(
                """
                INSERT INTO assistant.project_grants(
                    grant_id,project_id,grant_kind,resource_key,subject_kind,
                    policy_json,policy_sha256,source_message_id,approval_message_id
                ) VALUES (%s,%s,'file',%s,'project',%s,%s,%s,%s)
                """,
                (
                    grant_id, target_project_id, root_entry["key"],
                    Jsonb(root_entry), digest, source_message_id,
                    approval_message_id,
                ),
            )
            grant_ids.append(grant_id)
            rows.append({
                "grant_id": grant_id,
                "resource_key": root_entry["key"],
                "policy_json": root_entry,
                "policy_sha256": digest,
            })

        data = (
            yaml.safe_load(self.scopes_path.read_text(encoding="utf-8")) or {}
            if self.scopes_path.is_file() else {}
        )
        data["schema_version"] = "workspace-scope.v1"
        projects = data.setdefault("projects", {})
        target_config = projects.setdefault(target_project_id, {
            "roots": [{
                "key": "project", "path": "{project_root}",
                "description": "当前项目 Vault", "access": "write",
            }]
        })
        base_roots = [
            item for item in (target_config.get("roots") or [])
            if not item.get("source_binding")
        ]
        active = list(await (await connection.execute(
            """
            SELECT policy_json FROM assistant.project_grants
            WHERE project_id=%s AND grant_kind='file' AND subject_kind='project'
              AND status='active' ORDER BY resource_key
            """,
            (target_project_id,),
        )).fetchall())
        target_config["roots"] = [*base_roots, *[
            dict(row["policy_json"] or {}) for row in active
        ]]
        payload = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")
        temporary = self.scopes_path.with_suffix(".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        temporary.replace(self.scopes_path)
        return {
            "bindings": normalized,
            "grant_ids": grant_ids,
            "superseded_grant_ids": superseded_grant_ids,
            "scope_payload": payload,
        }

    async def activate(
        self, *, current_project_id: str, run_id: str, worker_instance_id: str,
        claim_token: str, proposal_uri: str, expected_sha256: str,
    ) -> dict[str, Any]:
        prefix = f"vault://{current_project_id}/"
        if not proposal_uri.startswith(prefix):
            raise ValueError("source scope proposal belongs to another Project")
        proposal_path = self.runtime.resolve_vault_file(
            current_project_id, proposal_uri[len(prefix):]
        )
        encoded = proposal_path.read_bytes()
        if self._hash(encoded) != expected_sha256:
            raise ValueError("source scope proposal hash does not match")
        proposal = yaml.safe_load(encoded.decode("utf-8")) or {}
        if proposal.get("schema_version") != "source-scope-proposal.v1":
            raise ValueError("unsupported source scope proposal")
        if str(proposal.get("source_run_id")) != run_id:
            raise ValueError("source scope proposal belongs to another Run")
        target_project_id = str(proposal.get("target_project_id") or "")
        self._validate_target(current_project_id, target_project_id)
        question_id = str(proposal.get("approval_question_id") or "")
        resume_blocked_run_id = str(proposal.get("resume_blocked_run_id") or "")
        approval_message_id = None
        async with self.repository.database.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("workspace-scope-config",),
            )
            run = await (await connection.execute(
                """
                SELECT r.*,m.channel AS trigger_channel,m.user_id AS approver_user_id,
                       m.direction AS trigger_direction,m.message_type AS trigger_type,
                       m.message_id AS source_message_id,m.content_json AS trigger_content
                FROM assistant.runs r
                JOIN assistant.messages m ON m.message_id=r.trigger_message_id
                WHERE r.run_id=%s AND r.worker_instance_id=%s AND r.claim_token=%s
                  AND r.status='running' FOR UPDATE OF r
                """,
                (run_id, worker_instance_id, claim_token),
            )).fetchone()
            if run is None:
                raise PermissionError("the scoped Run is no longer leased")
            if resume_blocked_run_id:
                blocked = await (await connection.execute(
                    "SELECT project_id,status,result_json FROM assistant.runs WHERE run_id=%s",
                    (resume_blocked_run_id,),
                )).fetchone()
                requested_action = str(
                    (((blocked or {}).get("result_json") or {}).get("permission_request") or {}).get("action") or ""
                )
                if (
                    blocked is None or blocked["project_id"] != target_project_id
                    or blocked["status"] != "blocked"
                    or requested_action not in {"workspace", "read"}
                ):
                    raise PermissionError(
                        "a file Scope grant cannot resume a Stage blocked on a different action"
                    )
            answer = dict((run["request_json"] or {}).get("answer") or {})
            approval_message_id = (run["request_json"] or {}).get("answer_message_id")
            selected = (answer.get("answers") or {}).get(question_id)
            selected_text = str(
                (selected or {}).get("text") if isinstance(selected, dict) else selected or ""
            ).strip()
            authorization_message_id = None
            authorization_mode = str(
                proposal.get("authorization_mode") or "explicit_request"
            )
            if proposal.get('presentation_version')=='directory-disclosure.v1' and (
                    authorization_mode=='explicit_approval' or any(a['warning_reasons'] for a in proposal.get('directory_assessments',[]))):
                from assistant_kernel.services.proposal_presentations import require_disclosed_subject
                await require_disclosed_subject(connection,run_id=run_id,answer_message_id=approval_message_id,
                    question_id=question_id,proposal_uri=proposal_uri,proposal_sha256=expected_sha256)
                if (selected_text not in {'接受现状','批准扩权'}
                        and approval_decision({'text':selected_text})!='approve'):
                    raise PermissionError('Directory warning was not accepted; do not grant or clean automatically')
            if authorization_mode == "explicit_approval":
                if selected_text not in {"批准扩权", "批准只读授权"} and approval_decision(
                    {"text": selected_text}
                ) != "approve":
                    raise PermissionError("the current answer does not approve this source scope")
            elif authorization_mode == "explicit_request":
                candidate = run
                if run["trigger_type"] == "handoff":
                    linked_id = str((run["trigger_content"] or {}).get("source_message_id") or "")
                    candidate = await (await connection.execute(
                        "SELECT message_id AS source_message_id,direction AS trigger_direction,"
                        " message_type AS trigger_type,content_json AS trigger_content,"
                        " user_id AS approver_user_id FROM assistant.messages WHERE message_id=%s",
                        (linked_id,),
                    )).fetchone() if linked_id else None
                if (
                    candidate is None or candidate["trigger_direction"] != "inbound"
                    or candidate["trigger_type"] not in {"text", "answer"}
                    or candidate["approver_user_id"] != run["approver_user_id"]
                ):
                    raise PermissionError(
                        "a Stage handoff is not itself a user workspace authorization; "
                        "obtain an explicit approval linked to this proposal"
                    )
                authorization_message_id = candidate["source_message_id"]
                user_text = str((candidate["trigger_content"] or {}).get("text") or "")
                bound_confirmation=False
                if proposal.get('presentation_version')=='directory-disclosure.v1':
                    # Mentioning a folder (or requesting a one-off read) is not
                    # a long-lived full-file grant. Only an explicit full grant
                    # can reuse the original instruction without another card.
                    direct_full=bool(re.search(r'(?:授权|授予|开放|允许)[^。；;!?？！\n]{0,100}(?:完整文件权限|完整权限|全部文件权限)|(?:grant|give|allow)[^.?!\n]{0,100}full (?:file )?access',user_text,re.IGNORECASE))
                    negated=bool(re.search(r'(?:不要|禁止|不允许|尚未)[^，,。；;!?？！\n]{0,20}(?:授权|完整权限)|(?:do not|don.t|never)\s+(?:grant|give|allow)',user_text,re.IGNORECASE))
                    if not direct_full or negated:
                        from assistant_kernel.services.proposal_presentations import require_disclosed_subject
                        await require_disclosed_subject(connection,run_id=run_id,answer_message_id=approval_message_id,
                            question_id=question_id,proposal_uri=proposal_uri,proposal_sha256=expected_sha256)
                        if selected_text not in {'接受现状','批准扩权'} and approval_decision({'text':selected_text})!='approve':
                            raise PermissionError('The displayed full directory agreement was not approved')
                        bound_confirmation=True
                        authorization_mode='explicit_approval'
                        authorization_message_id=approval_message_id
                normalized_text = user_text.replace("\\", "/").casefold()
                for binding in ([] if bound_confirmation else proposal.get("bindings") or []):
                    names = (
                        str(binding.get("display_path") or ""),
                        str(binding.get("resolved_path") or ""),
                    )
                    matched = False
                    for name in names:
                        named=name.replace('\\','/').rstrip('/').casefold()
                        # A mentioned child path does not authorize its parent,
                        # and a shared suffix does not identify another drive.
                        if named and re.search(re.escape(named)+r'(?![/A-Za-z0-9._-])',normalized_text):matched=True
                        if matched:
                            break
                    if not matched:
                        raise PermissionError(
                            "the user message did not name this folder; approve the exact proposal"
                        )
                approval_message_id = approval_message_id or str(authorization_message_id)
            else:
                raise ValueError("unsupported source scope authorization mode")
            applied = await self.apply_full_directory_grants(
                connection=connection,
                target_project_id=target_project_id,
                bindings=list(proposal.get("bindings") or []),
                source_binding={
                    "proposal_uri": proposal_uri,
                    "proposal_sha256": expected_sha256,
                    "approval_question_id": question_id,
                    "answer_message_id": approval_message_id,
                    "authorization_mode": authorization_mode,
                },
                source_message_id=(
                    str(authorization_message_id) if authorization_message_id else None
                ),
                approval_message_id=(
                    str(approval_message_id) if approval_message_id else None
                ),
            )
            grant_ids = list(applied["grant_ids"])
            payload = bytes(applied["scope_payload"])
            await connection.commit()
        receipt_relative = (
            Path("runtime") / "source-scope-receipts" /
            f"{expected_sha256}.json"
        )
        receipt_path = self.runtime.ensure(target_project_id).vault_root / receipt_relative
        if receipt_path.is_file():
            existing_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (
                existing_receipt.get("proposal_sha256") != expected_sha256
                or existing_receipt.get("target_project_id") != target_project_id
            ):
                raise RuntimeError("source scope receipt conflicts with this proposal")
            if list(existing_receipt.get("grant_ids") or []) != grant_ids:
                raise ValueError("revoked grant requires a new user request and proposal")
            receipt_sha256 = self._hash(receipt_path.read_bytes())
            resumed = (
                await self.repository.resume_blocked_stage_with_permission(
                    blocked_run_id=resume_blocked_run_id,
                    permission_run_id=run_id,
                    permission_receipt_uri=(
                        f"vault://{target_project_id}/{receipt_relative.as_posix()}"
                    ),
                    permission_receipt_sha256=receipt_sha256,
                    approval_message_id=str(approval_message_id or "") or None,
                )
                if resume_blocked_run_id else None
            )
            return {
                "target_project_id": target_project_id,
                "status": "active",
                "bindings": existing_receipt.get("bindings") or [],
                "grant_ids": existing_receipt.get("grant_ids") or [],
                "receipt_uri": f"vault://{target_project_id}/{receipt_relative.as_posix()}",
                "receipt_sha256": receipt_sha256,
                "effective_next_run": True,
                "idempotent": True,
                "resumed_stage_run_id": str(resumed["run_id"]) if resumed else None,
            }
        receipt = {
            "schema_version": "source-scope-receipt.v1",
            "target_project_id": target_project_id,
            "activation_run_id": run_id,
            "proposal_uri": proposal_uri,
            "proposal_sha256": expected_sha256,
            "approval_question_id": question_id,
            "authorization_mode": authorization_mode,
            "bindings": proposal.get("bindings") or [],
            "grant_ids": grant_ids,
            "scope_config_sha256": self._hash(payload),
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "effective_next_run": True,
        }
        receipt_bytes = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, indent=2,
        ).encode("utf-8")
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        if receipt_path.exists() and receipt_path.read_bytes() != receipt_bytes:
            raise RuntimeError("source scope receipt hash collision")
        if not receipt_path.exists():
            temporary = receipt_path.with_suffix(".tmp")
            temporary.write_bytes(receipt_bytes)
            temporary.replace(receipt_path)
        resumed = (
            await self.repository.resume_blocked_stage_with_permission(
                blocked_run_id=resume_blocked_run_id,
                permission_run_id=run_id,
                permission_receipt_uri=f"vault://{target_project_id}/{receipt_relative.as_posix()}",
                permission_receipt_sha256=self._hash(receipt_bytes),
                approval_message_id=str(approval_message_id or "") or None,
            )
            if resume_blocked_run_id else None
        )
        return {
            "target_project_id": target_project_id,
            "status": "active",
            "bindings": proposal.get("bindings") or [],
            "grant_ids": grant_ids,
            "receipt_uri": f"vault://{target_project_id}/{receipt_relative.as_posix()}",
            "receipt_sha256": self._hash(receipt_bytes),
            "effective_next_run": True,
            "idempotent": False,
            "resumed_stage_run_id": str(resumed["run_id"]) if resumed else None,
        }
