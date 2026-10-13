from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.workspace_scope import WorkspaceScopeService


SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git", "skill-revisions"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _line_delta(old: bytes | None, new: bytes | None) -> dict[str, Any]:
    try:
        if b"\x00" in (old or b"") or b"\x00" in (new or b""):
            raise UnicodeError("binary")
        operations = difflib.SequenceMatcher(
            a=(old or b"").decode("utf-8").splitlines(True),
            b=(new or b"").decode("utf-8").splitlines(True), autojunk=False).get_opcodes()
        return {"lines_added": sum(j2-j1 for tag, i1, i2, j1, j2 in operations if tag in {"insert", "replace"}),
                "lines_deleted": sum(i2-i1 for tag, i1, i2, j1, j2 in operations if tag in {"delete", "replace"})}
    except UnicodeError:
        return {"lines_added": None, "lines_deleted": None}


class CapabilityBundleService:
    """Inspect, version and publish Project-local Skill bundles.

    Active files keep the Codex-compatible ``.agents/skills/<name>`` location.
    Immutable revisions are stored beside the active Skill, not inside it, so
    discovery never confuses historical files with the active capability.
    """

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)

    @staticmethod
    def validate_name(name: str) -> str:
        normalized = str(name or "").strip()
        if not SKILL_NAME.fullmatch(normalized):
            raise ValueError("skill name must be a safe slug")
        return normalized

    def active_root(self, project_id: str, name: str) -> Path:
        name = self.validate_name(name)
        vault = self.runtime.ensure(project_id).vault_root
        return self.runtime.resolve_below(vault, f".agents/skills/{name}")

    @staticmethod
    def bundle_files(root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        files: list[Path] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            if relative.as_posix() == "skill-manifest.json":
                continue
            files.append(path)
        return files

    @classmethod
    def describe_root(cls, root: Path) -> dict[str, Any]:
        entries = []
        for path in cls.bundle_files(root):
            data = path.read_bytes()
            entries.append({
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(data),
                "size": len(data),
            })
        digest = _sha256(_json_bytes(entries))
        return {"bundle_hash": digest, "files": entries}

    def inspect(self, project_id: str, name: str) -> dict[str, Any]:
        name = self.validate_name(name)
        root = self.active_root(project_id, name)
        skill_path = root / "SKILL.md"
        manifest_path = root / "skill-manifest.json"
        if not root.exists():
            mode = "create"
            description = {"bundle_hash": None, "files": []}
        elif not skill_path.is_file():
            mode = "repair"
            description = self.describe_root(root)
        else:
            mode = "iterate"
            description = self.describe_root(root)
        active_revision = None
        manifest_valid = False
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                active_revision = manifest.get("active_revision")
                manifest_valid = manifest.get("name") == name
            except (OSError, json.JSONDecodeError):
                mode = "repair"
        return {
            "schema_version": "capability-baseline.v1",
            "kind": "skill",
            "name": name,
            "change_mode": mode,
            "exists": root.exists(),
            "skill_md_exists": skill_path.is_file(),
            "manifest_valid": manifest_valid,
            "active_revision": active_revision,
            **description,
        }

    def _revision_root(self, project_id: str, name: str, revision_id: str) -> Path:
        vault = self.runtime.ensure(project_id).vault_root
        return self.runtime.resolve_below(
            vault, f".agents/skill-revisions/{self.validate_name(name)}/{revision_id}"
        )

    def _write_revision(
        self, project_id: str, name: str, *, parent_revision: str | None,
        source_run_id: str, review_run_id: str | None, approval_message_id: str | None,
        change_mode: str, before: dict[str, bytes], after_root: Path,
    ) -> tuple[str, str, str]:
        description = self.describe_root(after_root)
        revision_id = description["bundle_hash"]
        revision_root = self._revision_root(project_id, name, revision_id)
        snapshot = revision_root / "snapshot"
        revision_root.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            shutil.copytree(after_root, snapshot)

        after = {
            path.relative_to(after_root).as_posix(): path.read_bytes()
            for path in self.bundle_files(after_root)
        }
        patch_parts: list[str] = []
        for relative in sorted(set(before) | set(after)):
            old = before.get(relative, b"").decode("utf-8", "replace").splitlines(True)
            new = after.get(relative, b"").decode("utf-8", "replace").splitlines(True)
            patch_parts.extend(difflib.unified_diff(
                old, new,
                fromfile=f"a/{relative}" if relative in before else "/dev/null",
                tofile=f"b/{relative}" if relative in after else "/dev/null",
            ))
        parent_key = (parent_revision or "empty")[:32]
        diff_path = revision_root / "diffs" / f"from-{parent_key}.patch"
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        if not diff_path.exists():
            diff_path.write_text("".join(patch_parts), encoding="utf-8")
        revision_metadata = {
            "schema_version": "skill-revision.v1",
            "revision_id": revision_id,
            "project_id": project_id,
            "name": name,
            "files": description["files"],
        }
        revision_path = revision_root / "revision.json"
        if revision_path.exists():
            existing = json.loads(revision_path.read_text(encoding="utf-8"))
            if existing != revision_metadata:
                raise RuntimeError("immutable Skill revision metadata changed")
        else:
            revision_path.write_text(
                json.dumps(revision_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        publication = {
            "schema_version": "skill-publication.v1",
            "revision_id": revision_id,
            "parent_revision": parent_revision,
            "change_mode": change_mode,
            "source_run_id": source_run_id,
            "review_run_id": review_run_id,
            "approval_message_id": approval_message_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "diff": diff_path.name,
        }
        publication_key = re.sub(
            r"[^A-Za-z0-9._-]+", "-", approval_message_id or source_run_id or revision_id
        )
        publication_path = revision_root / "publications" / f"{publication_key}.json"
        publication_path.parent.mkdir(parents=True, exist_ok=True)
        if not publication_path.exists():
            publication_path.write_text(
                json.dumps(publication, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        vault = self.runtime.ensure(project_id).vault_root
        revision_uri = (
            f"vault://{project_id}/"
            f"{revision_path.relative_to(vault).as_posix()}"
        )
        patch_uri = (
            f"vault://{project_id}/"
            f"{diff_path.relative_to(vault).as_posix()}"
        )
        return revision_id, revision_uri, patch_uri

    def publish(
        self, *, project_id: str, publish_run_id: str, request: dict[str, Any],
        cancel_event: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("publication cancelled before file effects")
        change = dict(request.get("capability_change") or {})
        if not change:
            return self._publish_files(
                project_id=project_id, publish_run_id=publish_run_id, request=request,
                cancel_event=cancel_event,
            )
        name = self.validate_name(str(change.get("target_skill") or ""))
        mode = str(change.get("change_mode") or "")
        if mode not in {"create", "iterate", "repair"}:
            raise ValueError("capability change mode is invalid")
        baseline = dict(change.get("baseline") or {})
        active_root = self.active_root(project_id, name)
        current = self.inspect(project_id, name)
        if mode == "create" and current["skill_md_exists"]:
            raise RuntimeError("target Skill was created after the task baseline")
        expected_bundle = baseline.get("bundle_hash")
        if mode in {"iterate", "repair"} and current["bundle_hash"] != expected_bundle:
            raise RuntimeError("active Skill changed after the task baseline")

        manifest_uri = str(request.get("candidate_manifest_uri") or "")
        prefix = f"vault://{project_id}/"
        if not manifest_uri.startswith(prefix):
            raise ValueError("publish run has no Project-local candidate manifest")
        manifest_path = self.runtime.resolve_vault_file(project_id, manifest_uri[len(prefix):])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        candidate_root = manifest_path.parent
        entries = [dict(item) for item in manifest.get("files") or []]
        if not entries:
            raise ValueError("candidate manifest is empty")

        expected_prefix = f"workspace://project/.agents/skills/{name}/"
        before = {
            path.relative_to(active_root).as_posix(): path.read_bytes()
            for path in self.bundle_files(active_root)
        }
        if (active_root / "SKILL.md").is_file():
            baseline_revision, _, _ = self._write_revision(
                project_id, name, parent_revision=None,
                source_run_id=f"baseline:{publish_run_id}", review_run_id=None,
                approval_message_id=None, change_mode="baseline",
                before=before, after_root=active_root,
            )
            if baseline_revision != current["bundle_hash"]:
                raise RuntimeError("baseline Skill revision hash is inconsistent")
        publish_root = self.runtime.ensure(project_id).vault_root / "runs" / publish_run_id
        proposed = publish_root / "proposed-skill"
        if proposed.exists():
            shutil.rmtree(proposed)
        if active_root.is_dir():
            shutil.copytree(active_root, proposed)
        else:
            proposed.mkdir(parents=True, exist_ok=True)

        for entry in entries:
            target_uri = str(entry.get("target_uri") or "")
            if not target_uri.startswith(expected_prefix):
                raise PermissionError("candidate contains a target outside the selected Skill")
            relative = target_uri[len(expected_prefix):]
            if not relative or ".." in Path(relative).parts or Path(relative).is_absolute():
                raise ValueError("candidate target path is invalid")
            target = active_root / relative
            actual_hash = _sha256(target.read_bytes()) if target.is_file() else None
            operation = str(entry.get("operation") or (
                "update" if entry.get("original_sha256") is not None else "create"
            ))
            expected_hash = entry.get("original_sha256")
            if operation == "create" and target.exists():
                raise RuntimeError(f"candidate expected an absent target: {relative}")
            if operation in {"update", "delete"} and actual_hash != expected_hash:
                raise RuntimeError(f"candidate baseline changed: {relative}")
            proposed_target = proposed / relative
            if operation == "delete":
                if proposed_target.exists():
                    proposed_target.unlink()
                continue
            candidate_uri = str(entry.get("candidate_uri") or "")
            expected_candidate = "workspace://candidate/"
            if not candidate_uri.startswith(expected_candidate):
                raise ValueError("candidate file URI is invalid")
            candidate_relative = candidate_uri[len(expected_candidate):]
            candidate_path = candidate_root / candidate_relative
            data = candidate_path.read_bytes()
            if _sha256(data) != entry.get("candidate_sha256"):
                raise ValueError("candidate content hash changed")
            proposed_target.parent.mkdir(parents=True, exist_ok=True)
            proposed_target.write_bytes(data)

        if not (proposed / "SKILL.md").is_file():
            raise ValueError("published Skill bundle must contain SKILL.md")
        proposed_description = self.describe_root(proposed)
        parent_revision = current.get("active_revision") or current.get("bundle_hash")
        revision_id = proposed_description["bundle_hash"]
        skill_manifest = {
            "schema_version": "skill-bundle.v1",
            "name": name,
            "active_revision": revision_id,
            "files": proposed_description["files"],
        }
        (proposed / "skill-manifest.json").write_text(
            json.dumps(skill_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        journal_path = publish_root / "publication-journal.json"
        journal = {
            "schema_version": "publication-journal.v1", "status": "prepared",
            "project_id": project_id, "target_skill": name,
            "baseline_bundle_hash": baseline.get("bundle_hash"),
            "candidate_bundle_hash": revision_id,
            "candidate_manifest_uri": manifest_uri,
        }
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        backup = publish_root / "previous-skill"
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("publication cancelled before file effects")
        if backup.exists():
            shutil.rmtree(backup)
        if active_root.exists():
            active_root.replace(backup)
        try:
            proposed.replace(active_root)
            journal["status"] = "files_published"
            journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            if backup.exists() and not active_root.exists():
                backup.replace(active_root)
            raise

        try:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("publication cancelled; rolling back Skill effects")
            revision_id, revision_uri, patch_uri = self._write_revision(
                project_id, name, parent_revision=parent_revision,
                source_run_id=str(request.get("implementation_run_id") or ""),
                review_run_id=str(request.get("review_run_id") or "") or None,
                approval_message_id=str(request.get("approval_message_id") or "") or None,
                change_mode=mode, before=before, after_root=active_root,
            )
        except Exception:
            failed = publish_root / "failed-proposed-skill"
            if failed.exists():
                shutil.rmtree(failed)
            if active_root.exists():
                active_root.replace(failed)
            if backup.exists():
                backup.replace(active_root)
            raise
        result = {
            "schema_version": "capability-publication.v1",
            "outcome": "published",
            "target_skill": name,
            "change_mode": mode,
            "parent_revision": parent_revision,
            "published_revision": revision_id,
            "revision_uri": revision_uri,
            "diff_uri": patch_uri,
        }
        patch_path = self.runtime.resolve_vault_file(project_id, patch_uri[len(f"vault://{project_id}/"):])
        result["map_change_batch"] = {
            "batch_id": f"publisher:{publish_run_id}",
            "task_id": str(request.get("implementation_run_id") or publish_run_id),
            "run_id": publish_run_id, "actor": "publisher", "scope_kind": "published",
            "changes": [{**entry,
                **_line_delta(before.get(str(entry["target_uri"])[len(expected_prefix):]),
                              (active_root / str(entry["target_uri"])[len(expected_prefix):]).read_bytes()
                              if (active_root / str(entry["target_uri"])[len(expected_prefix):]).is_file() else None),
                "diff_ref": {"uri": patch_uri, "sha256": _sha256(patch_path.read_bytes())}}
                for entry in entries],
        }
        artifacts = [
            {"uri": revision_uri, "kind": "skill_revision"},
            {"uri": patch_uri, "kind": "skill_diff"},
        ]
        journal["status"] = "completed"
        journal["published_revision"] = revision_id
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        return result, artifacts

    def _publish_files(
        self, *, project_id: str, publish_run_id: str, request: dict[str, Any],
        cancel_event: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        manifest_uri = str(request.get("candidate_manifest_uri") or "")
        prefix = f"vault://{project_id}/"
        if not manifest_uri.startswith(prefix):
            raise ValueError("publish run has no Project-local candidate manifest")
        manifest_path = self.runtime.resolve_vault_file(project_id, manifest_uri[len(prefix):])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = [dict(item) for item in manifest.get("files") or []]
        bundle_uri = str(request.get("candidate_bundle_uri") or "")
        bundle = None
        if bundle_uri:
            if not bundle_uri.startswith(prefix):
                raise ValueError("candidate bundle belongs to another Project")
            bundle_path = self.runtime.resolve_vault_file(
                project_id, bundle_uri[len(prefix):]
            )
            bundle_bytes = bundle_path.read_bytes()
            expected_bundle_file_hash = str(
                request.get("candidate_bundle_file_sha256") or ""
            )
            if (
                expected_bundle_file_hash
                and _sha256(bundle_bytes) != expected_bundle_file_hash
            ):
                raise ValueError("candidate bundle file hash changed")
            bundle = json.loads(bundle_bytes.decode("utf-8"))
            if bundle.get("schema_version") != "candidate-bundle.v1":
                raise ValueError("unsupported candidate bundle")
            entries = [dict(item) for item in bundle.get("files") or []]
        if not entries:
            raise ValueError("candidate manifest is empty")
        implementation_run_id = str(request.get("implementation_run_id") or "")
        task_protocol = str(request.get("task_protocol") or "")
        scope_stage = None if task_protocol == "single-agent.v2" else "implementation"
        scopes = WorkspaceScopeService(self.settings)
        resolved_root = (
            self.runtime.ensure(project_id).vault_root / "runs" /
            implementation_run_id / "resolved"
        )
        # Single-agent Task Runs pin per-mode plans (execution-plan-build.json
        # etc.); the publish request's own variant may be "audit" after a cold
        # audit, so resolve the build plan explicitly and never the audit one.
        plan_names = (
            ["execution-plan-build.json", "execution-plan.json"]
            if task_protocol == "single-agent.v2" else
            ["execution-plan.json"]
        )
        plan_path = next(
            (
                resolved_root / name for name in plan_names
                if (resolved_root / name).is_file()
            ),
            resolved_root / "execution-plan.json",
        )
        if not plan_path.is_file():
            raise ValueError("approved Implementation has no pinned execution plan")
        implementation_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        bindings = implementation_plan.get("bindings") or {}
        pinned_roots = scopes.load_snapshot(
            project_id, implementation_run_id, scope_stage,
            str(bindings.get("workspace_scope_snapshot_uri") or ""),
            str(bindings.get("workspace_scope_snapshot_sha256") or ""),
        )
        current_roots = scopes.resolve(
            project_id, implementation_run_id, scope_stage
        )
        formal_roots = [root for root in current_roots if root.key != "candidate"]
        approved_roots = [root for root in pinned_roots if root.key != "candidate"]
        candidate_root = manifest_path.parent
        prepared: list[dict[str, Any]] = []
        for entry in entries:
            target_uri = str(entry.get("target_uri") or "")
            approved_root, approved_target, _ = scopes.resolve_uri(
                approved_roots, target_uri
            )
            root, target, relative = scopes.resolve_uri(
                formal_roots, target_uri
            )
            if (
                target != approved_target
                or root.grant_id != approved_root.grant_id
                or root.grant_sha256 != approved_root.grant_sha256
            ):
                raise PermissionError("publication target grant changed after Implementation")
            if not root.publish_allowed:
                raise PermissionError(
                    f"workspace root is not approved for publication: {root.key}"
                )
            actual_hash = _sha256(target.read_bytes()) if target.is_file() else None
            operation = str(entry.get("operation") or (
                "update" if entry.get("original_sha256") is not None else "create"
            ))
            expected_hash = entry.get("original_sha256")
            if operation == "create" and target.exists():
                raise RuntimeError(f"candidate expected an absent target: {target_uri}")
            if operation in {"update", "delete"} and actual_hash != expected_hash:
                raise RuntimeError(f"candidate baseline changed: {target_uri}")
            candidate_path = None
            data = None
            if operation != "delete":
                artifact_uri = str(entry.get("artifact_uri") or "")
                if bundle is not None:
                    if not artifact_uri.startswith(prefix):
                        raise ValueError("bundle candidate artifact URI is invalid")
                    candidate_path = self.runtime.resolve_vault_file(
                        project_id, artifact_uri[len(prefix):]
                    )
                else:
                    candidate_uri = str(entry.get("candidate_uri") or "")
                    if not candidate_uri.startswith("workspace://candidate/"):
                        raise ValueError("candidate file URI is invalid")
                    candidate_path = candidate_root / candidate_uri[len("workspace://candidate/"):]
                data = candidate_path.read_bytes()
                if _sha256(data) != entry.get("candidate_sha256"):
                    raise ValueError("candidate content hash changed")
            prepared.append({
                **entry, "root_key": root.key, "relative": relative,
                "target": target, "data": data, "operation": operation,
            })

        vault = self.runtime.ensure(project_id).vault_root
        publication_root = vault / "runs" / publish_run_id / "publication"
        backup_root = publication_root / "backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        journal_path = publication_root / "journal.json"
        journal = {
            "schema_version": "publication-journal.v1", "status": "prepared",
            "project_id": project_id, "publish_run_id": publish_run_id,
            "candidate_manifest_uri": manifest_uri, "applied_target_uris": [],
            "files": [
                {
                    key: value for key, value in item.items()
                    if key in {"operation", "target_uri", "original_sha256", "candidate_sha256"}
                }
                for item in prepared
            ],
        }
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        applied: list[dict[str, Any]] = []
        patch_parts: list[str] = []
        try:
            for item in prepared:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("publication cancelled; rolling back file effects")
                target = Path(item["target"])
                relative = str(item["relative"])
                old_data = target.read_bytes() if target.is_file() else None
                item["map_line_delta"] = _line_delta(old_data, item["data"])
                if old_data is not None:
                    backup = backup_root / str(item["root_key"]) / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(old_data)
                new_data = item["data"]
                old_lines = (old_data or b"").decode("utf-8", "replace").splitlines(True)
                new_lines = (new_data or b"").decode("utf-8", "replace").splitlines(True)
                patch_parts.extend(difflib.unified_diff(
                    old_lines, new_lines,
                    fromfile=f"a/{item['root_key']}/{relative}" if old_data is not None else "/dev/null",
                    tofile=f"b/{item['root_key']}/{relative}" if new_data is not None else "/dev/null",
                ))
                if item["operation"] == "delete":
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_suffix(target.suffix + f".{publish_run_id}.tmp")
                    temporary.write_bytes(new_data)
                    temporary.replace(target)
                applied.append(item)
                journal["status"] = "applying"
                journal["applied_target_uris"].append(item["target_uri"])
                temporary_journal = journal_path.with_suffix(".tmp")
                temporary_journal.write_text(
                    json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                temporary_journal.replace(journal_path)
        except Exception:
            for item in reversed(applied):
                target = Path(item["target"])
                backup = backup_root / str(item["root_key"]) / str(item["relative"])
                if backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    backup.replace(target)
                elif target.exists():
                    target.unlink()
            raise

        try:
            for item in prepared:
                target = Path(item["target"])
                if item["operation"] == "delete":
                    if target.exists():
                        raise RuntimeError("published deletion did not take effect")
                elif _sha256(target.read_bytes()) != item.get("candidate_sha256"):
                    raise RuntimeError("published file hash does not match candidate")
        except Exception:
            for item in reversed(applied):
                target = Path(item["target"])
                backup = backup_root / str(item["root_key"]) / str(item["relative"])
                if backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    backup.replace(target)
                elif target.exists():
                    target.unlink()
            raise
        diff_path = publication_root / "changes.patch"
        diff_path.write_text("".join(patch_parts), encoding="utf-8")
        map_state = "not_applicable"
        map_generation = None
        map_error = None
        map_index = vault / "knowledge" / "map" / "index.yaml"
        if map_index.is_file():
            try:
                map_generation = SemanticMapService(self.settings).publish_current(project_id)
                map_state = "published_complete"
            except Exception as exc:
                map_state = "published_map_pending"
                map_error = {"type": type(exc).__name__, "message": str(exc)[:500]}
        receipt = {
            "schema_version": "file-publication.v1",
            "outcome": (
                "published_map_pending" if map_state == "published_map_pending"
                else "published"
            ),
            "project_id": project_id,
            "publish_run_id": publish_run_id,
            "implementation_run_id": implementation_run_id,
            "review_run_id": request.get("review_run_id"),
            "approval_message_id": request.get("approval_message_id"),
            "candidate_manifest_uri": manifest_uri,
            "files": [
                {
                    key: value for key, value in item.items()
                    if key in {"operation", "target_uri", "original_sha256", "candidate_sha256"}
                }
                for item in prepared
            ],
            "published_at": datetime.now(timezone.utc).isoformat(),
            "map_state": map_state,
            "map_generation": map_generation,
            "map_error": map_error,
        }
        receipt_path = publication_root / "publication.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        journal["status"] = "completed"
        journal_path.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
        receipt_uri = f"vault://{project_id}/{receipt_path.relative_to(vault).as_posix()}"
        diff_uri = f"vault://{project_id}/{diff_path.relative_to(vault).as_posix()}"
        result = {**receipt, "publication_uri": receipt_uri, "diff_uri": diff_uri}
        result["map_change_batch"] = {
            "batch_id": f"publisher:{publish_run_id}",
            "changes": [
                {**{key: value for key, value in item.items() if key in {
                    "operation", "target_uri", "original_sha256", "candidate_sha256"}},
                 **item.get("map_line_delta", {}),
                 "diff_ref": {"uri": diff_uri, "sha256": _sha256(diff_path.read_bytes())}}
                for item in prepared
            ],
            "task_id": implementation_run_id,
            "run_id": publish_run_id,
            "actor": "publisher", "scope_kind": "published",
        }
        return result, [
            {"uri": receipt_uri, "kind": "publication_receipt"},
            {"uri": diff_uri, "kind": "publication_diff"},
        ]
