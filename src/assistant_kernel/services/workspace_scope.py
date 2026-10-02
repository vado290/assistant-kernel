from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.project_grants import ProjectGrantService
from assistant_kernel.storage import ProjectRuntimeManager


DEFAULT_TREE_EXCLUDES = (
    "runs", "runs/**", "runtime", "runtime/**",
    ".pytest_cache", ".pytest_cache/**", "**/.pytest_cache", "**/.pytest_cache/**",
    "__pycache__", "__pycache__/**", "**/__pycache__", "**/__pycache__/**",
    "*.egg-info", "*.egg-info/**", "**/*.egg-info", "**/*.egg-info/**",
    "docs/archive", "docs/archive/**",
)

DEFAULT_CONTEXT_SOFT_EXCLUDES = (
    ".git", ".git/**", "**/.git", "**/.git/**",
    ".venv", ".venv/**", "**/.venv", "**/.venv/**",
    "node_modules", "node_modules/**", "**/node_modules", "**/node_modules/**",
    "build", "build/**", "**/build", "**/build/**",
    "dist", "dist/**", "**/dist", "**/dist/**",
    "logs", "logs/**", "**/logs", "**/logs/**",
    "archive", "archive/**", "**/archive", "**/archive/**",
)
TREE_SCAN_MAX_ENTRIES = 10_000
TREE_SCAN_MAX_SECONDS = 5.0


@dataclass(frozen=True)
class AuthorizedRoot:
    key: str
    path: Path
    access: str
    description: str
    includes: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    publish_allowed: bool = True
    grant_id: str | None = None
    grant_sha256: str | None = None
    write_mode: str = "direct"
    native_mode: str = "enforced"
    recursive: bool = False


class WorkspaceScopeService:
    """Resolve file roots from Kernel-owned configuration, never model arguments."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.path = settings.data_root / "config" / "project-scopes.yaml"
        self.grants = ProjectGrantService(settings)

    def configuration_hash(self, project_id: str) -> str:
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        projects = (data or {}).get("projects") or {}
        selected = projects.get(project_id) or projects.get("default") or {}
        base_roots = [
            ({**item, "access": "write"} if str(item.get("key") or "") == "project" else item)
            for item in (selected.get("roots") or [])
            if not item.get("source_binding") and (
                project_id == "system" or item.get("key") == "project"
                or str(item.get("access") or "read") == "read"
            )
        ]
        if project_id != "system" and not any(
            str(item.get("key") or "") == "project" for item in base_roots
        ):
            base_roots.insert(0, {
                "key": "project", "path": "{project_root}",
                "description": "当前项目 Vault", "access": "write",
            })
        source_map = self._source_map(project_id)
        payload = json.dumps(
            {
                "base_roots": base_roots,
                "grants": self.grants.active_file_roots(project_id),
                "source_map": source_map,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _source_map(self, project_id: str) -> dict[str, Any]:
        if project_id == "system":
            return {"schema_version": "project-source-map.v1", "roots": {}}
        path = self.runtime.ensure(project_id).vault_root / "source-map.yaml"
        if not path.is_file():
            return {"schema_version": "project-source-map.v1", "roots": {}}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict) or data.get("schema_version") != "project-source-map.v1":
            raise ValueError("source-map.yaml must use project-source-map.v1")
        roots = data.get("roots") or {}
        if not isinstance(roots, dict):
            raise ValueError("source-map.yaml roots must be an object")
        return {"schema_version": "project-source-map.v1", "roots": roots}

    @staticmethod
    def _safe_context_path(value: str) -> str:
        normalized = value.replace("\\", "/").strip("/")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("context path must stay below its authorized root")
        return path.as_posix() if normalized else ""

    def context_view(
        self, project_id: str, roots: list[AuthorizedRoot], *, request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve a compact per-Run view without changing the Project authorization ceiling."""
        source_map = self._source_map(project_id)
        configured = source_map.get("roots") or {}
        explicit: dict[str, list[str]] = {}
        for raw in (request or {}).get("context_paths") or []:
            value = str(raw)
            if not value.startswith("workspace://"):
                raise ValueError("context_paths entries must be workspace URIs")
            remainder = value[len("workspace://"):]
            key, separator, relative = remainder.partition("/")
            if not separator or not key:
                raise ValueError("context_paths entry is incomplete")
            explicit.setdefault(key, []).append(self._safe_context_path(relative))

        unknown_keys = sorted(set(explicit) - {root.key for root in roots})
        if unknown_keys:
            raise PermissionError(
                "context_paths reference unauthorized roots: " + ", ".join(unknown_keys)
            )

        views: list[dict[str, Any]] = []
        for root in roots:
            raw = configured.get(root.key) or {}
            if not isinstance(raw, dict):
                raise ValueError(f"source-map root must be an object: {root.key}")
            internal = root.key in {"project", "run_work", "candidate"}
            mode = str(raw.get("mode") or ("live" if internal else "discovery"))
            if mode not in {"live", "discovery", "lazy_snapshot", "indexed"}:
                raise ValueError(f"unsupported source-map mode for {root.key}")
            if not isinstance(raw.get("entrypoints") or [], list):
                raise ValueError(f"source-map entrypoints must be a list: {root.key}")
            if not isinstance(raw.get("soft_excludes") or [], list):
                raise ValueError(f"source-map soft_excludes must be a list: {root.key}")
            entrypoints = [
                self._safe_context_path(str(item))
                for item in (raw.get("entrypoints") or [])
                if str(item).strip()
            ]
            entrypoints.extend(explicit.get(root.key) or [])
            if internal and "" not in entrypoints:
                entrypoints.insert(0, "")
            entrypoints = list(dict.fromkeys(entrypoints))
            soft_excludes = [
                str(item).replace("\\", "/").strip()
                for item in (raw.get("soft_excludes") or DEFAULT_CONTEXT_SOFT_EXCLUDES)
                if str(item).strip()
            ]
            views.append({
                "key": root.key,
                "mode": mode,
                "initial_paths": entrypoints,
                "soft_excludes": soft_excludes,
            })
        return {
            "schema_version": "context-view.v1",
            "source_map_uri": (
                None if project_id == "system" else f"vault://{project_id}/source-map.yaml"
            ),
            "roots": views,
        }

    def resolve(
        self, project_id: str, run_id: str, stage: str | None,
        *, pinned_at_creation: bool = False, candidate_only: bool = False,
        candidate_enabled: bool = False,
    ) -> list[AuthorizedRoot]:
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        projects = (data or {}).get("projects") or {}
        selected = projects.get(project_id) or projects.get("default") or {}
        project_root = self.runtime.ensure(project_id).vault_root.resolve()
        replacements = {
            "{project_root}": str(project_root),
            "{workspace_root}": str(self.settings.workspace_root.resolve()),
        }
        roots: list[AuthorizedRoot] = []
        # Approved external roots are read from the grant ledger.  Legacy YAML
        # projections carrying source_binding are deliberately not authority.
        entries = [
            item for item in (selected.get("roots") or [])
            if not item.get("source_binding") and (
                project_id == "system" or item.get("key") == "project"
                or str(item.get("access") or "read") == "read"
            )
        ]
        entries.extend(self.grants.active_file_roots(
            project_id, run_id if pinned_at_creation else None
        ))
        if project_id != "system" and not any(
            str(item.get("key") or "") == "project" for item in entries
        ):
            entries.insert(0, {
                "key": "project", "path": "{project_root}",
                "description": "当前项目 Vault", "access": "write",
                "write_mode": "direct", "native_mode": "enforced",
            })
        for item in entries:
            raw = str(item.get("path") or "")
            for marker, value in replacements.items():
                raw = raw.replace(marker, value)
            path = Path(raw).resolve()
            if not path.exists():
                continue
            includes = tuple(
                str(pattern).replace("\\", "/").strip()
                for pattern in (item.get("includes") or [])
                if str(pattern).strip()
            )
            excludes = tuple(
                str(pattern).replace("\\", "/").strip()
                for pattern in (item.get("excludes") or [])
                if str(pattern).strip()
            )
            formal_access = (
                "read" if candidate_only and project_id != "system"
                else (
                    "write" if str(item.get("key") or "") == "project"
                    else str(item.get("access") or "read")
                )
            )
            roots.append(AuthorizedRoot(
                key=str(item["key"]), path=path,
                access=formal_access,
                description=str(item.get("description") or item["key"]),
                includes=includes, excludes=excludes,
                publish_allowed=bool(item.get("publish", True)),
                grant_id=str(item.get("grant_id") or "") or None,
                grant_sha256=str(item.get("grant_sha256") or "") or None,
                write_mode=(
                    "gated" if candidate_only and project_id != "system"
                    else str(item.get("write_mode") or "direct")
                ),
                native_mode=str(item.get("native_mode") or "enforced"),
                recursive=bool(item.get("recursive", False)),
            ))
            if roots[-1].access not in {"read", "write"}:
                raise ValueError("workspace root access must be read or write")
            if roots[-1].write_mode not in {"direct", "gated"}:
                raise ValueError("workspace root write_mode must be direct or gated")
            if roots[-1].native_mode not in {"enforced", "mcp_only"}:
                raise ValueError("workspace root native_mode is unsupported")
        if project_id != "system":
            work = self.runtime.resolve_below(project_root, f"runs/{run_id}/work")
            work.mkdir(parents=True, exist_ok=True)
            roots.append(AuthorizedRoot(
                key="run_work", path=work.resolve(), access="write",
                description="Current Run working files and research outputs",
                publish_allowed=False,
            ))
        if stage == "implementation" or candidate_enabled:
            candidate = project_root / "runs" / run_id / "candidate"
            candidate.mkdir(parents=True, exist_ok=True)
            roots.append(AuthorizedRoot(
                key="candidate", path=candidate.resolve(), access="write",
                description="Current Run candidate workspace",
            ))
        return roots

    def snapshot(
        self, project_id: str, run_id: str, stage: str | None,
        *, candidate_only: bool = False, candidate_enabled: bool = False,
        variant: str | None = None,
    ) -> tuple[str, str]:
        suffix = f"-{variant}" if variant else ""
        relative = Path("runs") / run_id / "resolved" / f"workspace-scope{suffix}.json"
        uri = f"vault://{project_id}/{relative.as_posix()}"
        target = self.runtime.ensure(project_id).vault_root / relative
        if target.is_file():
            encoded = target.read_bytes()
            payload = json.loads(encoded.decode("utf-8"))
            if (
                payload.get("schema_version") != "workspace-scope-snapshot.v1"
                or str(payload.get("project_id")) != project_id
                or str(payload.get("run_id")) != run_id
                or payload.get("stage") != stage
                or bool(payload.get("candidate_only", False)) != candidate_only
                or bool(payload.get("candidate_enabled", False)) != candidate_enabled
                or (str(payload.get("variant") or "") or None) != variant
            ):
                raise RuntimeError("workspace scope snapshot conflicts with this Run")
            return uri, hashlib.sha256(encoded).hexdigest()
        roots = self.resolve(
            project_id, run_id, stage,
            pinned_at_creation=True, candidate_only=candidate_only,
            candidate_enabled=candidate_enabled,
        )
        config_payload = self.path.read_bytes() if self.path.is_file() else b""
        payload = {
            "schema_version": "workspace-scope-snapshot.v1",
            "project_id": project_id,
            "run_id": run_id,
            "stage": stage,
            "candidate_only": candidate_only,
            "candidate_enabled": candidate_enabled,
            "variant": variant,
            "scope_config_sha256": hashlib.sha256(config_payload).hexdigest(),
            "roots": [
                {
                    "key": root.key,
                    "path": str(root.path.resolve()),
                    "access": root.access,
                    "description": root.description,
                    "includes": list(root.includes),
                    "excludes": list(root.excludes),
                    "publish_allowed": root.publish_allowed,
                    "grant_id": root.grant_id,
                    "grant_sha256": root.grant_sha256,
                    "write_mode": root.write_mode,
                    "native_mode": root.native_mode,
                    "recursive": root.recursive,
                }
                for root in roots
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2,
        ).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(target)
        return uri, hashlib.sha256(encoded).hexdigest()

    def load_snapshot(
        self, project_id: str, run_id: str, stage: str | None,
        uri: str, expected_sha256: str,
    ) -> list[AuthorizedRoot]:
        prefix = f"vault://{project_id}/"
        if not uri.startswith(prefix):
            raise ValueError("workspace scope snapshot belongs to another Project")
        path = self.runtime.resolve_vault_file(project_id, uri[len(prefix):])
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != expected_sha256:
            raise ValueError("workspace scope snapshot hash differs from the pinned Run")
        payload = json.loads(encoded.decode("utf-8"))
        if payload.get("schema_version") != "workspace-scope-snapshot.v1":
            raise ValueError("unsupported workspace scope snapshot")
        if str(payload.get("project_id")) != project_id or str(payload.get("run_id")) != run_id:
            raise ValueError("workspace scope snapshot identity mismatch")
        if payload.get("stage") != stage:
            raise ValueError("workspace scope snapshot Stage mismatch")
        roots: list[AuthorizedRoot] = []
        seen: set[str] = set()
        for item in payload.get("roots") or []:
            key = str(item.get("key") or "")
            if not key or key in seen:
                raise ValueError("workspace scope snapshot contains duplicate or empty root keys")
            seen.add(key)
            stored_path = Path(str(item.get("path") or ""))
            if not stored_path.is_absolute():
                raise ValueError("workspace scope snapshot root must be absolute")
            resolved_path = stored_path.resolve()
            if resolved_path != stored_path or not resolved_path.is_dir():
                raise PermissionError("workspace scope snapshot root changed or is unavailable")
            access = str(item.get("access") or "")
            if access not in {"read", "write"}:
                raise ValueError("workspace scope snapshot contains unsupported access")
            includes = tuple(str(value) for value in (item.get("includes") or []))
            excludes = tuple(str(value) for value in (item.get("excludes") or []))
            grant_id = str(item.get("grant_id") or "") or None
            roots.append(AuthorizedRoot(
                key=key,
                path=resolved_path,
                access=access,
                description=str(item.get("description") or key),
                includes=includes,
                excludes=excludes,
                publish_allowed=bool(item.get("publish_allowed", True)),
                grant_id=grant_id,
                grant_sha256=str(item.get("grant_sha256") or "") or None,
                write_mode=str(item.get("write_mode") or "direct"),
                native_mode=str(item.get("native_mode") or "enforced"),
                recursive=bool(item.get("recursive", bool(
                    grant_id and not includes and not excludes
                ))),
            ))
        return roots

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

    @staticmethod
    def _tree_excluded(relative: str) -> bool:
        return WorkspaceScopeService._matches(relative, DEFAULT_TREE_EXCLUDES)

    @classmethod
    def _root_excluded(
        cls, root: AuthorizedRoot, relative: str, *, is_directory: bool = False
    ) -> bool:
        normalized = relative.replace("\\", "/")
        if cls._matches(normalized, root.excludes):
            return True
        if root.includes and not is_directory:
            return not cls._matches(normalized, root.includes)
        return False

    def tree(
        self, roots: list[AuthorizedRoot], max_depth: int = 3, limit: int = 400, *,
        selected_paths: dict[str, tuple[str, ...]] | None = None,
        soft_excludes: dict[str, tuple[str, ...]] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        max_depth = min(max(max_depth, 1), 8)
        limit = min(max(limit, 1), 2000)
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        truncated = False
        scanned = 0
        deadline = time.monotonic() + TREE_SCAN_MAX_SECONDS

        def append(uri: str, kind: str, access: str) -> bool:
            nonlocal truncated
            if uri in seen:
                return True
            if len(entries) >= limit:
                truncated = True
                return False
            seen.add(uri)
            entries.append({"uri": uri, "type": kind, "access": access})
            return True

        for root in roots:
            if not append(f"workspace://{root.key}/", "directory", root.access):
                return entries, truncated
            if root.key == "candidate":
                continue
            selections = (
                tuple(selected_paths.get(root.key) or ())
                if selected_paths is not None else ("",)
            )
            root_soft_excludes = tuple((soft_excludes or {}).get(root.key) or ())
            for selection in selections:
                relative_selection = self._safe_context_path(selection)
                target = (root.path / relative_selection).resolve()
                if target != root.path and root.path not in target.parents:
                    raise ValueError("context view escaped its authorized root")
                if not target.exists():
                    continue
                if relative_selection and self._root_excluded(
                    root, relative_selection, is_directory=target.is_dir(),
                ):
                    continue
                if target.is_file():
                    if self._matches(relative_selection, root_soft_excludes):
                        continue
                    if not append(
                        f"workspace://{root.key}/{relative_selection}", "file", root.access,
                    ):
                        return entries, truncated
                    continue
                if relative_selection and not append(
                    f"workspace://{root.key}/{relative_selection}", "directory", root.access,
                ):
                    return entries, truncated
                stack: list[tuple[Path, int]] = [(target, 0)]
                while stack:
                    if time.monotonic() >= deadline or scanned >= TREE_SCAN_MAX_ENTRIES:
                        return entries, True
                    current, depth = stack.pop()
                    try:
                        with os.scandir(current) as iterator:
                            children = sorted(iterator, key=lambda item: item.name.casefold(), reverse=True)
                    except OSError:
                        continue
                    directories: list[Path] = []
                    for item in reversed(children):
                        scanned += 1
                        if time.monotonic() >= deadline or scanned > TREE_SCAN_MAX_ENTRIES:
                            return entries, True
                        try:
                            if item.is_symlink():
                                continue
                            is_directory = item.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        child = Path(item.path)
                        relative = child.relative_to(root.path).as_posix()
                        if (
                            self._root_excluded(root, relative, is_directory=is_directory)
                            or self._tree_excluded(relative)
                            or self._matches(relative, root_soft_excludes)
                        ):
                            continue
                        if depth + 1 <= max_depth and not append(
                            f"workspace://{root.key}/{relative}",
                            "directory" if is_directory else "file", root.access,
                        ):
                            return entries, truncated
                        if is_directory and depth + 1 < max_depth:
                            directories.append(child)
                    for directory in reversed(directories):
                        stack.append((directory, depth + 1))
        return entries, truncated

    def resource_guide(self, project_id: str) -> dict[str, Any]:
        """Return concise, machine-owned placement rules for structural tasks."""
        if project_id == "system":
            return {
                "schema_version": "project-resource-guide.v1",
                "layout_version": "butler",
                "resources": [
                    {"path": "agent-config.yaml", "purpose": "Butler invocation configuration"},
                    {"path": "role.md", "purpose": "Butler identity and responsibility"},
                    {"path": ".agents/skills/<skill>/", "purpose": "Butler Skills"},
                    {"path": "runs/ and runtime/", "purpose": "Kernel-generated state; do not scaffold"},
                ],
            }
        root = self.runtime.ensure(project_id).vault_root
        project_path = root / "project.yaml"
        project = (
            yaml.safe_load(project_path.read_text(encoding="utf-8")) or {}
            if project_path.is_file() else {}
        )
        return {
            "schema_version": "project-resource-guide.v1",
            "layout_version": int(project.get("layout_version") or 1),
            "resources": [
                {"path": "project.yaml", "purpose": "Project identity and active layout"},
                {
                    "path": "source-map.yaml",
                    "purpose": "Soft Context View entrypoints and ignores; never authorization",
                },
                {
                    "path": "agents/<agent-id>/",
                    "purpose": "Published Agent",
                    "required": ["config.yaml", "role.md", "MAP.md"],
                },
                {
                    "path": "knowledge/map/",
                    "purpose": "Living Semantic Map and bounded LLM navigation",
                    "required": ["index.yaml", "overview.md"],
                },
                {
                    "path": "changes/ledger.jsonl",
                    "purpose": "Append-only semantic and implementation change ledger",
                },
                {
                    "path": ".agents/skills/<skill>/",
                    "purpose": "Published Skill bundle",
                    "required": ["SKILL.md", "skill-manifest.json"],
                    "optional": ["scripts/", "references/", "templates/", "evals/"],
                },
                {"path": "schemas/", "purpose": "Project-local input and output Schemas"},
                {"path": "scripts/", "purpose": "Scripts shared by multiple Project capabilities"},
                {
                    "path": "mcp/<server-id>/",
                    "purpose": "Project MCP Server",
                    "required": ["mcp-manifest.yaml", "Python entrypoint"],
                },
                {"path": "artifacts/", "purpose": "Published business artifacts"},
                {"path": "workflows/, services/, schedules/", "purpose": "Optional resource definitions; require a runtime before activation"},
                {"path": "runs/ and runtime/", "purpose": "Kernel-generated evidence and snapshots; do not scaffold"},
            ],
        }

    def resolve_uri(
        self, roots: list[AuthorizedRoot], uri: str, require_write: bool = False
    ) -> tuple[AuthorizedRoot, Path, str]:
        if not uri.startswith("workspace://"):
            raise ValueError("workspace URI is required")
        remainder = uri[len("workspace://"):]
        key, separator, relative = remainder.partition("/")
        if not separator or not key:
            raise ValueError("workspace URI must contain a root key and relative path")
        root = next((item for item in roots if item.key == key), None)
        if root is None:
            raise PermissionError("workspace root is outside the authorized scope")
        if require_write and root.access != "write":
            raise PermissionError("workspace root is not writable")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("workspace URI escaped its authorized root")
        target = (root.path / relative_path).resolve()
        if target != root.path and root.path not in target.parents:
            raise ValueError("workspace URI escaped its authorized root")
        if relative and self._root_excluded(
            root, relative_path.as_posix(), is_directory=target.is_dir()
        ):
            raise PermissionError("path is excluded from the authorized scope")
        resolved_relative = target.relative_to(root.path).as_posix()
        if resolved_relative != relative_path.as_posix() and self._root_excluded(
            root, resolved_relative, is_directory=target.is_dir()
        ):
            raise PermissionError("resolved path is excluded from the authorized scope")
        return root, target, relative_path.as_posix()

    def write_candidate(
        self, roots: list[AuthorizedRoot], target_uri: str, content: str,
        expected_sha256: str | None, expected_state: str | None = None,
    ) -> dict[str, Any]:
        target_root, formal_target, relative = self.resolve_uri(roots, target_uri)
        if target_root.key == "candidate":
            raise ValueError("target_uri must identify the intended formal target")
        candidate_root = next((item for item in roots if item.key == "candidate"), None)
        if candidate_root is None:
            raise PermissionError("this invocation has no candidate write scope")
        original_sha256 = None
        if formal_target.exists():
            if not formal_target.is_file():
                raise ValueError("target_uri must identify a file")
            original_sha256 = hashlib.sha256(formal_target.read_bytes()).hexdigest()
        if expected_state == "absent" and formal_target.exists():
            raise ValueError("formal target exists but candidate expected it to be absent")
        if expected_state == "present" and not formal_target.is_file():
            raise ValueError("formal target is absent but candidate expected it to exist")
        if expected_sha256 is not None and original_sha256 != expected_sha256:
            raise ValueError("formal target changed from the expected baseline")
        destination = candidate_root.path / target_root.key / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = content.encode("utf-8")
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(destination)
        candidate_sha256 = hashlib.sha256(encoded).hexdigest()
        manifest_path = candidate_root.path / "manifest.json"
        manifest = {"schema_version": "candidate-manifest.v2", "files": []}
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = [item for item in manifest.get("files") or [] if item.get("target_uri") != target_uri]
        files.append({
            "operation": "update" if original_sha256 is not None else "create",
            "target_uri": target_uri,
            "candidate_uri": f"workspace://candidate/{target_root.key}/{relative}",
            "original_sha256": original_sha256,
            "candidate_sha256": candidate_sha256,
            "size": len(encoded),
        })
        manifest["schema_version"] = "candidate-manifest.v2"
        manifest["files"] = sorted(files, key=lambda item: str(item["target_uri"]))
        manifest_temp = manifest_path.with_suffix(".tmp")
        manifest_temp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest_temp.replace(manifest_path)
        return files[-1]

    def delete_candidate(
        self, roots: list[AuthorizedRoot], target_uri: str, expected_sha256: str,
    ) -> dict[str, Any]:
        target_root, formal_target, relative = self.resolve_uri(roots, target_uri)
        if target_root.key == "candidate" or not formal_target.is_file():
            raise ValueError("delete target must be an existing formal file")
        actual = hashlib.sha256(formal_target.read_bytes()).hexdigest()
        if not expected_sha256 or actual != expected_sha256:
            raise ValueError("formal target changed from the expected baseline")
        candidate_root = next((item for item in roots if item.key == "candidate"), None)
        if candidate_root is None:
            raise PermissionError("this invocation has no candidate write scope")
        manifest_path = candidate_root.path / "manifest.json"
        manifest = {"schema_version": "candidate-manifest.v2", "files": []}
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = [item for item in manifest.get("files") or [] if item.get("target_uri") != target_uri]
        entry = {
            "operation": "delete", "target_uri": target_uri,
            "candidate_uri": None, "original_sha256": actual,
            "candidate_sha256": None, "size": 0,
        }
        files.append(entry)
        manifest["schema_version"] = "candidate-manifest.v2"
        manifest["files"] = sorted(files, key=lambda item: str(item["target_uri"]))
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(manifest_path)
        return entry
